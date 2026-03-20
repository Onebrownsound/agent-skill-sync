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
    for scope in ("repo", "local"):
        registry = load_registry(registry_path(repo_root, scope))
        for key, record in registry["skills"].items():
            item = dict(record)
            item["key"] = key
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


def print_records(records: list[dict]) -> None:
    if not records:
        print("No tracked skill sources.")
        return
    for record in records:
        print(
            f"{record['key']} [{record['scope']}] {record['source_type']} -> {record['dest']} "
            f"(rev {record['resolved_revision']})"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install and update tracked skill sources in this repo.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List tracked skill sources.")
    list_parser.add_argument("--scope", choices=["repo", "local", "all"], default="all")

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
