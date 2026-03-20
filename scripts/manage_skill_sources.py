#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile


VALID_BUCKETS = {"shared", "codex", "claude"}
VALID_SCOPES = {"repo", "local"}
DEFAULT_REF = "main"
DEPLOY_STATE_FILENAME = "deploy-state.local.json"
MANAGED_AGENTS_BEGIN = "# BEGIN agent-skill-sync managed agents"
MANAGED_AGENTS_END = "# END agent-skill-sync managed agents"


class SourceError(Exception):
    pass


@dataclass
class GithubSource:
    repo: str
    skill_path: str
    ref: str = DEFAULT_REF
    method: str = "auto"


def detect_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def timestamp() -> str:
    return datetime.now().isoformat()


def load_registry(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "skills": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "skills" not in payload or not isinstance(payload["skills"], dict):
        raise SourceError(f"Invalid registry file: {path}")
    return payload


def save_registry(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def deploy_state_path(repo_root: Path) -> Path:
    return repo_root / "config" / DEPLOY_STATE_FILENAME


def load_deploy_state(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "targets": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "targets" not in payload or not isinstance(payload["targets"], dict):
        return {"version": 1, "targets": {}}
    return payload


def registry_path(repo_root: Path, scope: str) -> Path:
    if scope not in VALID_SCOPES:
        raise SourceError(f"Unsupported scope: {scope}")
    filename = "skill-sources.json" if scope == "repo" else "skill-sources.local.json"
    return repo_root / "config" / filename


def registry_key(bucket: str, name: str) -> str:
    return f"{bucket}/{name}"


def validate_bucket(bucket: str) -> None:
    if bucket not in VALID_BUCKETS:
        raise SourceError(f"Unsupported bucket: {bucket}")


def validate_skill_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise SourceError("Skill name must be a single path segment.")


def ensure_skill_dir(path: Path) -> None:
    if not path.is_dir():
        raise SourceError(f"Skill directory not found: {path}")
    if not (path / "SKILL.md").is_file():
        raise SourceError(f"SKILL.md not found in skill directory: {path}")


def classify_repo_asset(relative_path: str, marker_name: str) -> dict:
    normalized = relative_path.replace("\\", "/").strip("/")
    parts = normalized.split("/") if normalized else []
    bucket = "unknown"
    harness = None
    asset_type = "unknown"
    install_strategy = "ignore"

    if marker_name == "SKILL.md":
        asset_type = "skill"
        install_strategy = "installable"
        if len(parts) >= 3 and parts[0] == ".claude" and parts[1] == "skills":
            bucket = "claude"
            harness = "claude"
        elif len(parts) >= 3 and parts[0] == ".codex" and parts[1] == "skills":
            bucket = "codex"
            harness = "codex"
        elif len(parts) >= 3 and parts[0] == "claude" and parts[1] == "skills":
            bucket = "claude"
            harness = "claude"
        elif len(parts) >= 3 and parts[0] == "codex" and parts[1] == "skills":
            bucket = "codex"
            harness = "codex"
        elif len(parts) >= 2 and parts[0] == "skills":
            bucket = "shared"
        elif len(parts) >= 3 and parts[0].startswith(".") and parts[1] == "skills":
            harness = parts[0].lstrip(".")
        elif len(parts) >= 3 and parts[1] == "skills":
            harness = parts[0]
    else:
        asset_type = "agent"
        install_strategy = "manual"
        if len(parts) >= 3 and parts[0] == ".claude" and parts[1] == "agents":
            bucket = "claude"
            harness = "claude"
        elif len(parts) >= 3 and parts[0] == ".codex" and parts[1] == "agents":
            bucket = "codex"
            harness = "codex"
        elif len(parts) >= 2 and parts[0] == "agents":
            bucket = "shared"
        elif len(parts) >= 3 and parts[0].startswith(".") and parts[1] == "agents":
            harness = parts[0].lstrip(".")
        elif len(parts) >= 3 and parts[1] == "agents":
            harness = parts[0]

    name = parts[-1] if parts else normalized
    if asset_type == "agent" and "." in name:
        name = name.rsplit(".", 1)[0]

    return {
        "path": normalized,
        "name": name,
        "bucket": bucket,
        "harness": harness,
        "asset_type": asset_type,
        "install_strategy": install_strategy,
    }


def skill_dest(repo_root: Path, bucket: str, name: str) -> Path:
    validate_bucket(bucket)
    validate_skill_name(name)
    return repo_root / "skills" / bucket / name


def skill_snapshot(path: Path) -> str:
    ensure_skill_dir(path)
    digest = hashlib.sha256()
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(path).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def copy_skill(source_dir: Path, dest_dir: Path) -> None:
    ensure_skill_dir(source_dir)
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, dest_dir)


def normalize_plugin_path(path: Path) -> Path:
    return Path(os.path.expanduser(str(path))).resolve()


def list_records(repo_root: Path) -> list[dict]:
    records: list[dict] = []
    deploy_state = load_deploy_state(deploy_state_path(repo_root))
    for scope in ("repo", "local"):
        registry = load_registry(registry_path(repo_root, scope))
        for key, record in registry["skills"].items():
            item = dict(record)
            item["key"] = key
            deployments: dict[str, dict] = {}
            for target_id, target in deploy_state.get("targets", {}).items():
                skill_state = target.get("skills", {}).get(key)
                if skill_state:
                    deployments[target_id] = {
                        "status": skill_state.get("status"),
                        "target_up_to_date": skill_state.get("target_up_to_date"),
                    }
            item["deployments"] = deployments
            records.append(item)
    return sorted(records, key=lambda record: record["key"])


def find_record(repo_root: Path, key: str) -> tuple[str, dict]:
    found: list[tuple[str, dict]] = []
    for scope in ("repo", "local"):
        registry = load_registry(registry_path(repo_root, scope))
        record = registry["skills"].get(key)
        if record:
            found.append((scope, record))
    if not found:
        raise SourceError(f"Tracked skill not found: {key}")
    if len(found) > 1:
        raise SourceError(f"Tracked skill exists in multiple registries: {key}")
    return found[0]


def ensure_key_available(repo_root: Path, key: str) -> None:
    for scope in ("repo", "local"):
        registry = load_registry(registry_path(repo_root, scope))
        if key in registry["skills"]:
            raise SourceError(f"Tracked skill already exists: {key}")


def record_install(
    repo_root: Path,
    *,
    bucket: str,
    name: str,
    scope: str,
    source_type: str,
    source_payload: dict,
    source_dir: Path,
    resolved_revision: str,
) -> dict:
    validate_bucket(bucket)
    validate_skill_name(name)
    if scope not in VALID_SCOPES:
        raise SourceError(f"Unsupported scope: {scope}")
    ensure_skill_dir(source_dir)

    key = registry_key(bucket, name)
    ensure_key_available(repo_root, key)
    dest = skill_dest(repo_root, bucket, name)
    if dest.exists():
        raise SourceError(f"Destination already exists in repo: {dest}")

    copy_skill(source_dir, dest)

    registry_file = registry_path(repo_root, scope)
    registry = load_registry(registry_file)
    registry["skills"][key] = {
        "name": name,
        "bucket": bucket,
        "dest": dest.relative_to(repo_root).as_posix(),
        "scope": scope,
        "source_type": source_type,
        "source": source_payload,
        "resolved_revision": resolved_revision,
        "installed_at": timestamp(),
        "updated_at": timestamp(),
    }
    save_registry(registry_file, registry)
    return {"key": key, "dest": str(dest), "registry": str(registry_file)}


def install_plugin_skill(
    *,
    repo_root: Path,
    bucket: str,
    plugin_path: Path,
    name: str | None = None,
    scope: str = "local",
) -> dict:
    source_dir = normalize_plugin_path(plugin_path)
    ensure_skill_dir(source_dir)
    skill_name = name or source_dir.name
    revision = skill_snapshot(source_dir)
    return record_install(
        repo_root,
        bucket=bucket,
        name=skill_name,
        scope=scope,
        source_type="plugin",
        source_payload={"path": str(source_dir)},
        source_dir=source_dir,
        resolved_revision=revision,
    )


def install_materialized_github_skill(
    *,
    repo_root: Path,
    bucket: str,
    source_dir: Path,
    repo: str,
    skill_path: str,
    ref: str = DEFAULT_REF,
    resolved_revision: str,
    name: str | None = None,
    scope: str = "repo",
) -> dict:
    ensure_skill_dir(source_dir)
    validate_relative_repo_path(skill_path)
    skill_name = name or Path(skill_path).name
    return record_install(
        repo_root,
        bucket=bucket,
        name=skill_name,
        scope=scope,
        source_type="github",
        source_payload={"repo": repo, "path": skill_path, "ref": ref},
        source_dir=source_dir,
        resolved_revision=resolved_revision,
    )


def update_tracked_skill(
    repo_root: Path,
    key: str,
    github_loader=None,
) -> dict:
    scope, record = find_record(repo_root, key)
    dest = repo_root / record["dest"]

    if record["source_type"] == "plugin":
        source_dir = normalize_plugin_path(Path(record["source"]["path"]))
        ensure_skill_dir(source_dir)
        resolved_revision = skill_snapshot(source_dir)
    elif record["source_type"] == "github":
        loader = github_loader or load_github_source_for_record
        source_dir, resolved_revision = loader(record)
    else:
        raise SourceError(f"Unsupported source type: {record['source_type']}")

    copy_skill(source_dir, dest)

    registry_file = registry_path(repo_root, scope)
    registry = load_registry(registry_file)
    stored = registry["skills"][key]
    stored["resolved_revision"] = resolved_revision
    stored["updated_at"] = timestamp()
    save_registry(registry_file, registry)
    return {"key": key, "dest": str(dest), "resolved_revision": resolved_revision}


def update_all_tracked_skills(repo_root: Path, github_loader=None) -> list[dict]:
    results = []
    for record in list_records(repo_root):
        results.append(update_tracked_skill(repo_root, record["key"], github_loader=github_loader))
    return results


def github_request(url: str, user_agent: str = "agent-skill-sync") -> bytes:
    headers = {"User-Agent": user_agent}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request) as response:
        return response.read()


def validate_relative_repo_path(path: str) -> None:
    normalized = os.path.normpath(path)
    if os.path.isabs(path) or normalized.startswith(".."):
        raise SourceError("GitHub skill path must stay inside the repo.")


def parse_github_repo_only_url(url: str, default_ref: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != "github.com":
        raise SourceError("Only GitHub URLs are supported for repo scans.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise SourceError("Invalid GitHub URL.")
    owner, repo = parts[0], parts[1]
    ref = default_ref
    if len(parts) > 3 and parts[2] == "tree":
        ref = parts[3]
    return f"{owner}/{repo}", ref


def parse_github_repo_url(url: str, default_ref: str) -> GithubSource:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != "github.com":
        raise SourceError("Only GitHub URLs are supported for repo installs.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise SourceError("Invalid GitHub URL.")
    owner, repo = parts[0], parts[1]
    ref = default_ref
    subpath = ""
    if len(parts) > 2:
        if parts[2] in {"tree", "blob"}:
            if len(parts) < 5:
                raise SourceError("GitHub URL missing ref or path.")
            ref = parts[3]
            subpath = "/".join(parts[4:])
        else:
            subpath = "/".join(parts[2:])
    if not subpath:
        raise SourceError("GitHub URL must include the path to a skill directory.")
    return GithubSource(repo=f"{owner}/{repo}", skill_path=subpath, ref=ref)


def resolve_github_ref(repo: str, ref: str) -> str:
    payload = github_request(f"https://api.github.com/repos/{repo}/commits/{ref}")
    data = json.loads(payload.decode("utf-8"))
    sha = data.get("sha")
    if not sha:
        raise SourceError(f"Unable to resolve commit for {repo}@{ref}")
    return str(sha)


def safe_extract_zip(zip_file: zipfile.ZipFile, dest_dir: Path) -> None:
    dest_root = dest_dir.resolve()
    for info in zip_file.infolist():
        resolved = (dest_dir / info.filename).resolve()
        if resolved == dest_root or str(resolved).startswith(str(dest_root) + os.sep):
            continue
        raise SourceError("Archive contains files outside the destination.")
    zip_file.extractall(dest_dir)


def run_git(args: list[str]) -> None:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise SourceError(result.stderr.strip() or "Git command failed.")


def download_repo_zip(repo: str, ref: str, dest_dir: Path) -> Path:
    owner, repo_name = repo.split("/", 1)
    zip_url = f"https://codeload.github.com/{owner}/{repo_name}/zip/{ref}"
    zip_path = dest_dir / "repo.zip"
    try:
        payload = github_request(zip_url)
    except urllib.error.HTTPError as exc:
        raise SourceError(f"Download failed: HTTP {exc.code}") from exc
    zip_path.write_bytes(payload)
    with zipfile.ZipFile(zip_path, "r") as zip_file:
        safe_extract_zip(zip_file, dest_dir)
        top_levels = {name.split("/")[0] for name in zip_file.namelist() if name}
    if len(top_levels) != 1:
        raise SourceError("Unexpected archive layout.")
    return dest_dir / next(iter(top_levels))


def git_sparse_checkout(repo: str, ref: str, skill_path: str, dest_dir: Path) -> Path:
    repo_dir = dest_dir / "repo"
    repo_url = f"https://github.com/{repo}.git"
    clone_cmd = [
        "git",
        "clone",
        "--filter=blob:none",
        "--depth",
        "1",
        "--sparse",
        "--single-branch",
        "--branch",
        ref,
        repo_url,
        str(repo_dir),
    ]
    try:
        run_git(clone_cmd)
    except SourceError:
        fallback = [
            "git",
            "clone",
            "--filter=blob:none",
            "--depth",
            "1",
            "--sparse",
            "--single-branch",
            f"git@github.com:{repo}.git",
            str(repo_dir),
        ]
        run_git(fallback)
        run_git(["git", "-C", str(repo_dir), "checkout", ref])
    run_git(["git", "-C", str(repo_dir), "sparse-checkout", "set", skill_path])
    run_git(["git", "-C", str(repo_dir), "checkout", ref])
    return repo_dir


def git_clone_repo(repo: str, ref: str, dest_dir: Path) -> Path:
    repo_dir = dest_dir / "repo"
    clone_cmd = [
        "git",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--branch",
        ref,
        f"https://github.com/{repo}.git",
        str(repo_dir),
    ]
    try:
        run_git(clone_cmd)
    except SourceError:
        fallback = [
            "git",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            f"git@github.com:{repo}.git",
            str(repo_dir),
        ]
        run_git(fallback)
        run_git(["git", "-C", str(repo_dir), "checkout", ref])
    return repo_dir


@contextmanager
def materialize_github_skill(source: GithubSource):
    validate_relative_repo_path(source.skill_path)
    with tempfile.TemporaryDirectory(prefix="agent-skill-sync-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        repo_root: Path
        if source.method in {"download", "auto"}:
            try:
                repo_root = download_repo_zip(source.repo, source.ref, tmp_root)
            except SourceError:
                if source.method == "download":
                    raise
                repo_root = git_sparse_checkout(source.repo, source.ref, source.skill_path, tmp_root)
        elif source.method == "git":
            repo_root = git_sparse_checkout(source.repo, source.ref, source.skill_path, tmp_root)
        else:
            raise SourceError(f"Unsupported fetch method: {source.method}")

        skill_dir = repo_root / source.skill_path
        ensure_skill_dir(skill_dir)
        resolved_revision = resolve_github_ref(source.repo, source.ref)
        yield skill_dir, resolved_revision


@contextmanager
def materialize_github_repo(repo: str, ref: str = DEFAULT_REF, method: str = "auto"):
    with tempfile.TemporaryDirectory(prefix="agent-skill-sync-scan-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        if method in {"download", "auto"}:
            try:
                repo_root = download_repo_zip(repo, ref, tmp_root)
            except SourceError:
                if method == "download":
                    raise
                repo_root = git_clone_repo(repo, ref, tmp_root)
        elif method == "git":
            repo_root = git_clone_repo(repo, ref, tmp_root)
        else:
            raise SourceError(f"Unsupported fetch method: {method}")
        resolved_revision = resolve_github_ref(repo, ref)
        yield repo_root, resolved_revision


def scan_materialized_repo(
    *,
    repo_root: Path,
    repo: str,
    ref: str,
    resolved_revision: str,
    include_unknown: bool = False,
) -> dict:
    groups: dict[str, list[dict]] = {"shared": [], "codex": [], "claude": [], "unknown": []}
    skills: list[dict] = []
    agents: list[dict] = []

    for skill_md in sorted(repo_root.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        relative = skill_dir.relative_to(repo_root).as_posix()
        classified = classify_repo_asset(relative, "SKILL.md")
        if classified["bucket"] == "unknown" and not include_unknown:
            continue
        item = {
            "name": classified["name"],
            "path": classified["path"],
            "bucket": classified["bucket"],
            "harness": classified["harness"],
            "asset_type": "skill",
            "install_strategy": classified["install_strategy"],
            "repo": repo,
            "ref": ref,
            "resolved_revision": resolved_revision,
        }
        skills.append(item)
        groups.setdefault(classified["bucket"], []).append(item)

    for base in (".claude/agents", ".codex/agents", "agents"):
        agent_root = repo_root / base
        if not agent_root.is_dir():
            continue
        for file_path in sorted(agent_root.rglob("*")):
            if not file_path.is_file() or file_path.name.startswith("."):
                continue
            relative = file_path.relative_to(repo_root).as_posix()
            classified = classify_repo_asset(relative, file_path.name)
            if classified["bucket"] == "unknown" and not include_unknown:
                continue
            agents.append(
                {
                    "name": classified["name"],
                    "path": classified["path"],
                    "bucket": classified["bucket"],
                    "harness": classified["harness"],
                    "asset_type": "agent",
                    "install_strategy": classified["install_strategy"],
                    "repo": repo,
                    "ref": ref,
                    "resolved_revision": resolved_revision,
                }
            )

    for bucket in groups:
        groups[bucket] = sorted(groups[bucket], key=lambda item: item["path"])
    agents = sorted(agents, key=lambda item: item["path"])

    install_plan = {
        "skills": {
            "shared": {
                "count": len(groups["shared"]),
                "items": [item["path"] for item in groups["shared"]],
            },
            "codex": {
                "count": len(groups["codex"]),
                "items": [item["path"] for item in groups["codex"]],
            },
            "claude": {
                "count": len(groups["claude"]),
                "items": [item["path"] for item in groups["claude"]],
            },
            "recognized_total": len(groups["shared"]) + len(groups["codex"]) + len(groups["claude"]),
        },
        "agents": {
            "manual_total": len(agents),
            "items": [item["path"] for item in agents],
        },
    }

    return {
        "repo": repo,
        "ref": ref,
        "resolved_revision": resolved_revision,
        "skills": sorted(skills, key=lambda item: item["path"]),
        "agents": agents,
        "groups": groups,
        "summary": {bucket: len(items) for bucket, items in groups.items()},
        "install_plan": install_plan,
    }


def scan_github_repo(
    *,
    repo: str | None = None,
    url: str | None = None,
    ref: str = DEFAULT_REF,
    method: str = "auto",
    include_unknown: bool = False,
) -> dict:
    repo_name = repo
    repo_ref = ref
    if url:
        repo_name, repo_ref = parse_github_repo_only_url(url, ref)
    if not repo_name:
        raise SourceError("scan-github requires --repo or --url.")
    with materialize_github_repo(repo_name, repo_ref, method=method) as (materialized_root, resolved_revision):
        return scan_materialized_repo(
            repo_root=materialized_root,
            repo=repo_name,
            ref=repo_ref,
            resolved_revision=resolved_revision,
            include_unknown=include_unknown,
        )


def load_github_source_for_record(record: dict) -> tuple[Path, str]:
    source = record["source"]
    github_source = GithubSource(
        repo=source["repo"],
        skill_path=source["path"],
        ref=source.get("ref", DEFAULT_REF),
        method=source.get("method", "auto"),
    )
    with materialize_github_skill(github_source) as (skill_dir, resolved_revision):
        temp_copy_root = Path(tempfile.mkdtemp(prefix="agent-skill-sync-update-"))
        copied = temp_copy_root / skill_dir.name
        shutil.copytree(skill_dir, copied)
        return copied, resolved_revision


def install_github_skill(
    *,
    repo_root: Path,
    bucket: str,
    repo: str | None = None,
    skill_path: str | None = None,
    url: str | None = None,
    ref: str = DEFAULT_REF,
    name: str | None = None,
    scope: str = "repo",
    method: str = "auto",
) -> dict:
    if url:
        source = parse_github_repo_url(url, ref)
        source.method = method
    else:
        if not repo or not skill_path:
            raise SourceError("install-github requires --repo and --path, or --url.")
        source = GithubSource(repo=repo, skill_path=skill_path, ref=ref, method=method)

    with materialize_github_skill(source) as (source_dir, resolved_revision):
        return install_materialized_github_skill(
            repo_root=repo_root,
            bucket=bucket,
            source_dir=source_dir,
            repo=source.repo,
            skill_path=source.skill_path,
            ref=source.ref,
            resolved_revision=resolved_revision,
            name=name,
            scope=scope,
        )


def normalize_batch_selections(selections: list[str] | None) -> set[str]:
    if not selections:
        return {"recognized"}
    normalized = {item.lower() for item in selections}
    valid = {"recognized", "shared", "codex", "claude"}
    invalid = sorted(item for item in normalized if item not in valid)
    if invalid:
        raise SourceError(f"Unsupported batch selection(s): {', '.join(invalid)}")
    return normalized


def selected_scan_items(scan: dict, selections: list[str] | None = None) -> list[dict]:
    selected = normalize_batch_selections(selections)
    buckets: set[str] = set()
    if "recognized" in selected:
        buckets.update({"shared", "codex", "claude"})
    buckets.update(item for item in selected if item in {"shared", "codex", "claude"})

    items: list[dict] = []
    seen_paths: set[str] = set()
    for bucket in ("shared", "codex", "claude"):
        if bucket not in buckets:
            continue
        for item in scan["groups"].get(bucket, []):
            if item["path"] in seen_paths:
                continue
            seen_paths.add(item["path"])
            items.append(item)
    return items


def install_scanned_skills(
    *,
    repo_root: Path,
    scan: dict,
    selections: list[str] | None,
    source_repo_root: Path,
    scope: str = "repo",
) -> dict:
    items = selected_scan_items(scan, selections)
    results: list[dict] = []
    installed_total = 0
    skipped_total = 0

    for item in items:
        source_dir = source_repo_root / item["path"]
        key = registry_key(item["bucket"], item["name"])
        dest = skill_dest(repo_root, item["bucket"], item["name"])
        try:
            ensure_key_available(repo_root, key)
            if dest.exists():
                raise SourceError(f"Destination already exists in repo: {dest}")
            install_materialized_github_skill(
                repo_root=repo_root,
                bucket=item["bucket"],
                source_dir=source_dir,
                repo=item["repo"],
                skill_path=item["path"],
                ref=item["ref"],
                resolved_revision=item["resolved_revision"],
                name=item["name"],
                scope=scope,
            )
            results.append(
                {
                    "status": "installed",
                    "key": key,
                    "path": item["path"],
                    "bucket": item["bucket"],
                }
            )
            installed_total += 1
        except SourceError as exc:
            results.append(
                {
                    "status": "skipped",
                    "key": key,
                    "path": item["path"],
                    "bucket": item["bucket"],
                    "reason": str(exc),
                }
            )
            skipped_total += 1

    return {
        "installed_total": installed_total,
        "skipped_total": skipped_total,
        "results": results,
    }


def copy_file(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def agent_dest(repo_root: Path, bucket: str, relative_path: str) -> Path:
    normalized = relative_path.replace("\\", "/").strip("/")
    if bucket == "codex":
        return repo_root / ".codex" / "agents" / Path(normalized).name
    if bucket == "claude":
        return repo_root / ".claude" / "agents" / Path(normalized).name
    return repo_root / "agents" / Path(normalized).name


def install_scanned_agents(
    *,
    repo_root: Path,
    scan: dict,
    source_repo_root: Path,
    copy_agents: bool = False,
) -> dict:
    results: list[dict] = []
    installed_total = 0
    skipped_total = 0

    for item in scan.get("agents", []):
        dest = agent_dest(repo_root, item["bucket"], item["path"])
        if not copy_agents:
            results.append(
                {
                    "status": "skipped",
                    "name": item["name"],
                    "path": item["path"],
                    "reason": "agent copy is opt-in",
                }
            )
            skipped_total += 1
            continue
        if dest.exists():
            results.append(
                {
                    "status": "skipped",
                    "name": item["name"],
                    "path": item["path"],
                    "reason": f"destination already exists: {dest}",
                }
            )
            skipped_total += 1
            continue
        copy_file(source_repo_root / item["path"], dest)
        results.append({"status": "installed", "name": item["name"], "path": item["path"], "dest": str(dest)})
        installed_total += 1

    return {
        "installed_total": installed_total,
        "skipped_total": skipped_total,
        "results": results,
    }


def codex_agent_file(repo_root: Path, name: str) -> Path:
    return repo_root / ".codex" / "agents" / f"{name}.toml"


def codex_config_path(repo_root: Path) -> Path:
    return repo_root / ".codex" / "config.toml"


def codex_config_backup_path(repo_root: Path) -> Path:
    return repo_root / ".codex" / "config.toml.agent-skill-sync.bak"


def expected_codex_agent_config_line(name: str) -> str:
    return f'path = ".codex/agents/{name}.toml"'


def render_codex_agent_block(agent_names: list[str]) -> str:
    lines = [MANAGED_AGENTS_BEGIN]
    for index, name in enumerate(agent_names):
        if index:
            lines.append("")
        lines.append(f"[agents.{name}]")
        lines.append(expected_codex_agent_config_line(name))
    lines.append(MANAGED_AGENTS_END)
    return "\n".join(lines)


def split_managed_codex_agent_block(content: str) -> tuple[str, str, str]:
    start = content.find(MANAGED_AGENTS_BEGIN)
    end = content.find(MANAGED_AGENTS_END)
    if start == -1 and end == -1:
        return content, "", ""
    if start == -1 or end == -1 or end < start:
        raise SourceError("Invalid managed Codex agent block markers in .codex/config.toml")
    end += len(MANAGED_AGENTS_END)
    return content[:start], content[start:end], content[end:]


def managed_codex_agent_names(block: str) -> list[str]:
    pattern = re.compile(r"(?m)^\[agents\.([^\]]+)\]\s*$")
    return [match.group(1) for match in pattern.finditer(block)]


def find_codex_agent_section(content: str, name: str) -> tuple[int, int, str] | None:
    pattern = re.compile(
        rf"(?ms)^[ \t]*\[agents\.{re.escape(name)}\][ \t]*\n.*?(?=^[ \t]*\[|\Z)"
    )
    match = pattern.search(content)
    if not match:
        return None
    return match.start(), match.end(), match.group(0)


def is_exact_managed_codex_agent_section(section: str, name: str) -> bool:
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    return lines == [f"[agents.{name}]", expected_codex_agent_config_line(name)]


def merge_agent_names(existing_names: list[str], requested_names: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for name in existing_names + requested_names:
        if name in seen:
            continue
        seen.add(name)
        merged.append(name)
    return merged


def find_agent_section_in_content(content: str, name: str) -> tuple[str, str] | None:
    section_match = find_codex_agent_section(content, name)
    if not section_match:
        return None
    section_start, section_end, section = section_match
    updated = content[:section_start] + content[section_end:]
    return updated, section


def normalize_content_edges(prefix: str, suffix: str) -> tuple[str, str]:
    prefix_rendered = prefix.rstrip()
    suffix_rendered = suffix.lstrip()
    return prefix_rendered, suffix_rendered


def backup_file(source: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)


def register_codex_agents(*, repo_root: Path, agent_names: list[str]) -> dict:
    config_path = codex_config_path(repo_root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    prefix, managed_block, suffix = split_managed_codex_agent_block(existing)
    existing_managed = managed_codex_agent_names(managed_block)
    registered: list[str] = []
    skipped: list[dict] = []
    requested_managed: list[str] = []
    seen_names: set[str] = set()

    for name in agent_names:
        if name in seen_names:
            continue
        seen_names.add(name)
        agent_path = codex_agent_file(repo_root, name)
        if not agent_path.is_file():
            skipped.append({"name": name, "reason": f"missing agent file: {agent_path}"})
            continue

        prefix_match = find_agent_section_in_content(prefix, name)
        if prefix_match:
            updated_prefix, section = prefix_match
            if not is_exact_managed_codex_agent_section(section, name):
                skipped.append({"name": name, "reason": "existing unmanaged agent config"})
                continue
            prefix = updated_prefix
        else:
            suffix_match = find_agent_section_in_content(suffix, name)
            if suffix_match:
                updated_suffix, section = suffix_match
                if not is_exact_managed_codex_agent_section(section, name):
                    skipped.append({"name": name, "reason": "existing unmanaged agent config"})
                    continue
                suffix = updated_suffix

        registered.append(name)
        requested_managed.append(name)

    final_managed = merge_agent_names(existing_managed, requested_managed)
    parts: list[str] = []
    prefix_rendered, suffix_rendered = normalize_content_edges(prefix, suffix)
    if prefix_rendered:
        parts.append(prefix_rendered)
    if final_managed:
        parts.append(render_codex_agent_block(final_managed))
    if suffix_rendered:
        parts.append(suffix_rendered)
    new_content = "\n\n".join(parts)
    if new_content:
        new_content += "\n"
    backup_path: Path | None = None
    if new_content != existing:
        if config_path.exists():
            backup_path = codex_config_backup_path(repo_root)
            backup_file(config_path, backup_path)
        config_path.write_text(new_content, encoding="utf-8")

    return {
        "registered": registered,
        "skipped": skipped,
        "config": str(config_path),
        "backup": str(backup_path) if backup_path else None,
    }


def print_records(records: list[dict]) -> None:
    if not records:
        print("No tracked skill sources.")
        return
    for record in records:
        line = (
            f"{record['key']} [{record['scope']}] {record['source_type']} -> {record['dest']} "
            f"(rev {record['resolved_revision']})"
        )
        if record.get("deployments"):
            deployment_bits = [
                f"{target_id}={target['status']}"
                for target_id, target in sorted(record["deployments"].items())
            ]
            line += " [" + ", ".join(deployment_bits) + "]"
        print(line)


def print_scan(scan: dict, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(scan, indent=2, sort_keys=True))
        return

    print(f"Repo: {scan['repo']} @ {scan['resolved_revision']}")
    print("install plan:")
    for bucket in ("claude", "codex", "shared"):
        count = scan["install_plan"]["skills"][bucket]["count"]
        if count:
            print(f"  {bucket}: {count} skill(s)")
    if scan["install_plan"]["agents"]["manual_total"]:
        print(f"  agents: {scan['install_plan']['agents']['manual_total']} manual item(s)")

    for bucket in ("claude", "codex", "shared", "unknown"):
        items = scan["groups"].get(bucket, [])
        if not items:
            continue
        print(f"{bucket}:")
        for item in items:
            print(f"  - {item['path']}")
    if scan["agents"]:
        print("agents:")
        for item in scan["agents"]:
            print(f"  - {item['path']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install and update tracked skill sources in this repo.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List tracked skill sources.")
    list_parser.add_argument("--scope", choices=["repo", "local", "all"], default="all")

    scan_parser = subparsers.add_parser("scan-github", help="Scan a GitHub repo for installable skills.")
    scan_parser.add_argument("--repo", help="GitHub repo in owner/repo form.")
    scan_parser.add_argument("--url", help="GitHub repo URL.")
    scan_parser.add_argument("--ref", default=DEFAULT_REF)
    scan_parser.add_argument("--method", choices=["auto", "download", "git"], default="auto")
    scan_parser.add_argument("--format", choices=["text", "json"], default="text")
    scan_parser.add_argument("--include-unknown", action="store_true")

    install_batch_parser = subparsers.add_parser(
        "install-github-batch",
        help="Install all recognized skills from selected GitHub scan groups.",
    )
    install_batch_parser.add_argument("--repo", help="GitHub repo in owner/repo form.")
    install_batch_parser.add_argument("--url", help="GitHub repo URL.")
    install_batch_parser.add_argument("--ref", default=DEFAULT_REF)
    install_batch_parser.add_argument("--method", choices=["auto", "download", "git"], default="auto")
    install_batch_parser.add_argument(
        "--select",
        action="append",
        dest="selections",
        choices=["recognized", "shared", "codex", "claude"],
        help="Batch selection group. Defaults to recognized.",
    )
    install_batch_parser.add_argument("--scope", choices=sorted(VALID_SCOPES), default="repo")
    install_batch_parser.add_argument("--copy-agents", action="store_true")
    install_batch_parser.add_argument("--register-codex-agents", action="store_true")

    install_github_parser = subparsers.add_parser("install-github", help="Install a tracked skill from GitHub.")
    install_github_parser.add_argument("--bucket", choices=sorted(VALID_BUCKETS), required=True)
    install_github_parser.add_argument("--repo", help="GitHub repo in owner/repo form.")
    install_github_parser.add_argument("--path", help="Relative path to the skill inside the repo.")
    install_github_parser.add_argument("--url", help="GitHub URL to the skill directory.")
    install_github_parser.add_argument("--ref", default=DEFAULT_REF)
    install_github_parser.add_argument("--name")
    install_github_parser.add_argument("--scope", choices=sorted(VALID_SCOPES), default="repo")
    install_github_parser.add_argument("--method", choices=["auto", "download", "git"], default="auto")

    install_plugin_parser = subparsers.add_parser("install-plugin", help="Install a tracked skill from a local path.")
    install_plugin_parser.add_argument("--bucket", choices=sorted(VALID_BUCKETS), required=True)
    install_plugin_parser.add_argument("--path", required=True, help="Local path to a skill directory.")
    install_plugin_parser.add_argument("--name")
    install_plugin_parser.add_argument("--scope", choices=sorted(VALID_SCOPES), default="local")

    update_parser = subparsers.add_parser("update", help="Update one tracked skill from its recorded source.")
    update_parser.add_argument("--key", required=True, help="Tracked skill key in bucket/name form.")

    update_all_parser = subparsers.add_parser("update-all", help="Update all tracked skills.")
    update_all_parser.add_argument("--scope", choices=["repo", "local", "all"], default="all")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = detect_repo_root()

    try:
        if args.command == "list":
            records = list_records(repo_root)
            if args.scope != "all":
                records = [record for record in records if record["scope"] == args.scope]
            print_records(records)
            return 0

        if args.command == "scan-github":
            scan = scan_github_repo(
                repo=args.repo,
                url=args.url,
                ref=args.ref,
                method=args.method,
                include_unknown=args.include_unknown,
            )
            print_scan(scan, args.format)
            return 0

        if args.command == "install-github-batch":
            scan = scan_github_repo(
                repo=args.repo,
                url=args.url,
                ref=args.ref,
                method=args.method,
            )
            repo_name = scan["repo"]
            repo_ref = scan["ref"]
            with materialize_github_repo(repo_name, repo_ref, method=args.method) as (source_repo_root, resolved_revision):
                scan["resolved_revision"] = resolved_revision
                for collection in ("skills", "agents"):
                    for item in scan.get(collection, []):
                        item["resolved_revision"] = resolved_revision
                for group_items in scan.get("groups", {}).values():
                    for item in group_items:
                        item["resolved_revision"] = resolved_revision
                result = install_scanned_skills(
                    repo_root=repo_root,
                    scan=scan,
                    selections=args.selections,
                    source_repo_root=source_repo_root,
                    scope=args.scope,
                )
                agent_result = install_scanned_agents(
                    repo_root=repo_root,
                    scan=scan,
                    source_repo_root=source_repo_root,
                    copy_agents=args.copy_agents,
                )
            codex_agent_names = [
                item["name"]
                for item in scan.get("agents", [])
                if item["bucket"] == "codex"
            ]
            register_result = {"registered": [], "skipped": []}
            if args.register_codex_agents:
                register_result = register_codex_agents(
                    repo_root=repo_root,
                    agent_names=codex_agent_names,
                )
            print(
                f"Installed {result['installed_total']} skill(s); skipped {result['skipped_total']}."
            )
            for item in result["results"]:
                if item["status"] == "installed":
                    print(f"  + {item['key']} <- {item['path']}")
                else:
                    print(f"  ~ {item['key']} ({item['reason']})")
            print(
                f"Agent copy: installed {agent_result['installed_total']}; skipped {agent_result['skipped_total']}."
            )
            if args.register_codex_agents:
                print(
                    f"Codex agent registration: registered {len(register_result['registered'])}; "
                    f"skipped {len(register_result['skipped'])}."
                )
                if register_result.get("backup"):
                    print(f"  config backup: {register_result['backup']}")
            else:
                print("Codex agent registration was skipped (opt-in).")
            print("Run scripts/sync_skills.py --check before deploying outward.")
            return 0

        if args.command == "install-github":
            result = install_github_skill(
                repo_root=repo_root,
                bucket=args.bucket,
                repo=args.repo,
                skill_path=args.path,
                url=args.url,
                ref=args.ref,
                name=args.name,
                scope=args.scope,
                method=args.method,
            )
            print(f"Installed {result['key']} into {result['dest']}")
            print("Run scripts/sync_skills.py --check before deploying outward.")
            return 0

        if args.command == "install-plugin":
            result = install_plugin_skill(
                repo_root=repo_root,
                bucket=args.bucket,
                plugin_path=Path(args.path),
                name=args.name,
                scope=args.scope,
            )
            print(f"Installed {result['key']} into {result['dest']}")
            print("Run scripts/sync_skills.py --check before deploying outward.")
            return 0

        if args.command == "update":
            result = update_tracked_skill(repo_root, args.key)
            print(f"Updated {result['key']} -> {result['resolved_revision']}")
            print("Run scripts/sync_skills.py --check before deploying outward.")
            return 0

        if args.command == "update-all":
            records = list_records(repo_root)
            if args.scope != "all":
                records = [record for record in records if record["scope"] == args.scope]
            results = [update_tracked_skill(repo_root, record["key"]) for record in records]
            if not results:
                print("No tracked skills matched.")
                return 0
            for result in results:
                print(f"Updated {result['key']} -> {result['resolved_revision']}")
            print("Run scripts/sync_skills.py --check before deploying outward.")
            return 0
    except SourceError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"Unsupported command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
