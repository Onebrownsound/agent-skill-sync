from __future__ import annotations

import io
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "manage_skill_sources.py"
SOURCE_IMPRINTS_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "source_imprints.py"
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
    (repo_root / "config" / "install-plans").mkdir(parents=True, exist_ok=True)
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
    (repo_root / "scripts" / "source_imprints.py").write_text(
        SOURCE_IMPRINTS_MODULE_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return repo_root


def run_cli(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/manage_skill_sources.py", *args],
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
            self.assertTrue((repo_root / record["imprint"] / "SKILL.md").is_file())
            self.assertTrue((repo_root / record["overlay"]).is_dir())

    def test_update_all_tracked_skills_skips_tracked_repo_entries(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            plugin_skill = make_skill(root / "plugin", "demo-plugin")

            manage_skill_sources.install_plugin_skill(
                repo_root=repo_root,
                bucket="codex",
                plugin_path=plugin_skill,
            )
            manage_skill_sources.save_registry(
                repo_root / "config" / "tracked-skill-sources.local.json",
                {
                    "version": 1,
                    "skills": {
                        "shared/gstack-browse": {
                            "name": "gstack-browse",
                            "bucket": "shared",
                            "dest": "skills/shared/gstack-browse",
                            "scope": "repo",
                            "source_type": "tracked_repo",
                            "source": {
                                "repo": "owner/repo",
                                "ref": "main",
                                "path": "browse",
                                "source_name": "gstack",
                            },
                            "resolved_revision": "abc123",
                            "installed_at": "2026-03-24T00:00:00",
                            "updated_at": "2026-03-24T00:00:00",
                        }
                    },
                },
            )

            results = manage_skill_sources.update_all_tracked_skills(repo_root)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["key"], "codex/demo-plugin")

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
            self.assertTrue((repo_root / record["imprint"] / "SKILL.md").is_file())
            self.assertTrue((repo_root / record["overlay"]).is_dir())

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
            self.assertEqual((repo_root / record["imprint"] / "README.txt").read_text(encoding="utf-8"), "v2")

    def test_git_sparse_checkout_warns_when_https_clone_falls_back_to_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            calls: list[list[str]] = []
            original_run_git = manage_skill_sources.run_git

            def fake_run_git(args: list[str]) -> None:
                calls.append(args)
                if args[:2] == ["git", "clone"] and "https://github.com/owner/repo.git" in args:
                    raise manage_skill_sources.SourceError("https failed")

            buffer = io.StringIO()
            manage_skill_sources.run_git = fake_run_git
            try:
                with redirect_stderr(buffer):
                    repo_dir = manage_skill_sources.git_sparse_checkout(
                        "owner/repo",
                        "main",
                        "skills/demo",
                        root,
                    )
            finally:
                manage_skill_sources.run_git = original_run_git

            self.assertEqual(repo_dir, root / "repo")
            self.assertIn("Falling back to SSH clone for owner/repo", buffer.getvalue())
            self.assertTrue(any("git@github.com:owner/repo.git" in call for call in calls))

    def test_git_clone_repo_warns_when_https_clone_falls_back_to_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            calls: list[list[str]] = []
            original_run_git = manage_skill_sources.run_git

            def fake_run_git(args: list[str]) -> None:
                calls.append(args)
                if args[:2] == ["git", "clone"] and "https://github.com/owner/repo.git" in args:
                    raise manage_skill_sources.SourceError("https failed")

            buffer = io.StringIO()
            manage_skill_sources.run_git = fake_run_git
            try:
                with redirect_stderr(buffer):
                    repo_dir = manage_skill_sources.git_clone_repo("owner/repo", "main", root)
            finally:
                manage_skill_sources.run_git = original_run_git

            self.assertEqual(repo_dir, root / "repo")
            self.assertIn("Falling back to SSH clone for owner/repo", buffer.getvalue())
            self.assertTrue(any("git@github.com:owner/repo.git" in call for call in calls))


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

    def test_resolve_selected_scan_items_rejects_unknown_path(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            source_repo = root / "external-repo"
            make_skill(source_repo / "skills", "configure-ecc")

            scan = manage_skill_sources.scan_materialized_repo(
                repo_root=source_repo,
                repo="owner/repo",
                ref="main",
                resolved_revision="scan123",
            )

            with self.assertRaises(manage_skill_sources.SourceError) as raised:
                manage_skill_sources.resolve_selected_scan_items(scan, ["skills/missing-skill"])

            self.assertIn("skills/missing-skill", str(raised.exception))

    def test_install_selected_scan_items_installs_only_requested_paths(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            source_repo = root / "external-repo"
            make_skill(source_repo / ".claude" / "skills", "everything-claude-code")
            make_skill(source_repo / "skills", "configure-ecc")
            make_skill(source_repo / "skills", "continuous-learning")
            (source_repo / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (source_repo / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")
            (source_repo / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
            (source_repo / ".claude" / "agents" / "researcher.md").write_text("---\nname: researcher\n---\n", encoding="utf-8")

            scan = manage_skill_sources.scan_materialized_repo(
                repo_root=source_repo,
                repo="owner/repo",
                ref="main",
                resolved_revision="scan123",
            )
            result = manage_skill_sources.install_selected_scan_items(
                repo_root=repo_root,
                scan=scan,
                item_paths=["skills/configure-ecc", ".codex/agents/reviewer.toml"],
                source_repo_root=source_repo,
                copy_agents=True,
            )

            self.assertEqual(result["selected_total"], 2)
            self.assertEqual(result["skills"]["installed_total"], 1)
            self.assertEqual(result["agents"]["installed_total"], 1)
            self.assertTrue((repo_root / "skills" / "shared" / "configure-ecc" / "SKILL.md").is_file())
            self.assertTrue((repo_root / ".codex" / "agents" / "reviewer.toml").is_file())
            self.assertFalse((repo_root / "skills" / "shared" / "continuous-learning").exists())
            self.assertFalse((repo_root / "skills" / "claude" / "everything-claude-code").exists())
            self.assertFalse((repo_root / ".claude" / "agents" / "researcher.md").exists())

    def test_plan_path_uses_repo_slug(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            repo_root = prepare_repo_root(Path(root_dir))
            plan_path = manage_skill_sources.install_plan_path(repo_root, "garrytan/gstack")
            self.assertEqual(plan_path, repo_root / "config" / "install-plans" / "garrytan-gstack.json")

    def test_save_and_load_install_plan_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            repo_root = prepare_repo_root(Path(root_dir))
            plan = {
                "version": 1,
                "repo": "garrytan/gstack",
                "ref": "main",
                "resolved_revision": "abc123",
                "status": "proposed",
                "generated_at": "2026-03-22T12:00:00",
                "items": [
                    {
                        "source_path": ".agents/skills/gstack-review",
                        "kind": "skill",
                        "bucket": "shared",
                        "confidence": "high",
                        "reason": "Path matches .agents/skills convention",
                        "approved": True,
                    }
                ],
            }
            plan_path = manage_skill_sources.install_plan_path(repo_root, plan["repo"])

            manage_skill_sources.save_install_plan(plan_path, plan)
            loaded = manage_skill_sources.load_install_plan(plan_path)

            self.assertEqual(loaded, plan)

    def test_load_install_plan_accepts_extended_status_values(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            repo_root = prepare_repo_root(Path(root_dir))
            for status in ("reviewed", "superseded"):
                plan = {
                    "version": 1,
                    "repo": "garrytan/gstack",
                    "ref": "main",
                    "resolved_revision": "abc123",
                    "status": status,
                    "generated_at": "2026-03-22T12:00:00",
                    "items": [],
                }
                plan_path = manage_skill_sources.install_plan_path(repo_root, f"garrytan/{status}")

                manage_skill_sources.save_install_plan(plan_path, plan)
                loaded = manage_skill_sources.load_install_plan(plan_path)

                self.assertEqual(loaded["status"], status)

    def test_load_install_plan_rejects_invalid_shape(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            repo_root = prepare_repo_root(Path(root_dir))
            plan_path = manage_skill_sources.install_plan_path(repo_root, "garrytan/gstack")
            plan_path.write_text('{"version":1,"repo":"garrytan/gstack"}', encoding="utf-8")

            with self.assertRaises(manage_skill_sources.SourceError):
                manage_skill_sources.load_install_plan(plan_path)

    def test_load_install_plan_rejects_invalid_timestamp_fields(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            repo_root = prepare_repo_root(Path(root_dir))
            plan_path = manage_skill_sources.install_plan_path(repo_root, "garrytan/gstack")
            plan_path.write_text(
                (
                    '{'
                    '"version":1,'
                    '"repo":"garrytan/gstack",'
                    '"ref":"main",'
                    '"resolved_revision":"abc123",'
                    '"status":"proposed",'
                    '"generated_at":"not-a-timestamp",'
                    '"items":[]'
                    '}'
                ),
                encoding="utf-8",
            )

            with self.assertRaises(manage_skill_sources.SourceError):
                manage_skill_sources.load_install_plan(plan_path)

    def test_apply_install_plan_installs_only_approved_items(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            source_repo = root / "external-repo"
            make_skill(source_repo / ".agents" / "skills", "gstack-review")
            make_skill(source_repo / ".agents" / "skills", "gstack-qa")

            plan = {
                "version": 1,
                "repo": "garrytan/gstack",
                "ref": "main",
                "resolved_revision": "scan123",
                "status": "proposed",
                "items": [
                    {
                        "source_path": ".agents/skills/gstack-review",
                        "kind": "skill",
                        "bucket": "shared",
                        "confidence": "high",
                        "reason": "Path matches .agents/skills convention",
                        "approved": True,
                    },
                    {
                        "source_path": ".agents/skills/gstack-qa",
                        "kind": "skill",
                        "bucket": "shared",
                        "confidence": "high",
                        "reason": "Path matches .agents/skills convention",
                        "approved": False,
                    },
                ],
            }

            result = manage_skill_sources.apply_install_plan(
                repo_root=repo_root,
                plan=plan,
                source_repo_root=source_repo,
            )

            self.assertEqual(result["installed_total"], 1)
            self.assertEqual(result["skipped_total"], 1)
            self.assertTrue((repo_root / "skills" / "shared" / "gstack-review" / "SKILL.md").is_file())
            self.assertFalse((repo_root / "skills" / "shared" / "gstack-qa").exists())

    def test_apply_install_plan_rejects_missing_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            source_repo = root / "external-repo"
            source_repo.mkdir(parents=True, exist_ok=True)

            plan = {
                "version": 1,
                "repo": "garrytan/gstack",
                "ref": "main",
                "resolved_revision": "scan123",
                "status": "proposed",
                "items": [
                    {
                        "source_path": ".agents/skills/gstack-review",
                        "kind": "skill",
                        "bucket": "shared",
                        "confidence": "high",
                        "reason": "Path matches .agents/skills convention",
                        "approved": True,
                    }
                ],
            }

            with self.assertRaises(manage_skill_sources.SourceError):
                manage_skill_sources.apply_install_plan(
                    repo_root=repo_root,
                    plan=plan,
                    source_repo_root=source_repo,
                )

    def test_apply_install_plan_is_idempotent_after_initial_apply(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            source_repo = root / "external-repo"
            make_skill(source_repo / ".agents" / "skills", "gstack-review")

            plan = {
                "version": 1,
                "repo": "garrytan/gstack",
                "ref": "main",
                "resolved_revision": "scan123",
                "status": "applied",
                "generated_at": "2026-03-22T12:00:00",
                "last_applied_at": "2026-03-22T12:05:00",
                "items": [
                    {
                        "source_path": ".agents/skills/gstack-review",
                        "kind": "skill",
                        "bucket": "shared",
                        "confidence": "high",
                        "reason": "Path matches .agents/skills convention",
                        "approved": True,
                    }
                ],
            }

            first = manage_skill_sources.apply_install_plan(
                repo_root=repo_root,
                plan=plan,
                source_repo_root=source_repo,
            )
            second = manage_skill_sources.apply_install_plan(
                repo_root=repo_root,
                plan=plan,
                source_repo_root=source_repo,
            )

            self.assertEqual(first["installed_total"], 1)
            self.assertEqual(first["skipped_total"], 0)
            self.assertEqual(second["installed_total"], 0)
            self.assertEqual(second["skipped_total"], 1)
            self.assertEqual(second["results"][0]["status"], "skipped")
            self.assertIn("already exists", second["results"][0]["reason"])

    def test_update_plan_check_metadata_records_preview_state(self) -> None:
        plan = {
            "version": 1,
            "repo": "garrytan/gstack",
            "ref": "main",
            "resolved_revision": "scan123",
            "status": "proposed",
            "generated_at": "2026-03-22T12:00:00",
            "items": [],
        }
        updated = manage_skill_sources.update_plan_check_metadata(
            plan,
            {"installed_total": 2, "skipped_total": 1},
            "scan123",
        )

        self.assertEqual(updated["status"], "proposed")
        self.assertEqual(updated["last_checked_revision"], "scan123")
        self.assertEqual(updated["last_check_result"]["installed_total"], 2)
        self.assertIn("last_checked_at", updated)
        self.assertEqual(updated["generated_at"], "2026-03-22T12:00:00")

    def test_update_plan_apply_metadata_records_apply_state(self) -> None:
        plan = {
            "version": 1,
            "repo": "garrytan/gstack",
            "ref": "main",
            "resolved_revision": "scan123",
            "status": "proposed",
            "generated_at": "2026-03-22T12:00:00",
            "items": [],
        }
        updated = manage_skill_sources.update_plan_apply_metadata(
            plan,
            {"installed_total": 2, "skipped_total": 1},
        )

        self.assertEqual(updated["status"], "applied")
        self.assertEqual(updated["last_checked_revision"], "scan123")
        self.assertEqual(updated["last_check_result"]["skipped_total"], 1)
        self.assertIn("last_applied_at", updated)
        self.assertEqual(updated["generated_at"], "2026-03-22T12:00:00")

    def test_extract_json_payload_finds_first_json_object_in_text(self) -> None:
        payload = manage_skill_sources.extract_json_payload("prefix\n{\"items\": []}\nsuffix")
        self.assertEqual(payload, {"items": []})

    def test_extract_json_payload_uses_claude_structured_output_wrapper(self) -> None:
        payload = manage_skill_sources.extract_json_payload(
            '{"type":"result","structured_output":{"items":[]}}'
        )
        self.assertEqual(payload, {"items": []})

    def test_analyze_layout_with_claude_backend_uses_expected_command_shape(self) -> None:
        scan = {
            "repo": "garrytan/gstack",
            "ref": "main",
            "resolved_revision": "scan123",
            "skills": [],
            "agents": [],
            "groups": {"shared": [], "codex": [], "claude": [], "unknown": []},
        }
        commands: list[list[str]] = []
        inputs: list[str | None] = []

        def fake_runner(command: list[str], *, input_text=None) -> str:
            commands.append(command)
            inputs.append(input_text)
            return '{"items":[]}'

        plan = manage_skill_sources.analyze_layout_with_backend(scan, "claude", runner=fake_runner)

        self.assertEqual(plan["analysis_backend"], "claude")
        self.assertEqual(commands[0][0], "claude")
        self.assertIn("--json-schema", commands[0])
        self.assertIn("--output-format", commands[0])
        self.assertIsInstance(inputs[0], str)
        self.assertIn("Framework rules:", inputs[0])

    def test_analyze_layout_with_codex_backend_uses_expected_command_shape(self) -> None:
        scan = {
            "repo": "garrytan/gstack",
            "ref": "main",
            "resolved_revision": "scan123",
            "skills": [],
            "agents": [],
            "groups": {"shared": [], "codex": [], "claude": [], "unknown": []},
        }
        commands: list[list[str]] = []
        inputs: list[str | None] = []

        def fake_runner(command: list[str], *, input_text=None) -> str:
            commands.append(command)
            inputs.append(input_text)
            return '{"items":[]}'

        plan = manage_skill_sources.analyze_layout_with_backend(scan, "codex", runner=fake_runner)

        self.assertEqual(plan["analysis_backend"], "codex")
        self.assertEqual(commands[0][:2], ["codex", "exec"])
        self.assertIsInstance(inputs[0], str)
        self.assertIn("Scan inventory:", inputs[0])

    def test_analyze_layout_with_backend_rejects_invalid_payload(self) -> None:
        scan = {
            "repo": "garrytan/gstack",
            "ref": "main",
            "resolved_revision": "scan123",
            "skills": [],
            "agents": [],
            "groups": {"shared": [], "codex": [], "claude": [], "unknown": []},
        }

        def fake_runner(command: list[str], *, input_text=None) -> str:
            return '{"items":[{"source_path":"x"}]}'

        with self.assertRaises(manage_skill_sources.SourceError):
            manage_skill_sources.analyze_layout_with_backend(scan, "claude", runner=fake_runner)

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

    def test_register_codex_agents_writes_managed_block_and_preserves_manual_entries(self) -> None:
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
            self.assertIn(manage_skill_sources.MANAGED_AGENTS_BEGIN, content)
            self.assertIn(manage_skill_sources.MANAGED_AGENTS_END, content)
            self.assertIn("[agents.reviewer]", content)
            self.assertIn('path = ".codex/agents/reviewer.toml"', content)
            self.assertLess(content.index("[agents.existing]"), content.index(manage_skill_sources.MANAGED_AGENTS_BEGIN))

    def test_register_codex_agents_merges_into_existing_managed_block(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            (repo_root / ".codex").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")
            (repo_root / ".codex" / "agents" / "docs.toml").write_text("[agent]\n", encoding="utf-8")
            config_toml = repo_root / ".codex" / "config.toml"
            config_toml.write_text(
                "\n".join(
                    [
                        "[agents.existing]",
                        'path = ".codex/agents/existing.toml"',
                        "",
                        manage_skill_sources.MANAGED_AGENTS_BEGIN,
                        "[agents.old]",
                        'path = ".codex/agents/old.toml"',
                        manage_skill_sources.MANAGED_AGENTS_END,
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = manage_skill_sources.register_codex_agents(
                repo_root=repo_root,
                agent_names=["reviewer", "docs"],
            )

            self.assertEqual(result["registered"], ["reviewer", "docs"])
            content = config_toml.read_text(encoding="utf-8")
            self.assertIn("[agents.existing]", content)
            self.assertIn("[agents.old]", content)
            self.assertIn("[agents.reviewer]", content)
            self.assertIn("[agents.docs]", content)
            self.assertEqual(content.count(manage_skill_sources.MANAGED_AGENTS_BEGIN), 1)
            self.assertEqual(content.count(manage_skill_sources.MANAGED_AGENTS_END), 1)

    def test_register_codex_agents_migrates_existing_managed_section_into_block(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            (repo_root / ".codex").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")
            config_toml = repo_root / ".codex" / "config.toml"
            config_toml.write_text(
                "[agents.reviewer]\npath = \".codex/agents/reviewer.toml\"\n",
                encoding="utf-8",
            )

            result = manage_skill_sources.register_codex_agents(
                repo_root=repo_root,
                agent_names=["reviewer"],
            )

            self.assertEqual(result["registered"], ["reviewer"])
            content = config_toml.read_text(encoding="utf-8")
            self.assertIn(manage_skill_sources.MANAGED_AGENTS_BEGIN, content)
            self.assertIn(manage_skill_sources.MANAGED_AGENTS_END, content)
            self.assertEqual(content.count("[agents.reviewer]"), 1)

    def test_register_codex_agents_creates_backup_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            (repo_root / ".codex").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")
            config_toml = repo_root / ".codex" / "config.toml"
            original = "[agents.existing]\npath = \".codex/agents/existing.toml\"\n"
            config_toml.write_text(original, encoding="utf-8")

            result = manage_skill_sources.register_codex_agents(
                repo_root=repo_root,
                agent_names=["reviewer"],
            )

            backup_path = Path(result["backup"])
            self.assertTrue(backup_path.is_file())
            self.assertEqual(backup_path.read_text(encoding="utf-8"), original)

    def test_register_codex_agents_does_not_create_backup_when_creating_new_config(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            (repo_root / ".codex").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")

            result = manage_skill_sources.register_codex_agents(
                repo_root=repo_root,
                agent_names=["reviewer"],
            )

            self.assertIsNone(result["backup"])
            self.assertFalse(manage_skill_sources.codex_config_backup_path(repo_root).exists())

    def test_register_codex_agents_noop_does_not_rewrite_or_backup(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            (repo_root / ".codex").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")
            config_toml = repo_root / ".codex" / "config.toml"
            content = (
                manage_skill_sources.MANAGED_AGENTS_BEGIN
                + "\n[agents.reviewer]\npath = \".codex/agents/reviewer.toml\"\n"
                + manage_skill_sources.MANAGED_AGENTS_END
                + "\n"
            )
            config_toml.write_text(content, encoding="utf-8")

            result = manage_skill_sources.register_codex_agents(
                repo_root=repo_root,
                agent_names=["reviewer"],
            )

            self.assertEqual(result["registered"], ["reviewer"])
            self.assertIsNone(result["backup"])
            self.assertEqual(config_toml.read_text(encoding="utf-8"), content)
            self.assertFalse(manage_skill_sources.codex_config_backup_path(repo_root).exists())

    def test_register_codex_agents_deduplicates_requested_names(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            (repo_root / ".codex").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")

            result = manage_skill_sources.register_codex_agents(
                repo_root=repo_root,
                agent_names=["reviewer", "reviewer"],
            )

            self.assertEqual(result["registered"], ["reviewer"])
            content = (repo_root / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertEqual(content.count("[agents.reviewer]"), 1)

    def test_register_codex_agents_preserves_suffix_order_around_managed_block(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            (repo_root / ".codex").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")
            config_toml = repo_root / ".codex" / "config.toml"
            config_toml.write_text(
                "\n".join(
                    [
                        "[agents.before]",
                        'path = ".codex/agents/before.toml"',
                        "",
                        manage_skill_sources.MANAGED_AGENTS_BEGIN,
                        "[agents.old]",
                        'path = ".codex/agents/old.toml"',
                        manage_skill_sources.MANAGED_AGENTS_END,
                        "",
                        "[agents.after]",
                        'path = ".codex/agents/after.toml"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            manage_skill_sources.register_codex_agents(
                repo_root=repo_root,
                agent_names=["reviewer"],
            )

            content = config_toml.read_text(encoding="utf-8")
            self.assertLess(content.index("[agents.before]"), content.index(manage_skill_sources.MANAGED_AGENTS_BEGIN))
            self.assertLess(content.index(manage_skill_sources.MANAGED_AGENTS_END), content.index("[agents.after]"))

    def test_register_codex_agents_rejects_invalid_managed_markers_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            (repo_root / ".codex").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")
            config_toml = repo_root / ".codex" / "config.toml"
            invalid = manage_skill_sources.MANAGED_AGENTS_BEGIN + "\n[agents.old]\n"
            config_toml.write_text(invalid, encoding="utf-8")

            with self.assertRaises(manage_skill_sources.SourceError):
                manage_skill_sources.register_codex_agents(
                    repo_root=repo_root,
                    agent_names=["reviewer"],
                )

            self.assertEqual(config_toml.read_text(encoding="utf-8"), invalid)
            self.assertFalse(manage_skill_sources.codex_config_backup_path(repo_root).exists())

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
            content = config_toml.read_text(encoding="utf-8")
            self.assertNotIn(manage_skill_sources.MANAGED_AGENTS_BEGIN, content)

    def test_register_codex_agents_warns_on_partially_managed_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            (repo_root / ".codex").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents").mkdir(parents=True, exist_ok=True)
            (repo_root / ".codex" / "agents" / "reviewer.toml").write_text("[agent]\n", encoding="utf-8")
            config_toml = repo_root / ".codex" / "config.toml"
            config_toml.write_text(
                "[agents.reviewer]\npath = \".codex/agents/reviewer.toml\"\nmodel = \"custom\"\n",
                encoding="utf-8",
            )

            buffer = io.StringIO()
            with redirect_stderr(buffer):
                result = manage_skill_sources.register_codex_agents(
                    repo_root=repo_root,
                    agent_names=["reviewer"],
                )

            self.assertEqual(result["registered"], [])
            self.assertEqual(result["skipped"][0]["reason"], "existing partially managed agent config")
            self.assertIn("partially managed", buffer.getvalue())


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

    def test_list_records_includes_tracked_source_registry_entries(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            manage_skill_sources.save_registry(
                repo_root / "config" / "tracked-skill-sources.local.json",
                {
                    "version": 1,
                    "skills": {
                        "shared/gstack-browse": {
                            "name": "gstack-browse",
                            "bucket": "shared",
                            "dest": "skills/shared/gstack-browse",
                            "scope": "repo",
                            "source_type": "tracked_repo",
                            "source": {
                                "repo": "owner/repo",
                                "ref": "main",
                                "path": "browse",
                                "source_name": "gstack",
                            },
                            "resolved_revision": "abc123",
                            "installed_at": "2026-03-24T00:00:00",
                            "updated_at": "2026-03-24T00:00:00",
                        }
                    },
                },
            )

            records = manage_skill_sources.list_records(repo_root)
            record = next(item for item in records if item["key"] == "shared/gstack-browse")

            self.assertEqual(record["source_type"], "tracked_repo")
            self.assertEqual(record["source"]["source_name"], "gstack")


class CLITests(unittest.TestCase):
    def test_proposed_install_plan_from_scan_maps_agents_skills_as_high_confidence_shared(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            source_repo = root / "source-repo"
            make_skill(source_repo / ".agents" / "skills", "gstack-review")

            scan = manage_skill_sources.scan_materialized_repo(
                repo_root=source_repo,
                repo="garrytan/gstack",
                ref="main",
                resolved_revision="scan123",
                include_unknown=True,
            )
            plan = manage_skill_sources.proposed_install_plan_from_scan(scan)

            self.assertEqual(plan["repo"], "garrytan/gstack")
            self.assertEqual(plan["items"][0]["source_path"], ".agents/skills/gstack-review")
            self.assertEqual(plan["items"][0]["bucket"], "shared")
            self.assertEqual(plan["items"][0]["confidence"], "high")
            self.assertTrue(plan["items"][0]["approved"])

    def test_preview_install_plan_is_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = prepare_repo_root(root)
            source_repo = root / "source-repo"
            make_skill(source_repo / ".agents" / "skills", "gstack-review")
            plan = {
                "version": 1,
                "repo": "garrytan/gstack",
                "ref": "main",
                "resolved_revision": "scan123",
                "status": "proposed",
                "items": [
                    {
                        "source_path": ".agents/skills/gstack-review",
                        "kind": "skill",
                        "bucket": "shared",
                        "confidence": "high",
                        "reason": "Path matches .agents/skills convention",
                        "approved": True,
                    }
                ],
            }

            result = manage_skill_sources.preview_install_plan(
                repo_root=repo_root,
                plan=plan,
                source_repo_root=source_repo,
            )

            self.assertEqual(result["installed_total"], 1)
            self.assertFalse((repo_root / "skills" / "shared" / "gstack-review").exists())

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
