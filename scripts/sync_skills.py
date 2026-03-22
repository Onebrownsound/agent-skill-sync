#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import sync_agents


SOURCE_REGISTRY_FILENAMES = ("skill-sources.json", "skill-sources.local.json")
DEPLOY_STATE_FILENAME = "deploy-state.local.json"
MANAGED_AGENTS_BEGIN = sync_agents.MANAGED_AGENTS_BEGIN
MANAGED_AGENTS_END = sync_agents.MANAGED_AGENTS_END


class SourceError(Exception):
    pass


def detect_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def detect_host() -> str:
    if os.name == "nt":
        return "windows"
    if os.environ.get("WSL_DISTRO_NAME"):
        return "wsl"
    try:
        version_text = Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        version_text = ""
    if "microsoft" in version_text or "wsl" in version_text:
        return "wsl"
    return "linux"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync source-managed skills to local agent installs.")
    parser.add_argument("--config", default="config/targets.local.json", help="Path to local target config.")
    parser.add_argument("--host", choices=["auto", "windows", "wsl", "linux", "all"], default="auto")
    parser.add_argument("--target", action="append", default=[], help="Limit to one or more target ids.")
    parser.add_argument(
        "--pull",
        action="store_true",
        help="Import valid live skills from matching target installs into this repo.",
    )
    parser.add_argument(
        "--bucket",
        choices=["shared", "codex", "claude"],
        help="Destination bucket to use with --pull. Defaults to the target kind.",
    )
    parser.add_argument(
        "--rollback",
        help="Rollback a previous deployment ticket for matching host targets.",
    )
    parser.add_argument("--check", action="store_true", help="Explicit dry-run mode.")
    parser.add_argument("--apply", action="store_true", help="Apply the sync plan.")
    parser.add_argument("--clean", action="store_true", help="Remove previously managed skills not in source.")
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Disable the default backup step for destructive push changes.",
    )
    parser.add_argument("--quiet", action="store_true", help="Reduce output.")
    parser.add_argument("--ticket-id", help=argparse.SUPPRESS)
    return parser.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def source_registry_paths(repo_root: Path) -> list[Path]:
    return [repo_root / "config" / filename for filename in SOURCE_REGISTRY_FILENAMES]


def load_source_index(repo_root: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for path in source_registry_paths(repo_root):
        if not path.is_file():
            continue
        payload = load_json(path)
        for key, record in payload.get("skills", {}).items():
            if key not in records:
                records[key] = record
    return records


def deploy_state_path(repo_root: Path) -> Path:
    return repo_root / "config" / DEPLOY_STATE_FILENAME


def load_deploy_state(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "targets": {}}
    payload = load_json(path)
    if "targets" not in payload or not isinstance(payload["targets"], dict):
        return {"version": 1, "targets": {}}
    return payload


def save_deploy_state(path: Path, payload: dict) -> None:
    write_json(path, payload)


def iter_skill_dirs(root: Path) -> dict[str, Path]:
    skills: dict[str, Path] = {}
    if not root.is_dir():
        return skills
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not (child / "SKILL.md").is_file():
            continue
        skills[child.name] = child
    return skills


def collect_source_skill_entries(repo_root: Path, catalog: dict[str, str], kind: str) -> dict[str, dict]:
    skills: dict[str, dict] = {}
    for bucket in ("shared", kind):
        rel_path = catalog.get(bucket)
        if not rel_path:
            continue
        for name, path in iter_skill_dirs(repo_root / rel_path).items():
            skills[name] = {
                "path": path,
                "bucket": bucket,
                "key": f"{bucket}/{name}",
            }
    return skills


def collect_source_skills(repo_root: Path, catalog: dict[str, str], kind: str) -> dict[str, Path]:
    return {
        name: item["path"]
        for name, item in collect_source_skill_entries(repo_root, catalog, kind).items()
    }


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def dir_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        snapshot[rel] = file_hash(path)
    return snapshot


def skill_revision(root: Path) -> str:
    digest = hashlib.sha256()
    snapshot = dir_snapshot(root)
    for rel_path in sorted(snapshot):
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(snapshot[rel_path].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return load_json(path)
    except Exception:
        return {}


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def copy_skill(source: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def build_backup_path(backup_run_root: Path, name: str) -> Path:
    candidate = backup_run_root / name
    if not candidate.exists():
        return candidate

    suffix = 1
    while True:
        candidate = backup_run_root / f"{name}-{suffix}"
        if not candidate.exists():
            return candidate
        suffix += 1


def backup_skill(dest: Path, backup_run_root: Path) -> Path:
    ensure_dir(backup_run_root)
    backup_path = build_backup_path(backup_run_root, dest.name)
    shutil.move(str(dest), str(backup_path))
    return backup_path


def generate_ticket() -> str:
    return str(uuid.uuid4())


def ticket_root(target_root: Path, ticket: str) -> Path:
    return target_root / ".skill-sync-tickets" / ticket


def ticket_metadata_path(target_root: Path, ticket: str) -> Path:
    return ticket_root(target_root, ticket) / "ticket.json"


def load_ticket_metadata(path: Path) -> dict:
    return load_json(path)


def target_root_for_runtime(target_cfg: dict, runtime_host: str) -> Path:
    target_host = target_cfg.get("host")
    path = target_cfg["path"]
    if target_host == runtime_host:
        return Path(path)
    if target_host == "wsl" and runtime_host == "windows":
        raise SourceError(
            f"WSL target '{path}' must be synced from WSL or delegated via wsl.exe, not from Windows filesystem paths."
        )
    if target_host == "windows" and runtime_host in {"wsl", "linux"}:
        raise SourceError(
            f"Windows target '{path}' must be synced from Windows, not from a {runtime_host} runtime."
        )
    return Path(path)


def to_wsl_path(path: Path) -> str:
    path_str = str(path)
    if len(path_str) >= 3 and path_str[1:3] == ":\\":
        drive = path_str[0].lower()
        tail = path_str[2:].replace("\\", "/")
        return f"/mnt/{drive}{tail}"
    return path.as_posix()


def target_ids_for_host(config: dict, target_filter: set[str], host_name: str) -> list[str]:
    target_ids: list[str] = []
    for target_id, target_cfg in config.get("targets", {}).items():
        if target_filter and target_id not in target_filter:
            continue
        if not target_cfg.get("enabled", False):
            continue
        if target_cfg.get("host") == host_name:
            target_ids.append(target_id)
    return target_ids


def delegated_sync_args(
    args: argparse.Namespace,
    *,
    host_override: str,
    target_ids: list[str],
) -> list[str]:
    command = ["python3", "scripts/sync_skills.py", "--host", host_override, "--config", args.config]
    for target_id in target_ids:
        command.extend(["--target", target_id])
    if args.pull:
        command.append("--pull")
    if args.bucket:
        command.extend(["--bucket", args.bucket])
    if args.rollback:
        command.extend(["--rollback", args.rollback])
    if args.apply:
        command.append("--apply")
    if args.check:
        command.append("--check")
    if args.clean:
        command.append("--clean")
    if args.no_backup:
        command.append("--no-backup")
    if args.quiet:
        command.append("--quiet")
    if args.ticket_id:
        command.extend(["--ticket-id", args.ticket_id])
    return command


def run_delegated_wsl_sync(
    repo_root: Path,
    args: argparse.Namespace,
    target_ids: list[str],
    *,
    runner=subprocess.run,
) -> int:
    repo_root_wsl = to_wsl_path(repo_root)
    delegated = delegated_sync_args(args, host_override="wsl", target_ids=target_ids)
    shell_command = "cd " + shlex.quote(repo_root_wsl) + " && " + " ".join(shlex.quote(arg) for arg in delegated)
    result = runner(["wsl.exe", "bash", "-lc", shell_command], text=True)
    return result.returncode


def maybe_delegate_wsl_targets(
    *,
    repo_root: Path,
    config: dict,
    args: argparse.Namespace,
    requested_host: str,
    runtime_host: str,
    runner=subprocess.run,
) -> tuple[str | None, int | None]:
    if runtime_host != "windows":
        return requested_host, None

    target_filter = set(args.target)
    wsl_target_ids = target_ids_for_host(config, target_filter, "wsl")
    if not wsl_target_ids:
        return requested_host, None

    should_delegate = requested_host in {"all", "wsl"} or bool(target_filter)
    if not should_delegate:
        return requested_host, None

    if args.apply and not args.pull and not args.rollback and not args.ticket_id:
        args.ticket_id = generate_ticket()

    exit_code = run_delegated_wsl_sync(repo_root, args, wsl_target_ids, runner=runner)
    if exit_code != 0:
        return None, exit_code

    if requested_host == "wsl":
        return None, 0

    if requested_host == "all":
        return "windows", None

    if target_filter and not target_ids_for_host(config, target_filter, "windows"):
        return None, 0

    return requested_host, None


def plan_push_target(
    repo_root: Path,
    config: dict,
    target_id: str,
    target_cfg: dict,
    host: str,
    runtime_host: str | None = None,
) -> dict | None:
    if not target_cfg.get("enabled", False):
        return None
    if host != "all" and target_cfg.get("host") != host:
        return None
    if target_cfg.get("kind") not in ("codex", "claude"):
        raise ValueError(f"Unsupported target kind for {target_id}: {target_cfg.get('kind')}")

    target_root = target_root_for_runtime(target_cfg, runtime_host or host)
    manifest_name = config.get("manifest_filename", ".skill-sync-manifest.json")
    manifest_path = target_root / manifest_name
    manifest = load_manifest(manifest_path)

    source_skills = collect_source_skills(repo_root, config["catalog"], target_cfg["kind"])
    desired_names = sorted(source_skills)
    managed_names = sorted(manifest.get("skills", []))

    to_add: list[str] = []
    to_update: list[str] = []
    unchanged: list[str] = []
    for name, source_path in source_skills.items():
        dest_path = target_root / name
        if not dest_path.exists():
            to_add.append(name)
            continue
        if dir_snapshot(source_path) != dir_snapshot(dest_path):
            to_update.append(name)
        else:
            unchanged.append(name)

    to_remove = sorted(name for name in managed_names if name not in desired_names)
    agent_plan = sync_agents.plan_agent_sync(
        repo_root=repo_root,
        kind=target_cfg["kind"],
        target_root=target_root,
        managed_agents=sorted(manifest.get("agents", [])),
    )

    return {
        "id": target_id,
        "kind": target_cfg["kind"],
        "host": target_cfg["host"],
        "root": str(target_root),
        "manifest": str(manifest_path),
        "desired": desired_names,
        "add": sorted(to_add),
        "update": sorted(to_update),
        "unchanged": sorted(unchanged),
        "remove": to_remove,
        "source_skills": {name: str(path) for name, path in source_skills.items()},
        **agent_plan,
    }


def plan_pull_target(
    repo_root: Path,
    config: dict,
    target_id: str,
    target_cfg: dict,
    host: str,
    bucket: str | None = None,
    runtime_host: str | None = None,
) -> dict | None:
    if not target_cfg.get("enabled", False):
        return None
    if host != "all" and target_cfg.get("host") != host:
        return None
    if target_cfg.get("kind") not in ("codex", "claude"):
        raise ValueError(f"Unsupported target kind for {target_id}: {target_cfg.get('kind')}")

    bucket_name = bucket or target_cfg["kind"]
    bucket_rel = config["catalog"].get(bucket_name)
    if not bucket_rel:
        raise ValueError(f"Missing catalog bucket '{bucket_name}'")

    target_root = target_root_for_runtime(target_cfg, runtime_host or host)
    repo_bucket_root = repo_root / bucket_rel
    live_skills = iter_skill_dirs(target_root)

    to_add: list[str] = []
    unchanged: list[str] = []
    conflict: list[str] = []
    for name, source_path in live_skills.items():
        dest_path = repo_bucket_root / name
        if not dest_path.exists():
            to_add.append(name)
            continue
        if dir_snapshot(source_path) == dir_snapshot(dest_path):
            unchanged.append(name)
        else:
            conflict.append(name)

    return {
        "id": target_id,
        "kind": target_cfg["kind"],
        "host": target_cfg["host"],
        "root": str(target_root),
        "bucket": bucket_name,
        "bucket_root": str(repo_bucket_root),
        "add": sorted(to_add),
        "conflict": sorted(conflict),
        "unchanged": sorted(unchanged),
        "source_skills": {name: str(path) for name, path in live_skills.items()},
    }


def plan_rollback_target(
    config: dict,
    target_id: str,
    target_cfg: dict,
    host: str,
    ticket: str,
    runtime_host: str | None = None,
) -> dict | None:
    if host != "all" and target_cfg.get("host") != host:
        return None
    if target_cfg.get("kind") not in ("codex", "claude"):
        raise ValueError(f"Unsupported target kind for {target_id}: {target_cfg.get('kind')}")

    target_root = target_root_for_runtime(target_cfg, runtime_host or host)
    metadata_path = ticket_metadata_path(target_root, ticket)
    if not metadata_path.is_file():
        return None

    metadata = load_ticket_metadata(metadata_path)
    return {
        "id": target_id,
        "kind": target_cfg["kind"],
        "host": target_cfg["host"],
        "root": str(target_root),
        "ticket": ticket,
        "ticket_root": str(metadata_path.parent),
        "metadata_path": str(metadata_path),
        "added": sorted(metadata.get("added", [])),
        "updated": sorted(metadata.get("updated", [])),
        "removed": sorted(metadata.get("removed", [])),
        "backed_up": sorted(metadata.get("backed_up", [])),
        "added_agents": sorted(metadata.get("added_agents", [])),
        "updated_agents": sorted(metadata.get("updated_agents", [])),
        "removed_agents": sorted(metadata.get("removed_agents", [])),
        "backed_up_agents": sorted(metadata.get("backed_up_agents", [])),
        "rollback_ready": bool(metadata.get("rollback_ready", False)),
        "previous_manifest": metadata.get("previous_manifest"),
        "manifest": str(target_root / config.get("manifest_filename", ".skill-sync-manifest.json")),
        "source_skills": metadata.get("source_skills", {}),
        "source_agents": metadata.get("source_agents", {}),
        "agent_root": metadata.get("agent_root"),
        "agent_backup_root": metadata.get("agent_backup_root"),
        "codex_config": metadata.get("codex_config"),
    }


def apply_target(plan: dict, clean: bool, backup: bool = True, ticket: str | None = None) -> dict:
    target_root = Path(plan["root"])
    manifest_path = Path(plan["manifest"])
    ensure_dir(target_root)

    source_skills = {name: Path(path) for name, path in plan["source_skills"].items()}
    backup_run_root: Path | None = None
    backup_records: list[tuple[str, str]] = []
    previous_manifest = load_manifest(manifest_path) if manifest_path.exists() else None
    wrote_ticket = False

    changed = bool(
        plan["add"]
        or plan["update"]
        or (clean and plan["remove"])
        or plan.get("agent_add")
        or plan.get("agent_update")
        or (clean and plan.get("agent_remove"))
        or (plan.get("codex_config") and plan["codex_config"].get("update_needed"))
    )
    ticket_value = ticket if changed else None
    ticket_dir: Path | None = None
    if ticket_value:
        ticket_dir = ticket_root(target_root, ticket_value)

    if backup and (plan["update"] or (clean and plan["remove"])):
        if ticket_dir is not None:
            backup_run_root = ticket_dir / "skills"
        else:
            backup_run_root = target_root / ".skill-sync-backups" / timestamp_slug()

    for name in plan["add"]:
        copy_skill(source_skills[name], target_root / name)

    for name in plan["update"]:
        dest_path = target_root / name
        if backup and dest_path.exists():
            assert backup_run_root is not None
            backup_path = backup_skill(dest_path, backup_run_root)
            backup_records.append((name, str(backup_path)))
        copy_skill(source_skills[name], dest_path)

    if clean:
        for name in plan["remove"]:
            skill_dir = target_root / name
            if skill_dir.exists():
                if backup:
                    assert backup_run_root is not None
                    backup_path = sync_agents.backup_path_entry(skill_dir, backup_run_root)
                    backup_records.append((name, str(backup_path)))
                else:
                    shutil.rmtree(skill_dir)
    agent_result = sync_agents.apply_agent_sync(
        plan,
        backup=backup,
        clean=clean,
        ticket_dir=ticket_dir,
    )

    write_json(
        manifest_path,
        {
            "version": 1,
            "target": plan["id"],
            "kind": plan["kind"],
            "skills": plan["desired"],
            "agents": plan.get("desired_agents", []),
        },
    )

    if ticket_dir is not None:
        ensure_dir(ticket_dir)
        rollback_requires_backup = bool(
            plan["update"]
            or (clean and plan["remove"])
            or plan.get("agent_update")
            or (clean and plan.get("agent_remove"))
            or (
                plan.get("codex_config")
                and plan["codex_config"].get("update_needed")
                and plan["codex_config"].get("previous_exists")
            )
        )
        write_json(
            ticket_dir / "ticket.json",
            {
                "version": 1,
                "ticket": ticket_value,
                "timestamp": datetime.now().isoformat(),
                "target": plan["id"],
                "kind": plan["kind"],
                "root": plan["root"],
                "added": plan["add"],
                "updated": plan["update"],
                "removed": plan["remove"] if clean else [],
                "backed_up": [name for name, _ in backup_records],
                "backup_root": str(backup_run_root) if backup_run_root else None,
                "added_agents": plan.get("agent_add", []),
                "updated_agents": plan.get("agent_update", []),
                "removed_agents": plan.get("agent_remove", []) if clean else [],
                "backed_up_agents": [name for name, _ in agent_result["agent_backups"]],
                "agent_root": plan.get("agent_root"),
                "agent_backup_root": agent_result["agent_backup_root"],
                "codex_config_changed": agent_result["codex_config_changed"],
                "codex_config": {
                    "config_path": plan["codex_config"]["config_path"],
                    "changed": agent_result["codex_config_changed"],
                    "previous_exists": plan["codex_config"].get("previous_exists", False),
                    "backup_path": agent_result["codex_config_backup"],
                    "registered": plan["codex_config"].get("registered", []),
                    "skipped": plan["codex_config"].get("skipped", []),
                }
                if plan.get("codex_config")
                else None,
                "rollback_ready": backup or not rollback_requires_backup,
                "previous_manifest": previous_manifest,
                "source_skills": plan["source_skills"],
                "source_agents": plan.get("source_agents", {}),
            },
        )
        wrote_ticket = True
    return {
        "backup_root": str(backup_run_root) if backup_run_root else None,
        "backups": backup_records,
        "agent_backup_root": agent_result["agent_backup_root"],
        "agent_backups": agent_result["agent_backups"],
        "codex_config_backup": agent_result["codex_config_backup"],
        "ticket": ticket_value,
        "ticket_root": str(ticket_dir) if ticket_dir else None,
        "wrote_ticket": wrote_ticket,
    }


def apply_pull_target(plan: dict) -> None:
    bucket_root = Path(plan["bucket_root"])
    ensure_dir(bucket_root)
    source_skills = {name: Path(path) for name, path in plan["source_skills"].items()}

    for name in plan["add"]:
        copy_skill(source_skills[name], bucket_root / name)


def apply_rollback_target(plan: dict) -> None:
    target_root = Path(plan["root"])
    metadata_path = Path(plan["metadata_path"])
    metadata = load_ticket_metadata(metadata_path)
    backup_root_value = metadata.get("backup_root")
    backup_root = Path(backup_root_value) if backup_root_value else None
    manifest_path = Path(plan["manifest"])

    for name in plan["added"]:
        dest = target_root / name
        if dest.exists():
            shutil.rmtree(dest)

    for name in sorted(set(plan["updated"] + plan["removed"])):
        if backup_root is None:
            continue
        backup_skill_dir = backup_root / name
        if not backup_skill_dir.exists():
            continue
        dest = target_root / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(backup_skill_dir, dest)
    sync_agents.rollback_agent_sync(plan)

    previous_manifest = plan["previous_manifest"]
    if previous_manifest is None:
        if manifest_path.exists():
            manifest_path.unlink()
    else:
        write_json(manifest_path, previous_manifest)


def print_push_plan(plan: dict) -> None:
    print(f"[{plan['id']}] {plan['kind']} -> {plan['root']}")
    print(f"  add: {len(plan['add'])}")
    if plan["add"]:
        for name in plan["add"]:
            print(f"    + {name}")
    print(f"  update: {len(plan['update'])}")
    if plan["update"]:
        for name in plan["update"]:
            print(f"    ~ {name}")
    print(f"  remove: {len(plan['remove'])}")
    if plan["remove"]:
        for name in plan["remove"]:
            print(f"    - {name}")
    print(f"  unchanged: {len(plan['unchanged'])}")
    print(f"  agent add: {len(plan.get('agent_add', []))}")
    if plan.get("agent_add"):
        for name in plan["agent_add"]:
            print(f"    + {name}")
    print(f"  agent update: {len(plan.get('agent_update', []))}")
    if plan.get("agent_update"):
        for name in plan["agent_update"]:
            print(f"    ~ {name}")
    print(f"  agent remove: {len(plan.get('agent_remove', []))}")
    if plan.get("agent_remove"):
        for name in plan["agent_remove"]:
            print(f"    - {name}")
    if plan.get("codex_config"):
        config_plan = plan["codex_config"]
        print(f"  codex config update: {'yes' if config_plan['update_needed'] else 'no'}")
        if config_plan.get("skipped"):
            for item in config_plan["skipped"]:
                print(f"    ! {item['name']} ({item['reason']})")


def print_pull_plan(plan: dict) -> None:
    print(f"[{plan['id']}] import {plan['kind']} -> {plan['bucket_root']}")
    print(f"  add: {len(plan['add'])}")
    if plan["add"]:
        for name in plan["add"]:
            print(f"    + {name}")
    print(f"  conflict: {len(plan['conflict'])}")
    if plan["conflict"]:
        for name in plan["conflict"]:
            print(f"    ! {name}")
    print(f"  unchanged: {len(plan['unchanged'])}")


def print_rollback_plan(plan: dict) -> None:
    print(f"[{plan['id']}] rollback {plan['ticket']} -> {plan['root']}")
    print(f"  remove added: {len(plan['added'])}")
    if plan["added"]:
        for name in plan["added"]:
            print(f"    - {name}")
    print(f"  restore backed up: {len(plan['backed_up'])}")
    if plan["backed_up"]:
        for name in plan["backed_up"]:
            print(f"    + {name}")
    print(f"  remove added agents: {len(plan.get('added_agents', []))}")
    if plan.get("added_agents"):
        for name in plan["added_agents"]:
            print(f"    - {name}")
    print(f"  restore backed up agents: {len(plan.get('backed_up_agents', []))}")
    if plan.get("backed_up_agents"):
        for name in plan["backed_up_agents"]:
            print(f"    + {name}")
    print(f"  rollback ready: {'yes' if plan['rollback_ready'] else 'no'}")


def refresh_deploy_state(
    *,
    repo_root: Path,
    config: dict,
    target_ids: list[str],
    host: str,
    action: str,
    ticket: str | None = None,
    runtime_host: str | None = None,
) -> dict:
    state_file = deploy_state_path(repo_root)
    state = load_deploy_state(state_file)
    state["version"] = 1
    state["updated_at"] = datetime.now().isoformat()
    source_index = load_source_index(repo_root)
    target_filter = set(target_ids)

    for target_id, target_cfg in config.get("targets", {}).items():
        if target_filter and target_id not in target_filter:
            continue
        if host != "all" and target_cfg.get("host") != host:
            continue
        if not target_cfg.get("enabled", False):
            continue
        if target_cfg.get("kind") not in ("codex", "claude"):
            continue

        target_root = target_root_for_runtime(target_cfg, runtime_host or host)
        manifest_name = config.get("manifest_filename", ".skill-sync-manifest.json")
        manifest = load_manifest(target_root / manifest_name)
        source_entries = collect_source_skill_entries(repo_root, config["catalog"], target_cfg["kind"])
        previous_target_state = state["targets"].get(target_id, {})
        previous_skill_states = previous_target_state.get("skills", {})
        target_state = {
            "id": target_id,
            "host": target_cfg["host"],
            "kind": target_cfg["kind"],
            "root": str(target_root),
            "manifest": str(target_root / manifest_name),
            "last_checked_at": datetime.now().isoformat(),
            "last_action": action,
            "last_action_at": datetime.now().isoformat(),
            "last_ticket": ticket if ticket else previous_target_state.get("last_ticket"),
            "skills": {},
        }

        managed_names = set(manifest.get("skills", []))
        for name, entry in source_entries.items():
            source_path = entry["path"]
            target_path = target_root / name
            repo_revision = skill_revision(source_path)
            target_revision = skill_revision(target_path) if target_path.exists() else None
            target_up_to_date = target_revision == repo_revision
            if target_up_to_date:
                status = "up_to_date"
            elif target_path.exists():
                status = "out_of_date"
            else:
                status = "missing"

            tracked = source_index.get(entry["key"])
            if tracked:
                source_type = tracked.get("source_type", "repo")
                source_payload = tracked.get("source")
                source_resolved_revision = tracked.get("resolved_revision")
                tracked_scope = tracked.get("scope")
            else:
                source_type = "repo"
                source_payload = None
                source_resolved_revision = None
                tracked_scope = "repo"

            previous_skill = previous_skill_states.get(entry["key"], {})
            target_state["skills"][entry["key"]] = {
                "key": entry["key"],
                "name": name,
                "bucket": entry["bucket"],
                "repo_revision": repo_revision,
                "target_revision": target_revision,
                "source_type": source_type,
                "source": source_payload,
                "source_resolved_revision": source_resolved_revision,
                "tracked_scope": tracked_scope,
                "managed_by_repo": name in managed_names,
                "deployed_to_target": target_path.exists() and name in managed_names,
                "present_in_target": target_path.exists(),
                "target_up_to_date": target_up_to_date,
                "status": status,
                "last_ticket": ticket if ticket else previous_skill.get("last_ticket"),
            }

        state["targets"][target_id] = target_state

    save_deploy_state(state_file, state)
    return state


def main() -> int:
    args = parse_args()
    repo_root = detect_repo_root()
    runtime_host = detect_host()
    host = runtime_host if args.host == "auto" else args.host
    config_path = (repo_root / args.config).resolve()

    if args.pull and args.rollback:
        print("--pull and --rollback cannot be used together.", file=sys.stderr)
        return 2
    if args.pull and args.clean:
        print("--clean is only supported for push syncs.", file=sys.stderr)
        return 2
    if args.pull and args.no_backup:
        print("--no-backup only applies to push syncs.", file=sys.stderr)
        return 2
    if args.rollback and args.clean:
        print("--clean cannot be used with --rollback.", file=sys.stderr)
        return 2
    if args.rollback and args.bucket:
        print("--bucket cannot be used with --rollback.", file=sys.stderr)
        return 2
    if args.rollback and args.no_backup:
        print("--no-backup cannot be used with --rollback.", file=sys.stderr)
        return 2
    if args.bucket and not args.pull:
        print("--bucket can only be used with --pull.", file=sys.stderr)
        return 2

    if not config_path.is_file():
        print(f"Missing config file: {config_path}", file=sys.stderr)
        print("Copy config/targets.example.json to config/targets.local.json and edit the target paths.", file=sys.stderr)
        return 2

    config = load_json(config_path)
    targets = config.get("targets", {})
    if not targets:
        print("No targets configured.", file=sys.stderr)
        return 2

    host, delegated_exit = maybe_delegate_wsl_targets(
        repo_root=repo_root,
        config=config,
        args=args,
        requested_host=host,
        runtime_host=runtime_host,
    )
    if delegated_exit is not None:
        return delegated_exit
    if host is None:
        return 0

    target_filter = set(args.target)
    selected = []
    for target_id, target_cfg in targets.items():
        if target_filter and target_id not in target_filter:
            continue
        if args.rollback:
            plan = plan_rollback_target(
                config,
                target_id,
                target_cfg,
                host,
                ticket=args.rollback,
                runtime_host=runtime_host,
            )
        elif args.pull:
            plan = plan_pull_target(
                repo_root,
                config,
                target_id,
                target_cfg,
                host,
                bucket=args.bucket,
                runtime_host=runtime_host,
            )
        else:
            plan = plan_push_target(
                repo_root,
                config,
                target_id,
                target_cfg,
                host,
                runtime_host=runtime_host,
            )
        if plan:
            selected.append(plan)

    if not selected:
        if args.rollback:
            print(f"No rollback data found for ticket '{args.rollback}' on host '{host}'.")
            return 0
        print(f"No enabled targets matched host '{host}'.")
        return 0

    print(f"Repo root: {repo_root}")
    print(f"Host: {host}")
    if args.rollback:
        mode_name = "rollback"
    elif args.pull:
        mode_name = "pull"
    else:
        mode_name = "push"
    print(f"Mode: {mode_name}/{ 'apply' if args.apply else 'check' }")
    print()

    for plan in selected:
        if args.rollback:
            print_rollback_plan(plan)
        elif args.pull:
            print_pull_plan(plan)
        else:
            print_push_plan(plan)
        print()

    if args.apply:
        if args.rollback:
            blocked = [plan["id"] for plan in selected if not plan["rollback_ready"]]
            if blocked:
                print(
                    "Rollback unavailable for ticket on target(s) without backups: "
                    + ", ".join(blocked),
                    file=sys.stderr,
                )
                return 2
            for plan in selected:
                apply_rollback_target(plan)
            refresh_deploy_state(
                repo_root=repo_root,
                config=config,
                target_ids=[plan["id"] for plan in selected],
                host=host,
                action="rollback",
                ticket=args.rollback,
                runtime_host=runtime_host,
            )
            print(f"Rollback complete for ticket {args.rollback}.")
        elif args.pull:
            for plan in selected:
                apply_pull_target(plan)
            if any(plan["conflict"] for plan in selected):
                print("Import complete. Conflicting repo skills were left unchanged.")
            else:
                print("Import complete.")
        else:
            backup_runs: list[dict] = []
            deployment_ticket = (args.ticket_id if args.ticket_id else generate_ticket()) if any(
                plan["add"]
                or plan["update"]
                or (args.clean and plan["remove"])
                or plan.get("agent_add")
                or plan.get("agent_update")
                or (args.clean and plan.get("agent_remove"))
                or (plan.get("codex_config") and plan["codex_config"].get("update_needed"))
                for plan in selected
            ) else None
            for plan in selected:
                result = apply_target(
                    plan,
                    clean=args.clean,
                    backup=not args.no_backup,
                    ticket=deployment_ticket,
                )
                if result["backups"] or result.get("agent_backups") or result.get("codex_config_backup"):
                    backup_runs.append({"target": plan["id"], **result})
            if deployment_ticket:
                print(f"Ticket: {deployment_ticket}")
            if backup_runs:
                for backup_run in backup_runs:
                    if backup_run.get("backup_root"):
                        print(f"Skill backups for {backup_run['target']}: {backup_run['backup_root']}")
                    if backup_run.get("agent_backup_root"):
                        print(f"Agent backups for {backup_run['target']}: {backup_run['agent_backup_root']}")
                    if backup_run.get("codex_config_backup"):
                        print(f"Config backup for {backup_run['target']}: {backup_run['codex_config_backup']}")
            refresh_deploy_state(
                repo_root=repo_root,
                config=config,
                target_ids=[plan["id"] for plan in selected],
                host=host,
                action="apply",
                ticket=deployment_ticket,
                runtime_host=runtime_host,
            )
            print("Sync complete.")
    else:
        if not args.pull:
            refresh_deploy_state(
                repo_root=repo_root,
                config=config,
                target_ids=[plan["id"] for plan in selected],
                host=host,
                action="check" if not args.rollback else "rollback-check",
                ticket=args.rollback if args.rollback else None,
                runtime_host=runtime_host,
            )
        print("Dry run only. Re-run with --apply to make changes.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
