from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path
import re
import shutil


MANAGED_AGENTS_BEGIN = "# BEGIN agent-skill-sync managed agents"
MANAGED_AGENTS_END = "# END agent-skill-sync managed agents"


def iter_agent_files(root: Path) -> dict[str, Path]:
    agents: dict[str, Path] = {}
    if not root.is_dir():
        return agents
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name in agents:
            raise ValueError(f"Duplicate agent filename under {root}: {path.name}")
        agents[path.name] = path
    return agents


def target_base_dir(target_root: Path) -> Path:
    return target_root.parent


def target_agent_root(target_root: Path) -> Path:
    return target_base_dir(target_root) / "agents"


def target_codex_config_path(target_root: Path) -> Path:
    return target_base_dir(target_root) / "config.toml"


def codex_agent_name(filename: str) -> str:
    return Path(filename).stem


def collect_source_agents(repo_root: Path, kind: str) -> dict[str, Path]:
    if kind not in ("codex", "claude"):
        return {}
    return iter_agent_files(repo_root / f".{kind}" / "agents")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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
        raise ValueError("Invalid managed Codex agent block markers in target config.toml")
    end += len(MANAGED_AGENTS_END)
    return content[:start], content[start:end], content[end:]


def find_codex_agent_section(content: str, name: str) -> tuple[int, int, str] | None:
    pattern = re.compile(rf"(?ms)^[ \t]*\[agents\.{re.escape(name)}\][ \t]*\n.*?(?=^[ \t]*\[|\Z)")
    match = pattern.search(content)
    if not match:
        return None
    return match.start(), match.end(), match.group(0)


def is_exact_managed_codex_agent_section(section: str, name: str) -> bool:
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    return lines == [f"[agents.{name}]", expected_codex_agent_config_line(name)]


def is_partially_managed_codex_agent_section(section: str, name: str) -> bool:
    if is_exact_managed_codex_agent_section(section, name):
        return False
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    return expected_codex_agent_config_line(name) in lines and any(
        line != f"[agents.{name}]" for line in lines if line != expected_codex_agent_config_line(name)
    )


def normalize_content_edges(prefix: str, suffix: str) -> tuple[str, str]:
    return prefix.rstrip(), suffix.lstrip()


def render_target_codex_config(config_path: Path, desired_agent_names: list[str]) -> dict:
    existing = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    prefix, _managed_block, suffix = split_managed_codex_agent_block(existing)
    registered: list[str] = []
    skipped: list[dict] = []
    seen_names: set[str] = set()

    for name in desired_agent_names:
        if name in seen_names:
            continue
        seen_names.add(name)

        prefix_match = find_codex_agent_section(prefix, name)
        if prefix_match:
            section_start, section_end, section = prefix_match
            if not is_exact_managed_codex_agent_section(section, name):
                reason = "existing unmanaged agent config"
                if is_partially_managed_codex_agent_section(section, name):
                    reason = "existing partially managed agent config"
                skipped.append({"name": name, "reason": reason})
                continue
            prefix = prefix[:section_start] + prefix[section_end:]
        else:
            suffix_match = find_codex_agent_section(suffix, name)
            if suffix_match:
                section_start, section_end, section = suffix_match
                if not is_exact_managed_codex_agent_section(section, name):
                    reason = "existing unmanaged agent config"
                    if is_partially_managed_codex_agent_section(section, name):
                        reason = "existing partially managed agent config"
                    skipped.append({"name": name, "reason": reason})
                    continue
                suffix = suffix[:section_start] + suffix[section_end:]

        registered.append(name)

    prefix_rendered, suffix_rendered = normalize_content_edges(prefix, suffix)
    parts: list[str] = []
    if prefix_rendered:
        parts.append(prefix_rendered)
    if registered:
        parts.append(render_codex_agent_block(registered))
    if suffix_rendered:
        parts.append(suffix_rendered)
    new_content = "\n\n".join(parts)
    if new_content:
        new_content += "\n"

    return {
        "config_path": str(config_path),
        "previous_exists": config_path.is_file(),
        "update_needed": new_content != existing,
        "new_content": new_content,
        "registered": registered,
        "skipped": skipped,
    }


def plan_agent_sync(repo_root: Path, kind: str, target_root: Path, managed_agents: list[str]) -> dict:
    source_agents = collect_source_agents(repo_root, kind)
    desired_agents = sorted(source_agents)
    agent_root = target_agent_root(target_root)

    agent_add: list[str] = []
    agent_update: list[str] = []
    agent_unchanged: list[str] = []
    for name, source_path in source_agents.items():
        dest_path = agent_root / name
        if not dest_path.exists():
            agent_add.append(name)
            continue
        if file_hash(source_path) != file_hash(dest_path):
            agent_update.append(name)
        else:
            agent_unchanged.append(name)

    agent_remove = sorted(name for name in managed_agents if name not in desired_agents)
    codex_config = None
    if kind == "codex":
        codex_config = render_target_codex_config(
            target_codex_config_path(target_root),
            [codex_agent_name(name) for name in desired_agents],
        )

    return {
        "desired_agents": desired_agents,
        "agent_root": str(agent_root),
        "agent_add": sorted(agent_add),
        "agent_update": sorted(agent_update),
        "agent_unchanged": sorted(agent_unchanged),
        "agent_remove": agent_remove,
        "source_agents": {name: str(path) for name, path in source_agents.items()},
        "codex_config": codex_config,
    }


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


def backup_path_entry(dest: Path, backup_run_root: Path) -> Path:
    backup_run_root.mkdir(parents=True, exist_ok=True)
    backup_path = build_backup_path(backup_run_root, dest.name)
    shutil.move(str(dest), str(backup_path))
    return backup_path


def copy_agent(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def apply_agent_sync(plan: dict, *, backup: bool, clean: bool, ticket_dir: Path | None = None) -> dict:
    target_root = Path(plan["root"])
    source_agents = {name: Path(path) for name, path in plan.get("source_agents", {}).items()}
    agent_root = Path(plan.get("agent_root", target_agent_root(target_root)))
    codex_config_plan = plan.get("codex_config")
    agent_backup_root: Path | None = None
    agent_backup_records: list[tuple[str, str]] = []
    codex_config_backup: Path | None = None
    codex_config_changed = False

    if backup and (plan.get("agent_update") or (clean and plan.get("agent_remove"))):
        if ticket_dir is not None:
            agent_backup_root = ticket_dir / "agents"
        else:
            agent_backup_root = target_root / ".skill-sync-backups-agents" / timestamp_slug()

    for name in plan.get("agent_add", []):
        copy_agent(source_agents[name], agent_root / name)

    for name in plan.get("agent_update", []):
        dest_path = agent_root / name
        if backup and dest_path.exists():
            assert agent_backup_root is not None
            backup_path = backup_path_entry(dest_path, agent_backup_root)
            agent_backup_records.append((name, str(backup_path)))
        copy_agent(source_agents[name], dest_path)

    if clean:
        for name in plan.get("agent_remove", []):
            agent_path = agent_root / name
            if agent_path.exists():
                if backup:
                    assert agent_backup_root is not None
                    backup_path = backup_path_entry(agent_path, agent_backup_root)
                    agent_backup_records.append((name, str(backup_path)))
                else:
                    agent_path.unlink()

    if codex_config_plan and codex_config_plan.get("update_needed"):
        config_path = Path(codex_config_plan["config_path"])
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if backup and codex_config_plan.get("previous_exists"):
            if ticket_dir is not None:
                codex_config_backup = ticket_dir / "config.toml"
            else:
                codex_config_backup = config_path.with_name("config.toml.agent-skill-sync.bak")
            shutil.copy2(config_path, codex_config_backup)
        config_path.write_text(codex_config_plan["new_content"], encoding="utf-8")
        codex_config_changed = True

    return {
        "agent_backup_root": str(agent_backup_root) if agent_backup_root else None,
        "agent_backups": agent_backup_records,
        "codex_config_backup": str(codex_config_backup) if codex_config_backup else None,
        "codex_config_changed": codex_config_changed,
    }


def rollback_agent_sync(plan: dict) -> None:
    target_root = Path(plan["root"])
    agent_backup_root_value = plan.get("agent_backup_root")
    agent_backup_root = Path(agent_backup_root_value) if agent_backup_root_value else None
    agent_root = Path(plan["agent_root"]) if plan.get("agent_root") else target_agent_root(target_root)

    for name in plan.get("added_agents", []):
        dest = agent_root / name
        if dest.exists():
            dest.unlink()

    for name in sorted(set(plan.get("updated_agents", []) + plan.get("removed_agents", []))):
        if agent_backup_root is None:
            continue
        backup_agent = agent_backup_root / name
        if not backup_agent.exists():
            continue
        dest = agent_root / name
        if dest.exists():
            dest.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_agent, dest)

    codex_config = plan.get("codex_config") or {}
    if codex_config.get("changed"):
        config_path = Path(codex_config["config_path"])
        backup_path_value = codex_config.get("backup_path")
        if backup_path_value:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path_value, config_path)
        elif not codex_config.get("previous_exists", False) and config_path.exists():
            config_path.unlink()
