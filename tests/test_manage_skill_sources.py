from __future__ import annotations

import io
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "manage_skill_sources.py"
SPEC = importlib.util.spec_from_file_location("manage_skill_sources", MODULE_PATH)
manage_skill_sources = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = manage_skill_sources
SPEC.loader.exec_module(manage_skill_sources)


def make_skill(root: Path, name: str, body: str = "# Skill\n") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


def prepare_repo_root(root: Path) -> Path:
    repo_root = root / "repo"
    (repo_root / "config").mkdir(parents=True, exist_ok=True)
    (repo_root / "skills" / "shared").mkdir(parents=True, exist_ok=True)
    (repo_root / "skills" / "codex").mkdir(parents=True, exist_ok=True)
    (repo_root / "skills" / "claude").mkdir(parents=True, exist_ok=True)
    return repo_root


def prepare_cli_repo(root: Path) -> Path:
    repo_root = prepare_repo_root(root)
    (repo_root / "scripts").mkdir(parents=True, exist_ok=True)
    (repo_root / "scripts" / "manage_skill_sources.py").write_text(
        MODULE_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return repo_root


def run_cli(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python", "scripts/manage_skill_sources.py", *args],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=True,
    )


class PluginInstallTests(unittest.TestCase):
    def test_install_plugin_skill_writes_local_registry_and_repo_copy(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            plugin_root = root / "plugin"
            skill = make_skill(plugin_root, "demo-plugin", "# Demo plugin\n")
            (skill / "notes.txt").write_text("plugin-source", encoding="utf-8")

            result = manage_skill_sources.install_plugin_skill(
                repo_root=repo_root,
                bucket="codex",
                plugin_path=skill,
            )

            self.assertEqual(result["key"], "codex/demo-plugin")
            dest = repo_root / "skills" / "codex" / "demo-plugin"
            self.assertTrue((dest / "SKILL.md").is_file())
            self.assertEqual((dest / "notes.txt").read_text(encoding="utf-8"), "plugin-source")

            registry = manage_skill_sources.load_registry(
                manage_skill_sources.registry_path(repo_root, "local")
            )
            record = registry["skills"]["codex/demo-plugin"]
            self.assertEqual(record["bucket"], "codex")
            self.assertEqual(record["scope"], "local")
            self.assertEqual(record["source_type"], "plugin")
            self.assertEqual(record["source"]["path"], str(skill))
            self.assertEqual(record["dest"], "skills/codex/demo-plugin")
            self.assertTrue(record["resolved_revision"])

    def test_update_tracked_plugin_skill_refreshes_repo_copy_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            plugin_root = root / "plugin"
            skill = make_skill(plugin_root, "demo-plugin", "# Demo plugin\n")
            (skill / "notes.txt").write_text("v1", encoding="utf-8")

            manage_skill_sources.install_plugin_skill(
                repo_root=repo_root,
                bucket="codex",
                plugin_path=skill,
            )
            registry_path = manage_skill_sources.registry_path(repo_root, "local")
            before = manage_skill_sources.load_registry(registry_path)["skills"]["codex/demo-plugin"]

            (skill / "notes.txt").write_text("v2", encoding="utf-8")
            result = manage_skill_sources.update_tracked_skill(repo_root, "codex/demo-plugin")

            self.assertEqual(result["key"], "codex/demo-plugin")
            dest = repo_root / "skills" / "codex" / "demo-plugin"
            self.assertEqual((dest / "notes.txt").read_text(encoding="utf-8"), "v2")

            after = manage_skill_sources.load_registry(registry_path)["skills"]["codex/demo-plugin"]
            self.assertNotEqual(before["resolved_revision"], after["resolved_revision"])


class GithubInstallTests(unittest.TestCase):
    def test_install_materialized_github_skill_writes_repo_registry(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            source_root = root / "github-source"
            skill = make_skill(source_root, "gh-skill", "# GH skill\n")
            (skill / "README.txt").write_text("repo-source", encoding="utf-8")

            result = manage_skill_sources.install_materialized_github_skill(
                repo_root=repo_root,
                bucket="shared",
                source_dir=skill,
                repo="owner/repo",
                skill_path="skills/shared/gh-skill",
                ref="main",
                resolved_revision="abc123",
            )

            self.assertEqual(result["key"], "shared/gh-skill")
            dest = repo_root / "skills" / "shared" / "gh-skill"
            self.assertTrue((dest / "SKILL.md").is_file())
            self.assertEqual((dest / "README.txt").read_text(encoding="utf-8"), "repo-source")

            registry = manage_skill_sources.load_registry(
                manage_skill_sources.registry_path(repo_root, "repo")
            )
            record = registry["skills"]["shared/gh-skill"]
            self.assertEqual(record["scope"], "repo")
            self.assertEqual(record["source_type"], "github")
            self.assertEqual(record["source"]["repo"], "owner/repo")
            self.assertEqual(record["source"]["path"], "skills/shared/gh-skill")
            self.assertEqual(record["source"]["ref"], "main")
            self.assertEqual(record["resolved_revision"], "abc123")

    def test_update_tracked_github_skill_uses_loader_and_updates_record(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            source_root = root / "github-source"
            initial = make_skill(source_root, "gh-skill", "# GH skill\n")
            (initial / "README.txt").write_text("v1", encoding="utf-8")

            manage_skill_sources.install_materialized_github_skill(
                repo_root=repo_root,
                bucket="shared",
                source_dir=initial,
                repo="owner/repo",
                skill_path="skills/shared/gh-skill",
                ref="main",
                resolved_revision="abc123",
            )

            updated_root = root / "github-updated"
            updated = make_skill(updated_root, "gh-skill", "# GH skill\n")
            (updated / "README.txt").write_text("v2", encoding="utf-8")

            def fake_loader(record: dict) -> tuple[Path, str]:
                self.assertEqual(record["source"]["repo"], "owner/repo")
                return updated, "def456"

            result = manage_skill_sources.update_tracked_skill(
                repo_root,
                "shared/gh-skill",
                github_loader=fake_loader,
            )

            self.assertEqual(result["key"], "shared/gh-skill")
            dest = repo_root / "skills" / "shared" / "gh-skill"
            self.assertEqual((dest / "README.txt").read_text(encoding="utf-8"), "v2")

            registry = manage_skill_sources.load_registry(
                manage_skill_sources.registry_path(repo_root, "repo")
            )
            record = registry["skills"]["shared/gh-skill"]
            self.assertEqual(record["resolved_revision"], "def456")


class ScanTests(unittest.TestCase):
    def test_classify_skill_dir_maps_common_agent_layouts(self) -> None:
        self.assertEqual(
            manage_skill_sources.classify_repo_asset(".claude/skills/everything-claude-code", "SKILL.md")["bucket"],
            "claude",
        )
        self.assertEqual(
            manage_skill_sources.classify_repo_asset(".codex/skills/my-skill", "SKILL.md")["bucket"],
            "codex",
        )
        self.assertEqual(
            manage_skill_sources.classify_repo_asset("skills/configure-ecc", "SKILL.md")["bucket"],
            "shared",
        )
        self.assertEqual(
            manage_skill_sources.classify_repo_asset(".claude/agents/researcher", "researcher.md")["asset_type"],
            "agent",
        )
        self.assertEqual(
            manage_skill_sources.classify_repo_asset(".codex/agents/reviewer", "reviewer.toml")["asset_type"],
            "agent",
        )

    def test_scan_materialized_repo_builds_batch_groups(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = root / "external-repo"
            make_skill(repo_root / ".claude" / "skills", "everything-claude-code")
            make_skill(repo_root / "skills", "configure-ecc")
            make_skill(repo_root / "skills", "continuous-learning")
            (repo_root / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".claude" / "agents" / "researcher.md").write_text("---\nname: researcher\n---\n", encoding="utf-8")
            (repo_root / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")

            scan = manage_skill_sources.scan_materialized_repo(
                repo_root=repo_root,
                repo="affaan-m/everything-claude-code",
                ref="main",
                resolved_revision="scan123",
            )

            self.assertEqual(scan["repo"], "affaan-m/everything-claude-code")
            self.assertEqual(scan["resolved_revision"], "scan123")
            self.assertIn("claude", scan["groups"])
            self.assertIn("shared", scan["groups"])

            claude_paths = [item["path"] for item in scan["groups"]["claude"]]
            shared_paths = [item["path"] for item in scan["groups"]["shared"]]

            self.assertIn(".claude/skills/everything-claude-code", claude_paths)
            self.assertIn("skills/configure-ecc", shared_paths)
            self.assertIn("skills/continuous-learning", shared_paths)
            self.assertEqual(scan["summary"]["claude"], 1)
            self.assertEqual(scan["summary"]["shared"], 2)
            self.assertEqual(len(scan["agents"]), 2)
            self.assertEqual(scan["install_plan"]["skills"]["claude"]["count"], 1)
            self.assertEqual(scan["install_plan"]["skills"]["shared"]["count"], 2)
            self.assertEqual(scan["install_plan"]["skills"]["recognized_total"], 3)
            self.assertEqual(scan["install_plan"]["agents"]["manual_total"], 2)

    def test_scan_materialized_repo_excludes_unknown_by_default_but_can_include_them(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = root / "external-repo"
            make_skill(repo_root / ".cursor" / "skills", "cursor-only")

            default_scan = manage_skill_sources.scan_materialized_repo(
                repo_root=repo_root,
                repo="owner/repo",
                ref="main",
                resolved_revision="scan123",
            )
            verbose_scan = manage_skill_sources.scan_materialized_repo(
                repo_root=repo_root,
                repo="owner/repo",
                ref="main",
                resolved_revision="scan123",
                include_unknown=True,
            )

            self.assertEqual(default_scan["summary"]["unknown"], 0)
            self.assertEqual(verbose_scan["summary"]["unknown"], 1)

    def test_install_scanned_skills_installs_selected_groups_only(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            source_repo = root / "external-repo"
            make_skill(source_repo / ".claude" / "skills", "everything-claude-code")
            make_skill(source_repo / "skills", "configure-ecc")
            make_skill(source_repo / "skills", "continuous-learning")
            (source_repo / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
            (source_repo / ".claude" / "agents" / "researcher.md").write_text("---\nname: researcher\n---\n", encoding="utf-8")

            scan = manage_skill_sources.scan_materialized_repo(
                repo_root=source_repo,
                repo="owner/repo",
                ref="main",
                resolved_revision="scan123",
            )
            result = manage_skill_sources.install_scanned_skills(
                repo_root=repo_root,
                scan=scan,
                selections=["claude", "shared"],
                source_repo_root=source_repo,
            )

            self.assertEqual(result["installed_total"], 3)
            self.assertEqual(result["skipped_total"], 0)
            self.assertTrue((repo_root / "skills" / "claude" / "everything-claude-code" / "SKILL.md").is_file())
            self.assertTrue((repo_root / "skills" / "shared" / "configure-ecc" / "SKILL.md").is_file())
            self.assertTrue((repo_root / "skills" / "shared" / "continuous-learning" / "SKILL.md").is_file())
            self.assertFalse((repo_root / "skills" / "claude" / "researcher").exists())

    def test_install_scanned_skills_skips_existing_tracked_entries(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            source_repo = root / "external-repo"
            make_skill(source_repo / "skills", "configure-ecc")

            existing = make_skill(repo_root / "skills" / "shared", "configure-ecc")
            (existing / "notes.txt").write_text("existing", encoding="utf-8")
            manage_skill_sources.save_registry(
                manage_skill_sources.registry_path(repo_root, "repo"),
                {
                    "version": 1,
                    "skills": {
                        "shared/configure-ecc": {
                            "name": "configure-ecc",
                            "bucket": "shared",
                            "dest": "skills/shared/configure-ecc",
                            "scope": "repo",
                            "source_type": "github",
                            "source": {"repo": "owner/repo", "path": "skills/configure-ecc", "ref": "main"},
                            "resolved_revision": "oldrev",
                            "installed_at": "2026-03-20T00:00:00",
                            "updated_at": "2026-03-20T00:00:00",
                        }
                    },
                },
            )

            scan = manage_skill_sources.scan_materialized_repo(
                repo_root=source_repo,
                repo="owner/repo",
                ref="main",
                resolved_revision="scan123",
            )
            result = manage_skill_sources.install_scanned_skills(
                repo_root=repo_root,
                scan=scan,
                selections=["shared"],
                source_repo_root=source_repo,
            )

            self.assertEqual(result["installed_total"], 0)
            self.assertEqual(result["skipped_total"], 1)
            self.assertEqual(result["results"][0]["status"], "skipped")

    def test_install_scanned_agents_requires_opt_in_copy(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            source_repo = root / "external-repo"
            (source_repo / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (source_repo / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")

            scan = manage_skill_sources.scan_materialized_repo(
                repo_root=source_repo,
                repo="owner/repo",
                ref="main",
                resolved_revision="scan123",
            )

            result = manage_skill_sources.install_scanned_agents(
                repo_root=repo_root,
                scan=scan,
                source_repo_root=source_repo,
                copy_agents=False,
            )
            self.assertEqual(result["installed_total"], 0)
            self.assertEqual(result["skipped_total"], 1)
            self.assertFalse((repo_root / ".codex" / "agents" / "reviewer.toml").exists())

            result = manage_skill_sources.install_scanned_agents(
                repo_root=repo_root,
                scan=scan,
                source_repo_root=source_repo,
                copy_agents=True,
            )
            self.assertEqual(result["installed_total"], 1)
            self.assertTrue((repo_root / ".codex" / "agents" / "reviewer.toml").is_file())

    def test_register_codex_agents_updates_config_toml_safely(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            (repo_root / ".codex").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")
            config_toml = repo_root / ".codex" / "config.toml"
            config_toml.write_text("[agents.existing]\npath = \".codex/agents/existing.toml\"\n", encoding="utf-8")

            result = manage_skill_sources.register_codex_agents(
                repo_root=repo_root,
                agent_names=["reviewer"],
            )

            self.assertEqual(result["registered"], ["reviewer"])
            content = config_toml.read_text(encoding="utf-8")
            self.assertIn("[agents.existing]", content)
            self.assertIn("[agents.reviewer]", content)
            self.assertIn('path = ".codex/agents/reviewer.toml"', content)

    def test_register_codex_agents_skips_unmanaged_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            (repo_root / ".codex").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")
            config_toml = repo_root / ".codex" / "config.toml"
            config_toml.write_text("[agents.reviewer]\npath = \"/some/other/location.toml\"\n", encoding="utf-8")

            result = manage_skill_sources.register_codex_agents(
                repo_root=repo_root,
                agent_names=["reviewer"],
            )

            self.assertEqual(result["registered"], [])
            self.assertEqual(result["skipped"][0]["name"], "reviewer")


class RegistryTests(unittest.TestCase):
    def test_list_records_merges_repo_and_local_registries(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            plugin_skill = make_skill(root / "plugin", "demo-plugin")
            github_skill = make_skill(root / "github", "gh-skill")

            manage_skill_sources.install_plugin_skill(
                repo_root=repo_root,
                bucket="codex",
                plugin_path=plugin_skill,
            )
            manage_skill_sources.install_materialized_github_skill(
                repo_root=repo_root,
                bucket="shared",
                source_dir=github_skill,
                repo="owner/repo",
                skill_path="skills/shared/gh-skill",
                ref="main",
                resolved_revision="abc123",
            )

            records = manage_skill_sources.list_records(repo_root)

            keys = [record["key"] for record in records]
            self.assertEqual(sorted(keys), ["codex/demo-plugin", "shared/gh-skill"])


class CLITests(unittest.TestCase):
    def test_print_scan_renders_install_plan_sections(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            source_repo = root / "source-repo"
            make_skill(source_repo / ".claude" / "skills", "everything-claude-code")
            make_skill(source_repo / "skills", "configure-ecc")
            (source_repo / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
            (source_repo / ".claude" / "agents" / "researcher.md").write_text("---\nname: researcher\n---\n", encoding="utf-8")

            scan = manage_skill_sources.scan_materialized_repo(
                repo_root=source_repo,
                repo="owner/repo",
                ref="main",
                resolved_revision="scan123",
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                manage_skill_sources.print_scan(scan, "text")
            output = buffer.getvalue()

            self.assertIn("install plan:", output)
            self.assertIn("claude: 1 skill(s)", output)
            self.assertIn("shared: 1 skill(s)", output)
            self.assertIn("agents: 1 manual item(s)", output)

    def test_cli_install_plugin_prints_install_message_and_writes_registry(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_cli_repo(root)
            plugin_skill = make_skill(root / "plugin", "demo-plugin", "# Demo plugin\n")
            (plugin_skill / "notes.txt").write_text("plugin-source", encoding="utf-8")

            result = run_cli(
                repo_root,
                "install-plugin",
                "--bucket",
                "codex",
                "--path",
                str(plugin_skill),
            )

            self.assertIn("Installed codex/demo-plugin into", result.stdout)
            registry = manage_skill_sources.load_registry(
                manage_skill_sources.registry_path(repo_root, "local")
            )
            self.assertIn("codex/demo-plugin", registry["skills"])

    def test_cli_update_refreshes_tracked_plugin_skill(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_cli_repo(root)
            plugin_skill = make_skill(root / "plugin", "demo-plugin", "# Demo plugin\n")
            (plugin_skill / "notes.txt").write_text("v1", encoding="utf-8")

            run_cli(
                repo_root,
                "install-plugin",
                "--bucket",
                "codex",
                "--path",
                str(plugin_skill),
            )
            (plugin_skill / "notes.txt").write_text("v2", encoding="utf-8")

            result = run_cli(repo_root, "update", "--key", "codex/demo-plugin")

            self.assertIn("Updated codex/demo-plugin ->", result.stdout)
            dest = repo_root / "skills" / "codex" / "demo-plugin"
            self.assertEqual((dest / "notes.txt").read_text(encoding="utf-8"), "v2")


if __name__ == "__main__":
    unittest.main()
