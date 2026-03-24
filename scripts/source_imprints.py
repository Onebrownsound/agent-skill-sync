from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


SOURCE_ROOT_DIRNAME = "sources"
SOURCE_METADATA_FILENAME = "source.json"


class SourceImprintError(Exception):
    pass


def sources_root(repo_root: Path) -> Path:
    return repo_root / SOURCE_ROOT_DIRNAME


def normalize_source_id(source_id: str) -> str:
    return source_id.replace("\\", "__").replace("/", "__")


def source_root(repo_root: Path, source_id: str) -> Path:
    return sources_root(repo_root) / normalize_source_id(source_id)


def imprint_root(repo_root: Path, source_id: str) -> Path:
    return source_root(repo_root, source_id) / "imprint"


def overlays_root(repo_root: Path, source_id: str) -> Path:
    return source_root(repo_root, source_id) / "overlays"


def metadata_path(repo_root: Path, source_id: str) -> Path:
    return source_root(repo_root, source_id) / SOURCE_METADATA_FILENAME


def ensure_source_layout(repo_root: Path, source_id: str) -> tuple[Path, Path]:
    imprint = imprint_root(repo_root, source_id)
    overlays = overlays_root(repo_root, source_id)
    imprint.parent.mkdir(parents=True, exist_ok=True)
    imprint.mkdir(parents=True, exist_ok=True)
    overlays.mkdir(parents=True, exist_ok=True)
    return imprint, overlays


def load_source_metadata(repo_root: Path, source_id: str) -> dict:
    path = metadata_path(repo_root, source_id)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_source_metadata(repo_root: Path, source_id: str, payload: dict) -> None:
    path = metadata_path(repo_root, source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _copy_tree(source: Path, dest: Path, *, ignore_names: set[str] | None = None) -> None:
    if not source.is_dir():
        raise SourceImprintError(f"Source tree not found: {source}")
    ignore_names = ignore_names or set()
    if dest.exists():
        _remove_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        dest,
        ignore=shutil.ignore_patterns(*sorted(ignore_names)) if ignore_names else None,
    )


def refresh_imprint_tree(
    *,
    repo_root: Path,
    source_id: str,
    source_tree: Path,
    ignore_names: set[str] | None = None,
) -> Path:
    imprint = imprint_root(repo_root, source_id)
    overlays = overlays_root(repo_root, source_id)
    imprint.parent.mkdir(parents=True, exist_ok=True)
    overlays.mkdir(parents=True, exist_ok=True)
    _copy_tree(source_tree, imprint, ignore_names=ignore_names)
    return imprint


def _apply_overlay_files(overlay_tree: Path, dest_tree: Path) -> None:
    if not overlay_tree.is_dir():
        return
    for path in sorted(overlay_tree.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(overlay_tree)
        target = dest_tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def tree_snapshot(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
    return snapshot


@contextmanager
def staged_materialization(
    *,
    imprint_tree: Path,
    overlay_tree: Path | None = None,
    staged_name: str = "materialized",
):
    if not imprint_tree.is_dir():
        raise SourceImprintError(f"Imprint tree not found: {imprint_tree}")
    temp_root = Path(tempfile.mkdtemp(prefix="agent-skill-sync-materialize-"))
    staged = temp_root / staged_name
    try:
        shutil.copytree(imprint_tree, staged)
        if overlay_tree is not None:
            _apply_overlay_files(overlay_tree, staged)
        if not (staged / "SKILL.md").is_file():
            raise SourceImprintError(f"Materialized skill missing SKILL.md: {staged}")
        yield staged
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def materialize_skill(
    *,
    imprint_tree: Path,
    overlay_tree: Path | None,
    dest: Path,
) -> None:
    with staged_materialization(
        imprint_tree=imprint_tree,
        overlay_tree=overlay_tree,
        staged_name=dest.name,
    ) as staged:
        if dest.exists():
            _remove_path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(dest))


def materialization_status(
    *,
    imprint_tree: Path,
    overlay_tree: Path | None,
    dest: Path,
) -> str:
    if not dest.exists():
        return "add"
    with staged_materialization(
        imprint_tree=imprint_tree,
        overlay_tree=overlay_tree,
        staged_name=dest.name,
    ) as staged:
        return "unchanged" if tree_snapshot(staged) == tree_snapshot(dest) else "update"
