from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
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

    def test_list_records_includes_target_deployment_status_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            github_skill = make_skill(root / "github", "gh-skill")

            manage_skill_sources.install_materialized_github_skill(
                repo_root=repo_root,
                bucket="shared",
                source_dir=github_skill,
                repo="owner/repo",
                skill_path="skills/shared/gh-skill",
                ref="main",
                resolved_revision="abc123",
            )

            manage_skill_sources.save_registry(
                repo_root / "config" / "deploy-state.local.json",
                {
                    "version": 1,
                    "targets": {
                        "windows_codex": {
                            "skills": {
                                "shared/gh-skill": {
                                    "status": "up_to_date",
                                    "target_up_to_date": True,
                                }
                            }
                        }
                    },
                },
            )

            records = manage_skill_sources.list_records(repo_root)
            record = next(item for item in records if item["key"] == "shared/gh-skill")

            self.assertIn("deployments", record)
            self.assertEqual(record["deployments"]["windows_codex"]["status"], "up_to_date")


class CLITests(unittest.TestCase):
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
