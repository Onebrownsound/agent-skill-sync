"""Tracked repo management: clone, pull, enumerate, distribute, symlink, state tracking."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path


TRACKED_REPOS_STATE_FILENAME = "tracked-repos-state.local.json"


def cache_dir_for_source(name: str, cache_root: Path) -> Path:
    return cache_root / name


def load_state(state_path: Path) -> dict:
    if state_path.is_file():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {"version": 1, "sources": {}}


def save_state(state_path: Path, state: dict) -> None:
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clone_or_pull(
    repo_url: str,
    ref: str,
    dest: Path,
    *,
    runner=subprocess.run,
) -> tuple[str, str]:
    """Clone if missing, pull if exists. Returns (action, commit_hash)."""
    if (dest / ".git").is_dir():
        runner(["git", "-C", str(dest), "fetch", "origin", ref], check=True, capture_output=True)
        runner(["git", "-C", str(dest), "checkout", f"origin/{ref}"], check=True, capture_output=True)
        action = "pulled"
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        runner(
            ["git", "clone", "--branch", ref, "--single-branch", repo_url, str(dest)],
            check=True,
            capture_output=True,
        )
        action = "cloned"
    result = runner(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return action, result.stdout.strip()


def current_commit(repo_root: Path, *, runner=subprocess.run) -> str:
    result = runner(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def enumerate_skills(repo_root: Path, skill_map: dict[str, dict]) -> dict[str, Path]:
    """Return {skill_name: path_to_SKILL.md} for entries that exist."""
    found: dict[str, Path] = {}
    for skill_name, entry in skill_map.items():
        source_path = entry.get("source_path", ".")
        if source_path == ".":
            skill_md = repo_root / "SKILL.md"
        else:
            skill_md = repo_root / source_path / "SKILL.md"
        if skill_md.is_file():
            found[skill_name] = skill_md
    return found


def distribute_flat_copies(
    skills: dict[str, Path],
    target_root: Path,
) -> tuple[int, int]:
    """Copy SKILL.md files to target as flat skill directories. Returns (updated, skipped)."""
    updated = 0
    skipped = 0
    for skill_name, source_md in skills.items():
        dest_dir = target_root / skill_name
        dest_md = dest_dir / "SKILL.md"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source_md), str(dest_md))
            updated += 1
        except OSError:
            skipped += 1
    return updated, skipped


def create_skill_symlinks(
    source_name: str,
    skill_map: dict[str, dict],
    target_root: Path,
) -> tuple[int, int]:
    """Create symlinks from target_root/skill-name -> target_root/source_name/sub_path.

    Skips the root skill (source_path=".") since the clone itself IS the root skill.
    Returns (created, skipped).
    """
    created = 0
    skipped = 0
    clone_dir = target_root / source_name
    for skill_name, entry in skill_map.items():
        source_path = entry.get("source_path", ".")
        if source_path == ".":
            continue
        link_path = target_root / skill_name
        real_target = clone_dir / source_path
        if not real_target.is_dir():
            skipped += 1
            continue
        if link_path.is_symlink():
            if link_path.resolve() == real_target.resolve():
                skipped += 1
                continue
            link_path.unlink()
        elif link_path.exists():
            skipped += 1
            continue
        link_path.symlink_to(real_target)
        created += 1
    return created, skipped


def plan_skill_symlinks(
    source_name: str,
    skill_map: dict[str, dict],
    target_root: Path,
) -> tuple[int, int]:
    """Report the symlink changes a clone target would make without mutating it."""
    created = 0
    skipped = 0
    clone_dir = target_root / source_name
    for skill_name, entry in skill_map.items():
        source_path = entry.get("source_path", ".")
        if source_path == ".":
            continue
        link_path = target_root / skill_name
        real_target = clone_dir / source_path
        if not real_target.is_dir():
            skipped += 1
            continue
        if link_path.is_symlink():
            try:
                if link_path.resolve() == real_target.resolve():
                    skipped += 1
                    continue
            except OSError:
                pass
        elif link_path.exists():
            skipped += 1
            continue
        created += 1
    return created, skipped


def planned_flat_copy_counts(skills: dict[str, Path], target_root: Path) -> tuple[int, int]:
    """Estimate flat-copy work without mutating the target."""
    updated = 0
    skipped = 0
    for skill_name in skills:
        dest_dir = target_root / skill_name
        if dest_dir.exists() and not dest_dir.is_dir():
            skipped += 1
            continue
        updated += 1
    return updated, skipped


def update_tracked_repo(
    source_name: str,
    source_cfg: dict,
    all_targets: dict,
    cache_root: Path,
    *,
    runner=subprocess.run,
    resolve_target_path=None,
    state: dict | None = None,
    target_ids: set[str] | None = None,
    allowed_hosts: set[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Update a single tracked repo across all its targets.

    If state is provided, records deployed SHA per target and skips flat_copy
    targets that are already at the current commit.

    Returns summary dict with per-target results.
    """
    repo_url = source_cfg["repo"]
    ref = source_cfg.get("ref", "main")
    skill_map = source_cfg.get("skill_map", {})
    source_targets = source_cfg.get("targets", {})

    source_state = {}
    if state is not None:
        source_state = state.get("sources", {}).get(source_name, {})

    cache_dir = cache_dir_for_source(source_name, cache_root)
    temp_repo: tempfile.TemporaryDirectory[str] | None = None
    if dry_run and cache_dir.exists():
        action = "cached"
        commit = current_commit(cache_dir, runner=runner)
        repo_view = cache_dir
    elif dry_run:
        temp_repo = tempfile.TemporaryDirectory()
        repo_view = Path(temp_repo.name) / source_name
        action, commit = clone_or_pull(repo_url, ref, repo_view, runner=runner)
    else:
        action, commit = clone_or_pull(repo_url, ref, cache_dir, runner=runner)
        repo_view = cache_dir
    skills = enumerate_skills(repo_view, skill_map)

    results: dict = {
        "source": source_name,
        "action": action,
        "commit": commit,
        "skills_found": len(skills),
        "skill_names": sorted(skills.keys()),
        "targets": {},
    }

    now = datetime.now().isoformat()
    new_source_state: dict = {
        "repo": repo_url,
        "ref": ref,
        "commit": commit,
        "updated_at": now,
        "targets": {},
    }

    for target_id, mode in source_targets.items():
        if target_ids is not None and target_id not in target_ids:
            continue
        target_cfg = all_targets.get(target_id)
        if not target_cfg or not target_cfg.get("enabled", False):
            results["targets"][target_id] = {"status": "skipped", "reason": "disabled"}
            continue
        if allowed_hosts is not None and target_cfg.get("host") not in allowed_hosts:
            continue

        if resolve_target_path:
            target_path = resolve_target_path(target_cfg)
        else:
            target_path = Path(target_cfg["path"])

        prev_target_state = source_state.get("targets", {}).get(target_id, {})
        prev_commit = prev_target_state.get("commit")

        if mode == "clone":
            dest = target_path / source_name
            try:
                if dry_run:
                    clone_action = "pulled" if (dest / ".git").is_dir() else "cloned"
                    clone_commit = commit
                    sym_created, sym_skipped = plan_skill_symlinks(source_name, skill_map, target_path)
                    status = "planned"
                else:
                    clone_action, clone_commit = clone_or_pull(repo_url, ref, dest, runner=runner)
                    sym_created, sym_skipped = create_skill_symlinks(source_name, skill_map, target_path)
                    status = "ok"
                results["targets"][target_id] = {
                    "status": status,
                    "mode": "clone",
                    "action": clone_action,
                    "commit": clone_commit,
                    "symlinks_created": sym_created,
                    "symlinks_skipped": sym_skipped,
                }
                if not dry_run:
                    new_source_state["targets"][target_id] = {
                        "mode": "clone",
                        "commit": clone_commit,
                        "deployed_at": now,
                    }
            except subprocess.CalledProcessError as e:
                results["targets"][target_id] = {"status": "error", "mode": "clone", "error": str(e)}

        elif mode == "flat_copy":
            if prev_commit == commit:
                results["targets"][target_id] = {
                    "status": "up_to_date",
                    "mode": "flat_copy",
                    "commit": commit,
                }
                if not dry_run:
                    new_source_state["targets"][target_id] = prev_target_state
                continue

            if dry_run:
                updated_count, skipped_count = planned_flat_copy_counts(skills, target_path)
                status = "planned"
            else:
                updated_count, skipped_count = distribute_flat_copies(skills, target_path)
                status = "ok"
            results["targets"][target_id] = {
                "status": status,
                "mode": "flat_copy",
                "updated": updated_count,
                "skipped": skipped_count,
                "commit": commit,
                "previous_commit": prev_commit,
            }
            if not dry_run:
                new_source_state["targets"][target_id] = {
                    "mode": "flat_copy",
                    "commit": commit,
                    "deployed_at": now,
                }

    if state is not None and not dry_run:
        if "sources" not in state:
            state["sources"] = {}
        state["sources"][source_name] = new_source_state

    if temp_repo is not None:
        temp_repo.cleanup()

    return results
