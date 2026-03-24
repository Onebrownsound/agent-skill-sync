from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_skills.py"
AGENT_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_agents.py"
SOURCE_IMPRINTS_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "source_imprints.py"
SPEC = importlib.util.spec_from_file_location("sync_skills", MODULE_PATH)
sync_skills = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = sync_skills
SPEC.loader.exec_module(sync_skills)


def make_skill(root: Path, name: str, body: str = "# Skill\n") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


def make_agent(root: Path, filename: str, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    agent_file = root / filename
    agent_file.write_text(body, encoding="utf-8")
    return agent_file


def sample_config(target_path: Path) -> dict:
    return {
        "version": 1,
        "manifest_filename": ".skill-sync-manifest.json",
        "catalog": {
            "shared": "skills/shared",
            "codex": "skills/codex",
            "claude": "skills/claude",
        },
        "targets": {
            "windows_codex": {
                "enabled": True,
                "host": "windows",
                "kind": "codex",
                "path": str(target_path),
            }
        },
    }


def make_cli_repo(root: Path, target_path: Path) -> Path:
    repo_root = root / "repo"
    (repo_root / "scripts").mkdir(parents=True, exist_ok=True)
    (repo_root / "config").mkdir(parents=True, exist_ok=True)
    (repo_root / "skills" / "shared").mkdir(parents=True, exist_ok=True)
    (repo_root / "skills" / "codex").mkdir(parents=True, exist_ok=True)
    (repo_root / "skills" / "claude").mkdir(parents=True, exist_ok=True)
    (repo_root / "scripts" / "sync_skills.py").write_text(MODULE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    (repo_root / "scripts" / "sync_agents.py").write_text(
        AGENT_MODULE_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repo_root / "scripts" / "sync_tracked_repos.py").write_text(
        (Path(__file__).resolve().parents[1] / "scripts" / "sync_tracked_repos.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repo_root / "scripts" / "source_imprints.py").write_text(
        SOURCE_IMPRINTS_MODULE_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    config = sample_config(target_path)
    config["targets"]["windows_codex"]["host"] = sync_skills.detect_host()
    sync_skills.write_json(repo_root / "config" / "targets.local.json", config)
    return repo_root


def run_cli(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/sync_skills.py", *args],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=True,
    )


class HelperTests(unittest.TestCase):
    def test_target_root_for_runtime_rejects_wsl_path_from_windows_runtime(self) -> None:
        with self.assertRaises(sync_skills.SourceError):
            sync_skills.target_root_for_runtime(
                {"host": "wsl", "path": "/home/redme/.codex/skills"},
                "windows",
            )

    def test_to_wsl_path_converts_windows_repo_root(self) -> None:
        converted = sync_skills.to_wsl_path(Path(r"C:\Users\redme\01-Active\Projects\agent-skill-sync"))
        self.assertEqual(converted, "/mnt/c/Users/redme/01-Active/Projects/agent-skill-sync")

    def test_maybe_delegate_wsl_targets_shells_out_from_windows_for_host_all(self) -> None:
        config = {
            "targets": {
                "windows_codex": {
                    "enabled": True,
                    "host": "windows",
                    "kind": "codex",
                    "path": r"C:\Users\redme\.codex\skills",
                },
                "wsl_codex": {
                    "enabled": True,
                    "host": "wsl",
                    "kind": "codex",
                    "path": "/home/redme/.codex/skills",
                },
            }
        }
        args = sync_skills.parse_args.__globals__["argparse"].Namespace(
            config="config/targets.local.json",
            host="all",
            target=[],
            pull=False,
            bucket=None,
            rollback=None,
            check=True,
            apply=False,
            clean=False,
            no_backup=False,
            quiet=False,
            ticket_id=None,
        )
        calls: list[list[str]] = []

        def fake_runner(command: list[str], text: bool = True):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        local_host, delegated_exit = sync_skills.maybe_delegate_wsl_targets(
            repo_root=Path(r"C:\Users\redme\01-Active\Projects\agent-skill-sync"),
            config=config,
            args=args,
            requested_host="all",
            runtime_host="windows",
            runner=fake_runner,
        )

        self.assertEqual(local_host, "windows")
        self.assertIsNone(delegated_exit)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:3], ["wsl.exe", "bash", "-lc"])
        self.assertIn("--host wsl", calls[0][3])
        self.assertIn("--target wsl_codex", calls[0][3])

    def test_delegated_sync_args_preserve_new_flags(self) -> None:
        args = sync_skills.parse_args.__globals__["argparse"].Namespace(
            config="config/targets.local.json",
            host="all",
            target=[],
            pull=False,
            bucket=None,
            rollback=None,
            check=True,
            apply=False,
            clean=False,
            no_backup=False,
            quiet=False,
            ticket_id=None,
            update_sources=True,
            migrate_manifests=True,
        )

        command = sync_skills.delegated_sync_args(args, host_override="wsl", target_ids=["wsl_codex"])

        self.assertIn("--update-sources", command)
        self.assertIn("--migrate-manifests", command)


class TrackedSourceRefreshTests(unittest.TestCase):
    def _make_runner(self, commit: str = "abc123"):
        def runner(cmd, **kwargs):
            if "rev-parse" in cmd:
                from unittest.mock import MagicMock
                result = MagicMock()
                result.stdout = commit + "\n"
                return result
            from unittest.mock import MagicMock
            return MagicMock()

        return runner

    def test_refresh_tracked_source_catalog_materializes_repo_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = root / "repo"
            (repo_root / "config").mkdir(parents=True)
            (repo_root / "skills" / "shared").mkdir(parents=True)

            cache_root = repo_root / ".tracked-repos-cache" / "myrepo"
            cache_root.mkdir(parents=True)
            (cache_root / ".git").mkdir()
            (cache_root / "SKILL.md").write_text("# root", encoding="utf-8")
            (cache_root / "sub").mkdir()
            (cache_root / "sub" / "SKILL.md").write_text("# sub", encoding="utf-8")

            result = sync_skills.refresh_tracked_source_catalog(
                actual_repo_root=repo_root,
                catalog_repo_root=repo_root,
                source_name="myrepo",
                source_cfg={
                    "repo": "https://example.com/repo.git",
                    "ref": "main",
                    "skill_map": {
                        "myrepo": {"source_path": "."},
                        "myrepo-sub": {"source_path": "sub"},
                    },
                },
                runner=self._make_runner(),
                dry_run=False,
            )

            self.assertEqual(result["skills_found"], 2)
            self.assertTrue((repo_root / "sources" / "tracked__myrepo" / "imprint" / "SKILL.md").is_file())
            self.assertTrue((repo_root / "skills" / "shared" / "myrepo" / "SKILL.md").is_file())
            self.assertTrue((repo_root / "skills" / "shared" / "myrepo-sub" / "SKILL.md").is_file())
            registry = sync_skills.load_json(repo_root / "config" / "tracked-skill-sources.json")
            self.assertIn("shared/myrepo", registry["skills"])
            self.assertIn("shared/myrepo-sub", registry["skills"])

    def test_refresh_tracked_source_catalog_removes_stale_generated_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = root / "repo"
            (repo_root / "config").mkdir(parents=True)
            (repo_root / "skills" / "shared").mkdir(parents=True)

            stale = make_skill(repo_root / "skills" / "shared", "myrepo-old", "# old\n")
            self.assertTrue(stale.exists())
            sync_skills.write_json(
                repo_root / "config" / "tracked-skill-sources.local.json",
                {
                    "version": 1,
                    "skills": {
                        "shared/myrepo-old": {
                            "name": "myrepo-old",
                            "bucket": "shared",
                            "dest": "skills/shared/myrepo-old",
                            "scope": "repo",
                            "source_type": "tracked_repo",
                            "source": {
                                "repo": "https://example.com/repo.git",
                                "ref": "main",
                                "path": "old",
                                "source_name": "myrepo",
                            },
                            "resolved_revision": "old",
                            "installed_at": "2026-03-24T00:00:00",
                            "updated_at": "2026-03-24T00:00:00",
                        }
                    },
                },
            )

            cache_root = repo_root / ".tracked-repos-cache" / "myrepo"
            cache_root.mkdir(parents=True)
            (cache_root / ".git").mkdir()
            (cache_root / "SKILL.md").write_text("# root", encoding="utf-8")

            result = sync_skills.refresh_tracked_source_catalog(
                actual_repo_root=repo_root,
                catalog_repo_root=repo_root,
                source_name="myrepo",
                source_cfg={
                    "repo": "https://example.com/repo.git",
                    "ref": "main",
                    "skill_map": {
                        "myrepo": {"source_path": "."},
                    },
                },
                runner=self._make_runner(),
                dry_run=False,
            )

            self.assertEqual([item["key"] for item in result["stale_outputs"]], ["shared/myrepo-old"])
            self.assertFalse((repo_root / "skills" / "shared" / "myrepo-old").exists())

    def test_load_tracked_source_registry_supports_legacy_local_filename(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            repo_root = Path(root_dir) / "repo"
            (repo_root / "config").mkdir(parents=True)
            sync_skills.write_json(
                repo_root / "config" / "tracked-skill-sources.local.json",
                {
                    "version": 1,
                    "skills": {
                        "shared/legacy": {
                            "name": "legacy",
                            "bucket": "shared",
                            "dest": "skills/shared/legacy",
                            "scope": "repo",
                            "source_type": "tracked_repo",
                            "source": {"repo": "owner/repo", "ref": "main", "path": ".", "source_name": "legacy"},
                            "resolved_revision": "abc123",
                            "installed_at": "2026-03-24T00:00:00",
                            "updated_at": "2026-03-24T00:00:00",
                        }
                    },
                },
            )

            registry = sync_skills.load_tracked_source_registry(
                sync_skills.tracked_source_registry_path(repo_root)
            )

            self.assertIn("shared/legacy", registry["skills"])

    def test_refresh_tracked_source_catalog_rejects_key_collisions_with_existing_sources(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = root / "repo"
            (repo_root / "config").mkdir(parents=True)
            (repo_root / "skills" / "shared").mkdir(parents=True)

            existing = make_skill(repo_root / "skills" / "shared", "foo", "# vendored\n")
            self.assertTrue(existing.exists())
            sync_skills.write_json(
                repo_root / "config" / "skill-sources.json",
                {
                    "version": 1,
                    "skills": {
                        "shared/foo": {
                            "name": "foo",
                            "bucket": "shared",
                            "dest": "skills/shared/foo",
                            "scope": "repo",
                            "source_type": "github",
                            "source": {"repo": "owner/old", "path": "skills/foo", "ref": "main"},
                            "resolved_revision": "oldrev",
                            "installed_at": "2026-03-24T00:00:00",
                            "updated_at": "2026-03-24T00:00:00",
                        }
                    },
                },
            )

            cache_root = repo_root / ".tracked-repos-cache" / "myrepo"
            cache_root.mkdir(parents=True)
            (cache_root / ".git").mkdir()
            (cache_root / "foo").mkdir()
            (cache_root / "foo" / "SKILL.md").write_text("# tracked\n", encoding="utf-8")

            with self.assertRaises(sync_skills.SourceError):
                sync_skills.refresh_tracked_source_catalog(
                    actual_repo_root=repo_root,
                    catalog_repo_root=repo_root,
                    source_name="myrepo",
                    source_cfg={
                        "repo": "https://example.com/repo.git",
                        "ref": "main",
                        "skill_map": {"foo": {"source_path": "foo", "bucket": "shared"}},
                    },
                    runner=self._make_runner(),
                    dry_run=False,
                )

            self.assertEqual((repo_root / "skills" / "shared" / "foo" / "SKILL.md").read_text(encoding="utf-8"), "# vendored\n")


class TrackedSourceDiscoveryTests(unittest.TestCase):
    def test_discover_tracked_skill_entries_uses_recognized_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            repo_root = Path(root_dir) / "source"
            make_skill(repo_root / "skills", "configure-ecc")
            make_skill(repo_root / ".claude" / "skills", "everything-claude-code")

            result = sync_skills.discover_tracked_skill_entries(
                repo_root=repo_root,
                source_name="superpowers",
                source_cfg={
                    "repo": "owner/repo",
                    "ref": "main",
                },
                resolved_revision="abc123",
            )

            self.assertEqual(result["configure-ecc"]["source_path"], "skills/configure-ecc")
            self.assertEqual(result["configure-ecc"]["bucket"], "shared")
            self.assertEqual(result["everything-claude-code"]["source_path"], ".claude/skills/everything-claude-code")
            self.assertEqual(result["everything-claude-code"]["bucket"], "claude")

    def test_discover_tracked_skill_entries_supports_top_level_pack_layout(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            repo_root = Path(root_dir) / "source"
            repo_root.mkdir(parents=True)
            (repo_root / "SKILL.md").write_text("# root\n", encoding="utf-8")
            make_skill(repo_root, "browse")
            make_skill(repo_root, "qa")

            result = sync_skills.discover_tracked_skill_entries(
                repo_root=repo_root,
                source_name="gstack",
                source_cfg={
                    "repo": "owner/repo",
                    "ref": "main",
                    "prefix": "gstack-",
                    "root_name": "gstack",
                },
                resolved_revision="abc123",
            )

            self.assertEqual(result["gstack"]["source_path"], ".")
            self.assertEqual(result["gstack-browse"]["source_path"], "browse")
            self.assertEqual(result["gstack-qa"]["source_path"], "qa")


class TrackedSourceCLITests(unittest.TestCase):
    def test_update_sources_check_uses_preview_catalog_for_deploy_state(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            target_root = root / "target"
            target_root.mkdir()
            repo_root = make_cli_repo(root, target_root)
            host = sync_skills.detect_host()

            config = sync_skills.load_json(repo_root / "config" / "targets.local.json")
            config["tracked_repos"] = {
                "myrepo": {
                    "repo": "https://example.com/repo.git",
                    "ref": "main",
                    "skill_map": {
                        "myrepo": {"source_path": ".", "bucket": "shared"},
                    },
                }
            }
            sync_skills.write_json(repo_root / "config" / "targets.local.json", config)

            cache_root = repo_root / ".tracked-repos-cache" / "myrepo"
            cache_root.mkdir(parents=True)
            (cache_root / "SKILL.md").write_text("# tracked\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=cache_root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=cache_root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=cache_root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "add", "SKILL.md"], cwd=cache_root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=cache_root, check=True, capture_output=True, text=True)

            run_cli(repo_root, "--update-sources", "--check", "--host", host)

            deploy_state = sync_skills.load_json(repo_root / "config" / "deploy-state.local.json")
            self.assertIn("shared/myrepo", deploy_state["targets"]["windows_codex"]["skills"])


class PullPlanTests(unittest.TestCase):
    def test_pull_plan_only_includes_valid_non_hidden_skills(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as live_dir:
            repo_root = Path(repo_dir)
            live_root = Path(live_dir)
            make_skill(live_root, "alpha")
            make_skill(live_root / ".system", "ignored-system-skill")
            (live_root / "plain-folder").mkdir()

            config = sample_config(live_root)
            plan = sync_skills.plan_pull_target(
                repo_root,
                config,
                "windows_codex",
                config["targets"]["windows_codex"],
                "windows",
            )

            self.assertEqual(plan["add"], ["alpha"])
            self.assertEqual(plan["conflict"], [])
            self.assertEqual(plan["unchanged"], [])

    def test_apply_pull_target_copies_new_skill_into_repo_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as live_dir:
            repo_root = Path(repo_dir)
            live_root = Path(live_dir)
            skill_dir = make_skill(live_root, "alpha", "# Alpha\n")
            (skill_dir / "notes.txt").write_text("hello", encoding="utf-8")

            config = sample_config(live_root)
            plan = sync_skills.plan_pull_target(
                repo_root,
                config,
                "windows_codex",
                config["targets"]["windows_codex"],
                "windows",
            )
            sync_skills.apply_pull_target(plan)

            imported = repo_root / "skills" / "codex" / "alpha"
            self.assertTrue((imported / "SKILL.md").is_file())
            self.assertTrue((imported / "notes.txt").is_file())

    def test_pull_plan_marks_identical_repo_skill_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as live_dir:
            repo_root = Path(repo_dir)
            live_root = Path(live_dir)
            make_skill(live_root, "alpha", "# Same\n")
            make_skill(repo_root / "skills" / "codex", "alpha", "# Same\n")

            config = sample_config(live_root)
            plan = sync_skills.plan_pull_target(
                repo_root,
                config,
                "windows_codex",
                config["targets"]["windows_codex"],
                "windows",
            )

            self.assertEqual(plan["add"], [])
            self.assertEqual(plan["conflict"], [])
            self.assertEqual(plan["unchanged"], ["alpha"])

    def test_pull_plan_marks_different_repo_skill_as_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as live_dir:
            repo_root = Path(repo_dir)
            live_root = Path(live_dir)
            make_skill(live_root, "alpha", "# Live\n")
            make_skill(repo_root / "skills" / "codex", "alpha", "# Repo\n")

            config = sample_config(live_root)
            plan = sync_skills.plan_pull_target(
                repo_root,
                config,
                "windows_codex",
                config["targets"]["windows_codex"],
                "windows",
            )

            self.assertEqual(plan["add"], [])
            self.assertEqual(plan["conflict"], ["alpha"])
            self.assertEqual(plan["unchanged"], [])


class PushBackupTests(unittest.TestCase):
    def test_plan_push_target_collects_harness_specific_agents(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = root / "repo"
            target_skills_root = root / "target" / "skills"
            (repo_root / "skills" / "codex").mkdir(parents=True, exist_ok=True)
            make_agent(repo_root / ".codex" / "agents", "reviewer.toml", "[agent]\n")
            target_skills_root.mkdir(parents=True, exist_ok=True)

            config = sample_config(target_skills_root)
            plan = sync_skills.plan_push_target(
                repo_root,
                config,
                "windows_codex",
                config["targets"]["windows_codex"],
                "windows",
            )

            assert plan is not None
            self.assertEqual(plan["agent_add"], ["reviewer.toml"])
            self.assertEqual(plan["agent_update"], [])
            self.assertEqual(plan["agent_remove"], [])
            self.assertTrue(plan["codex_config"]["update_needed"])

    def test_apply_target_syncs_codex_agents_and_registers_target_config(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = root / "repo"
            target_skills_root = root / "target" / "skills"
            (repo_root / "skills" / "codex").mkdir(parents=True, exist_ok=True)
            make_agent(repo_root / ".codex" / "agents", "reviewer.toml", "[agent]\nname = \"reviewer\"\n")
            target_skills_root.mkdir(parents=True, exist_ok=True)

            config = sample_config(target_skills_root)
            plan = sync_skills.plan_push_target(
                repo_root,
                config,
                "windows_codex",
                config["targets"]["windows_codex"],
                "windows",
            )
            assert plan is not None

            result = sync_skills.apply_target(plan, clean=False, backup=True, ticket="ticket-agent-codex")

            target_agent = target_skills_root.parent / "agents" / "reviewer.toml"
            target_config = target_skills_root.parent / "config.toml"
            self.assertTrue(target_agent.is_file())
            self.assertIn(sync_skills.MANAGED_AGENTS_BEGIN, target_config.read_text(encoding="utf-8"))
            self.assertIn("[agents.reviewer]", target_config.read_text(encoding="utf-8"))
            self.assertEqual(result["ticket"], "ticket-agent-codex")
            ticket_file = target_skills_root / ".skill-sync-tickets" / "ticket-agent-codex" / "ticket.json"
            metadata = sync_skills.load_json(ticket_file)
            self.assertEqual(metadata["added_agents"], ["reviewer.toml"])
            self.assertTrue(metadata["codex_config_changed"])

    def test_apply_target_syncs_claude_agents_without_settings_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = root / "repo"
            target_skills_root = root / "target" / "skills"
            target_skills_root.mkdir(parents=True, exist_ok=True)
            (repo_root / "skills" / "claude").mkdir(parents=True, exist_ok=True)
            make_agent(repo_root / ".claude" / "agents", "researcher.md", "---\nname: researcher\n---\n")

            config = sample_config(target_skills_root)
            config["targets"]["windows_codex"]["kind"] = "claude"
            plan = sync_skills.plan_push_target(
                repo_root,
                config,
                "windows_codex",
                config["targets"]["windows_codex"],
                "windows",
            )
            assert plan is not None

            sync_skills.apply_target(plan, clean=False, backup=True, ticket="ticket-agent-claude")

            target_agent = target_skills_root.parent / "agents" / "researcher.md"
            self.assertTrue(target_agent.is_file())
            self.assertFalse((target_skills_root.parent / "settings.json").exists())

    def test_apply_target_backs_up_updated_skill_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            target_root = root / "target"
            source_root = root / "source"
            target_root.mkdir()
            source_root.mkdir()

            live_skill = make_skill(target_root, "alpha", "# Live\n")
            (live_skill / "notes.txt").write_text("live", encoding="utf-8")
            source_skill = make_skill(source_root, "alpha", "# Repo\n")
            (source_skill / "notes.txt").write_text("repo", encoding="utf-8")

            plan = {
                "root": str(target_root),
                "manifest": str(target_root / ".skill-sync-manifest.json"),
                "id": "windows_codex",
                "kind": "codex",
                "desired": ["alpha"],
                "add": [],
                "update": ["alpha"],
                "remove": [],
                "source_skills": {"alpha": str(source_skill)},
            }

            result = sync_skills.apply_target(plan, clean=False, backup=True, ticket="ticket-123")

            self.assertIsNotNone(result["backup_root"])
            self.assertEqual(result["ticket"], "ticket-123")
            current_skill = target_root / "alpha"
            self.assertEqual((current_skill / "notes.txt").read_text(encoding="utf-8"), "repo")

            backup_root = Path(result["backup_root"])
            backup_skill = backup_root / "alpha"
            self.assertTrue((backup_skill / "SKILL.md").is_file())
            self.assertEqual((backup_skill / "notes.txt").read_text(encoding="utf-8"), "live")
            ticket_file = target_root / ".skill-sync-tickets" / "ticket-123" / "ticket.json"
            self.assertTrue(ticket_file.is_file())

    def test_apply_target_backs_up_removed_skill_when_cleaning(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            target_root = root / "target"
            target_root.mkdir()
            remove_skill = make_skill(target_root, "obsolete", "# Old\n")
            (remove_skill / "notes.txt").write_text("old", encoding="utf-8")

            plan = {
                "root": str(target_root),
                "manifest": str(target_root / ".skill-sync-manifest.json"),
                "id": "windows_codex",
                "kind": "codex",
                "desired": [],
                "add": [],
                "update": [],
                "remove": ["obsolete"],
                "source_skills": {},
            }

            result = sync_skills.apply_target(plan, clean=True, backup=True, ticket="ticket-456")

            self.assertFalse((target_root / "obsolete").exists())
            backup_root = Path(result["backup_root"])
            self.assertTrue((backup_root / "obsolete" / "SKILL.md").is_file())

    def test_apply_target_can_skip_backups_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            target_root = root / "target"
            source_root = root / "source"
            target_root.mkdir()
            source_root.mkdir()

            make_skill(target_root, "alpha", "# Live\n")
            source_skill = make_skill(source_root, "alpha", "# Repo\n")

            plan = {
                "root": str(target_root),
                "manifest": str(target_root / ".skill-sync-manifest.json"),
                "id": "windows_codex",
                "kind": "codex",
                "desired": ["alpha"],
                "add": [],
                "update": ["alpha"],
                "remove": [],
                "source_skills": {"alpha": str(source_skill)},
            }

            result = sync_skills.apply_target(plan, clean=False, backup=False, ticket="ticket-789")

            self.assertIsNone(result["backup_root"])
            self.assertEqual(result["ticket"], "ticket-789")
            self.assertFalse((target_root / ".skill-sync-backups").exists())
            ticket_file = target_root / ".skill-sync-tickets" / "ticket-789" / "ticket.json"
            self.assertTrue(ticket_file.is_file())

    def test_apply_rollback_target_restores_predeploy_state(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            target_root = root / "target"
            source_root = root / "source"
            target_root.mkdir()
            source_root.mkdir()

            live_alpha = make_skill(target_root, "alpha", "# Live alpha\n")
            (live_alpha / "notes.txt").write_text("old-alpha", encoding="utf-8")
            live_beta = make_skill(target_root, "beta", "# Live beta\n")
            (live_beta / "notes.txt").write_text("old-beta", encoding="utf-8")

            source_alpha = make_skill(source_root, "alpha", "# Repo alpha\n")
            (source_alpha / "notes.txt").write_text("new-alpha", encoding="utf-8")
            source_gamma = make_skill(source_root, "gamma", "# Repo gamma\n")

            plan = {
                "root": str(target_root),
                "manifest": str(target_root / ".skill-sync-manifest.json"),
                "id": "windows_codex",
                "kind": "codex",
                "desired": ["alpha", "gamma"],
                "add": ["gamma"],
                "update": ["alpha"],
                "remove": ["beta"],
                "source_skills": {
                    "alpha": str(source_alpha),
                    "gamma": str(source_gamma),
                },
            }

            sync_skills.apply_target(plan, clean=True, backup=True, ticket="ticket-rollback")
            rollback_plan = {
                "root": str(target_root),
                "metadata_path": str(target_root / ".skill-sync-tickets" / "ticket-rollback" / "ticket.json"),
                "manifest": str(target_root / ".skill-sync-manifest.json"),
                "added": ["gamma"],
                "updated": ["alpha"],
                "removed": ["beta"],
                "backed_up": ["alpha", "beta"],
                "previous_manifest": None,
            }

            sync_skills.apply_rollback_target(rollback_plan)

            self.assertFalse((target_root / "gamma").exists())
            self.assertEqual((target_root / "alpha" / "notes.txt").read_text(encoding="utf-8"), "old-alpha")
            self.assertEqual((target_root / "beta" / "notes.txt").read_text(encoding="utf-8"), "old-beta")
            self.assertFalse((target_root / ".skill-sync-manifest.json").exists())

    def test_apply_rollback_target_restores_codex_agents_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = root / "repo"
            target_skills_root = root / "target" / "skills"
            target_skills_root.mkdir(parents=True, exist_ok=True)
            (repo_root / "skills" / "codex").mkdir(parents=True, exist_ok=True)
            make_agent(repo_root / ".codex" / "agents", "docs.toml", "[agent]\nname = \"docs\"\n")

            existing_agent = make_agent(target_skills_root.parent / "agents", "reviewer.toml", "[agent]\nname = \"reviewer\"\n")
            sync_skills.write_json(
                target_skills_root / ".skill-sync-manifest.json",
                {
                    "version": 1,
                    "target": "windows_codex",
                    "kind": "codex",
                    "skills": [],
                    "agents": ["reviewer.toml"],
                },
            )
            (target_skills_root.parent / "config.toml").write_text(
                "\n".join(
                    [
                        sync_skills.MANAGED_AGENTS_BEGIN,
                        "[agents.reviewer]",
                        'path = ".codex/agents/reviewer.toml"',
                        sync_skills.MANAGED_AGENTS_END,
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            config = sample_config(target_skills_root)
            plan = sync_skills.plan_push_target(
                repo_root,
                config,
                "windows_codex",
                config["targets"]["windows_codex"],
                "windows",
            )
            assert plan is not None

            sync_skills.apply_target(plan, clean=True, backup=True, ticket="ticket-agent-rollback")
            rollback_plan = sync_skills.plan_rollback_target(
                config,
                "windows_codex",
                config["targets"]["windows_codex"],
                "windows",
                ticket="ticket-agent-rollback",
            )
            assert rollback_plan is not None
            sync_skills.apply_rollback_target(rollback_plan)

            self.assertTrue(existing_agent.is_file())
            self.assertFalse((target_skills_root.parent / "agents" / "docs.toml").exists())
            config_text = (target_skills_root.parent / "config.toml").read_text(encoding="utf-8")
            self.assertIn("[agents.reviewer]", config_text)
            self.assertNotIn("[agents.docs]", config_text)


class TicketFlowTests(unittest.TestCase):
    def test_plan_rollback_target_returns_none_for_missing_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            target_root = Path(root_dir) / "target"
            target_root.mkdir()
            config = sample_config(target_root)

            plan = sync_skills.plan_rollback_target(
                config,
                "windows_codex",
                config["targets"]["windows_codex"],
                "windows",
                ticket="missing-ticket",
            )

            self.assertIsNone(plan)

    def test_plan_rollback_target_reads_saved_ticket_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            target_root = root / "target"
            source_root = root / "source"
            target_root.mkdir()
            source_root.mkdir()

            old_manifest = {
                "version": 1,
                "target": "windows_codex",
                "kind": "codex",
                "skills": ["alpha"],
            }
            sync_skills.write_json(target_root / ".skill-sync-manifest.json", old_manifest)

            make_skill(target_root, "alpha", "# Live\n")
            source_alpha = make_skill(source_root, "alpha", "# Repo\n")

            plan = {
                "root": str(target_root),
                "manifest": str(target_root / ".skill-sync-manifest.json"),
                "id": "windows_codex",
                "kind": "codex",
                "desired": ["alpha"],
                "add": [],
                "update": ["alpha"],
                "remove": [],
                "source_skills": {"alpha": str(source_alpha)},
            }

            sync_skills.apply_target(plan, clean=False, backup=True, ticket="ticket-plan")
            config = sample_config(target_root)
            rollback_plan = sync_skills.plan_rollback_target(
                config,
                "windows_codex",
                config["targets"]["windows_codex"],
                "windows",
                ticket="ticket-plan",
            )

            self.assertIsNotNone(rollback_plan)
            assert rollback_plan is not None
            self.assertEqual(rollback_plan["ticket"], "ticket-plan")
            self.assertEqual(rollback_plan["updated"], ["alpha"])
            self.assertEqual(rollback_plan["added"], [])
            self.assertEqual(rollback_plan["removed"], [])
            self.assertTrue(rollback_plan["rollback_ready"])
            self.assertEqual(rollback_plan["previous_manifest"], old_manifest)

    def test_add_only_ticket_can_roll_back_without_backups(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            target_root = root / "target"
            source_root = root / "source"
            target_root.mkdir()
            source_root.mkdir()

            previous_manifest = {
                "version": 1,
                "target": "windows_codex",
                "kind": "codex",
                "skills": [],
            }
            sync_skills.write_json(target_root / ".skill-sync-manifest.json", previous_manifest)

            source_gamma = make_skill(source_root, "gamma", "# Repo gamma\n")
            plan = {
                "root": str(target_root),
                "manifest": str(target_root / ".skill-sync-manifest.json"),
                "id": "windows_codex",
                "kind": "codex",
                "desired": ["gamma"],
                "add": ["gamma"],
                "update": [],
                "remove": [],
                "source_skills": {"gamma": str(source_gamma)},
            }

            result = sync_skills.apply_target(plan, clean=False, backup=False, ticket="ticket-add-only")
            self.assertIsNone(result["backup_root"])
            config = sample_config(target_root)
            rollback_plan = sync_skills.plan_rollback_target(
                config,
                "windows_codex",
                config["targets"]["windows_codex"],
                "windows",
                ticket="ticket-add-only",
            )

            assert rollback_plan is not None
            self.assertTrue(rollback_plan["rollback_ready"])
            self.assertEqual(rollback_plan["added"], ["gamma"])
            self.assertEqual(rollback_plan["backed_up"], [])

            sync_skills.apply_rollback_target(rollback_plan)
            self.assertFalse((target_root / "gamma").exists())
            self.assertEqual(sync_skills.load_manifest(target_root / ".skill-sync-manifest.json"), previous_manifest)


class DeployStateTests(unittest.TestCase):
    def test_refresh_deploy_state_records_source_revision_and_target_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = root / "repo"
            target_root = root / "target"
            (repo_root / "config").mkdir(parents=True, exist_ok=True)
            (repo_root / "skills" / "shared").mkdir(parents=True, exist_ok=True)
            target_root.mkdir()

            source_skill = make_skill(repo_root / "skills" / "shared", "gh-skill", "# Shared skill\n")
            (source_skill / "notes.txt").write_text("repo-v1", encoding="utf-8")
            target_skill = make_skill(target_root, "gh-skill", "# Shared skill\n")
            (target_skill / "notes.txt").write_text("repo-v1", encoding="utf-8")

            sync_skills.write_json(
                repo_root / "config" / "skill-sources.json",
                {
                    "version": 1,
                    "skills": {
                        "shared/gh-skill": {
                            "name": "gh-skill",
                            "bucket": "shared",
                            "dest": "skills/shared/gh-skill",
                            "scope": "repo",
                            "source_type": "github",
                            "source": {
                                "repo": "owner/repo",
                                "path": "skills/shared/gh-skill",
                                "ref": "main",
                            },
                            "resolved_revision": "abc123",
                            "installed_at": "2026-03-20T00:00:00",
                            "updated_at": "2026-03-20T00:00:00",
                        }
                    },
                },
            )

            config = sample_config(target_root)
            sync_skills.write_json(
                target_root / ".skill-sync-manifest.json",
                {
                    "version": 1,
                    "target": "windows_codex",
                    "kind": "codex",
                    "skills": ["gh-skill"],
                },
            )

            state = sync_skills.refresh_deploy_state(
                repo_root=repo_root,
                config=config,
                target_ids=["windows_codex"],
                host="windows",
                action="apply",
                ticket="ticket-state",
            )

            skill_state = state["targets"]["windows_codex"]["skills"]["shared/gh-skill"]
            self.assertEqual(skill_state["source_type"], "github")
            self.assertEqual(skill_state["source"]["repo"], "owner/repo")
            self.assertEqual(skill_state["source_resolved_revision"], "abc123")
            self.assertEqual(skill_state["status"], "up_to_date")
            self.assertTrue(skill_state["deployed_to_target"])
            self.assertTrue(skill_state["target_up_to_date"])

    def test_refresh_deploy_state_marks_target_stale_after_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = root / "repo"
            target_root = root / "target"
            (repo_root / "config").mkdir(parents=True, exist_ok=True)
            (repo_root / "skills" / "codex").mkdir(parents=True, exist_ok=True)
            target_root.mkdir()

            source_alpha = make_skill(repo_root / "skills" / "codex", "alpha", "# Repo alpha\n")
            (source_alpha / "notes.txt").write_text("repo-v2", encoding="utf-8")
            source_gamma = make_skill(repo_root / "skills" / "codex", "gamma", "# Repo gamma\n")

            target_alpha = make_skill(target_root, "alpha", "# Live alpha\n")
            (target_alpha / "notes.txt").write_text("repo-v1", encoding="utf-8")
            target_beta = make_skill(target_root, "beta", "# Live beta\n")
            (target_beta / "notes.txt").write_text("repo-v1", encoding="utf-8")
            sync_skills.write_json(
                target_root / ".skill-sync-manifest.json",
                {
                    "version": 1,
                    "target": "windows_codex",
                    "kind": "codex",
                    "skills": ["alpha", "beta"],
                },
            )

            plan = {
                "root": str(target_root),
                "manifest": str(target_root / ".skill-sync-manifest.json"),
                "id": "windows_codex",
                "kind": "codex",
                "desired": ["alpha", "gamma"],
                "add": ["gamma"],
                "update": ["alpha"],
                "remove": ["beta"],
                "source_skills": {
                    "alpha": str(source_alpha),
                    "gamma": str(source_gamma),
                },
            }

            sync_skills.apply_target(plan, clean=True, backup=True, ticket="ticket-rollback-state")
            rollback_plan = {
                "root": str(target_root),
                "metadata_path": str(target_root / ".skill-sync-tickets" / "ticket-rollback-state" / "ticket.json"),
                "manifest": str(target_root / ".skill-sync-manifest.json"),
                "added": ["gamma"],
                "updated": ["alpha"],
                "removed": ["beta"],
                "backed_up": ["alpha", "beta"],
                "previous_manifest": {
                    "version": 1,
                    "target": "windows_codex",
                    "kind": "codex",
                    "skills": ["alpha", "beta"],
                },
            }
            sync_skills.apply_rollback_target(rollback_plan)

            config = sample_config(target_root)
            state = sync_skills.refresh_deploy_state(
                repo_root=repo_root,
                config=config,
                target_ids=["windows_codex"],
                host="windows",
                action="rollback",
                ticket="ticket-rollback-state",
            )

            target_state = state["targets"]["windows_codex"]["skills"]
            self.assertEqual(target_state["codex/alpha"]["status"], "out_of_date")
            self.assertFalse(target_state["codex/alpha"]["target_up_to_date"])
            self.assertEqual(target_state["codex/gamma"]["status"], "missing")
            self.assertFalse(target_state["codex/gamma"]["deployed_to_target"])


class CLITests(unittest.TestCase):
    def test_cli_apply_prints_ticket_uuid_and_writes_ticket_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            target_root = root / "target"
            target_root.mkdir()
            repo_root = make_cli_repo(root, target_root)

            make_skill(repo_root / "skills" / "codex", "alpha", "# Repo alpha\n")
            make_skill(target_root, "alpha", "# Live alpha\n")

            host = sync_skills.detect_host()
            result = run_cli(repo_root, "--apply", "--host", host)
            match = re.search(r"Ticket: ([0-9a-f-]{36})", result.stdout)

            self.assertIsNotNone(match, result.stdout)
            assert match is not None
            ticket = match.group(1)
            self.assertEqual(str(uuid.UUID(ticket)), ticket)
            self.assertIn(
                f"Skill backups for windows_codex: {target_root / '.skill-sync-tickets' / ticket / 'skills'}",
                result.stdout,
            )
            self.assertTrue((target_root / ".skill-sync-tickets" / ticket / "ticket.json").is_file())

    def test_cli_rollback_prints_completion_message_and_restores_state(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            target_root = root / "target"
            target_root.mkdir()
            repo_root = make_cli_repo(root, target_root)

            live_alpha = make_skill(target_root, "alpha", "# Live alpha\n")
            (live_alpha / "notes.txt").write_text("old-alpha", encoding="utf-8")
            sync_skills.write_json(
                target_root / ".skill-sync-manifest.json",
                {
                    "version": 1,
                    "target": "windows_codex",
                    "kind": "codex",
                    "skills": ["alpha"],
                },
            )

            source_alpha = make_skill(repo_root / "skills" / "codex", "alpha", "# Repo alpha\n")
            (source_alpha / "notes.txt").write_text("new-alpha", encoding="utf-8")
            make_skill(repo_root / "skills" / "codex", "gamma", "# Repo gamma\n")

            host = sync_skills.detect_host()
            deploy = run_cli(repo_root, "--apply", "--host", host)
            match = re.search(r"Ticket: ([0-9a-f-]{36})", deploy.stdout)
            self.assertIsNotNone(match, deploy.stdout)
            assert match is not None
            ticket = match.group(1)

            self.assertTrue((target_root / "gamma").exists())
            self.assertEqual((target_root / "alpha" / "notes.txt").read_text(encoding="utf-8"), "new-alpha")

            rollback = run_cli(repo_root, "--rollback", ticket, "--apply", "--host", host)

            self.assertIn(f"Rollback complete for ticket {ticket}.", rollback.stdout)
            self.assertFalse((target_root / "gamma").exists())
            self.assertEqual((target_root / "alpha" / "notes.txt").read_text(encoding="utf-8"), "old-alpha")


class CrossRuntimePathTests(unittest.TestCase):
    def test_wsl_to_windows_c_drive(self) -> None:
        cfg = {"host": "windows", "path": "C:/Users/redme/.claude/skills"}
        result = sync_skills.target_root_for_runtime(cfg, "wsl")
        self.assertEqual(result, Path("/mnt/c/Users/redme/.claude/skills"))

    def test_wsl_to_windows_d_drive(self) -> None:
        cfg = {"host": "windows", "path": "D:/Projects/skills"}
        result = sync_skills.target_root_for_runtime(cfg, "wsl")
        self.assertEqual(result, Path("/mnt/d/Projects/skills"))

    def test_same_host_unchanged(self) -> None:
        cfg = {"host": "wsl", "path": "/home/redme/.claude/skills"}
        result = sync_skills.target_root_for_runtime(cfg, "wsl")
        self.assertEqual(result, Path("/home/redme/.claude/skills"))

    def test_windows_to_wsl_still_raises(self) -> None:
        cfg = {"host": "wsl", "path": "/home/redme/.claude/skills"}
        with self.assertRaises(sync_skills.SourceError):
            sync_skills.target_root_for_runtime(cfg, "windows")


class OwnerAwareManifestTests(unittest.TestCase):
    def _make_repo_and_target(self, tmp_dir: str) -> tuple:
        repo_root = Path(tmp_dir) / "repo"
        (repo_root / "skills" / "shared" / "my-skill").mkdir(parents=True)
        (repo_root / "skills" / "shared" / "my-skill" / "SKILL.md").write_text("---\nname: my-skill\n---\n")
        (repo_root / "skills" / "claude").mkdir(parents=True)
        (repo_root / "skills" / "codex").mkdir(parents=True)

        target_root = Path(tmp_dir) / "target"
        (target_root / "my-skill").mkdir(parents=True)
        (target_root / "my-skill" / "SKILL.md").write_text("---\nname: my-skill\n---\n")
        (target_root / "gstack-browse").mkdir(parents=True)
        (target_root / "gstack-browse" / "SKILL.md").write_text("---\nname: gstack-browse\n---\n")

        config = {
            "version": 1,
            "manifest_filename": ".skill-sync-manifest.json",
            "catalog": {"shared": "skills/shared", "codex": "skills/codex", "claude": "skills/claude"},
        }
        target_cfg = {"host": "wsl", "path": str(target_root), "kind": "claude", "enabled": True}
        return repo_root, target_root, config, target_cfg

    def test_v2_manifest_does_not_remove_unowned(self) -> None:
        import json
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root, target_root, config, target_cfg = self._make_repo_and_target(tmp_dir)
            manifest = {
                "version": 2, "target": "test", "kind": "claude",
                "skills": {"my-skill": {"owner": "sync"}, "gstack-browse": {}},
                "agents": []
            }
            (target_root / ".skill-sync-manifest.json").write_text(json.dumps(manifest))

            plan = sync_skills.plan_push_target(repo_root, config, "test", target_cfg, "wsl")
            self.assertNotIn("gstack-browse", plan["remove"])

    def test_v1_manifest_auto_migrates_unowned(self) -> None:
        import json
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root, target_root, config, target_cfg = self._make_repo_and_target(tmp_dir)
            manifest = {
                "version": 1, "target": "test", "kind": "claude",
                "skills": ["my-skill", "gstack-browse"], "agents": []
            }
            (target_root / ".skill-sync-manifest.json").write_text(json.dumps(manifest))

            plan = sync_skills.plan_push_target(repo_root, config, "test", target_cfg, "wsl")
            self.assertNotIn("gstack-browse", plan["remove"])
            self.assertTrue(
                "my-skill" in plan.get("unchanged", []) or "my-skill" in plan.get("update", [])
            )

    def test_build_manifest_v2_preserves_unowned(self) -> None:
        existing = {
            "version": 2, "skills": {"a": {"owner": "sync"}, "gstack": {}}, "agents": []
        }
        result = sync_skills.build_manifest_v2("t", "claude", ["a", "b"], existing)
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["skills"]["a"], {"owner": "sync"})
        self.assertEqual(result["skills"]["b"], {"owner": "sync"})
        self.assertEqual(result["skills"]["gstack"], {})
        self.assertNotIn("owner", result["skills"].get("gstack", {}))

    def test_build_manifest_v2_from_v1(self) -> None:
        existing = {"version": 1, "skills": ["a", "gstack", "gstack-browse"], "agents": []}
        result = sync_skills.build_manifest_v2("t", "claude", ["a"], existing)
        self.assertEqual(result["skills"]["a"], {"owner": "sync"})
        self.assertEqual(result["skills"]["gstack"], {})
        self.assertEqual(result["skills"]["gstack-browse"], {})


class ApplyTargetManifestV2Tests(unittest.TestCase):
    """Verify that apply_target writes v2 manifests with correct owner tracking."""

    def test_apply_target_writes_v2_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir) / "repo"
            (repo_root / "skills" / "shared" / "alpha").mkdir(parents=True)
            (repo_root / "skills" / "shared" / "alpha" / "SKILL.md").write_text("# alpha")
            (repo_root / "skills" / "claude").mkdir(parents=True)

            target_root = Path(tmp_dir) / "target"
            target_root.mkdir()

            config = sample_config(target_root)
            target_cfg = {"host": "wsl", "path": str(target_root), "kind": "claude", "enabled": True}

            plan = sync_skills.plan_push_target(repo_root, config, "test", target_cfg, "wsl")
            sync_skills.apply_target(plan, clean=False, backup=False)

            manifest_path = target_root / ".skill-sync-manifest.json"
            self.assertTrue(manifest_path.exists())
            import json
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], 2)
            self.assertIsInstance(manifest["skills"], dict)
            self.assertEqual(manifest["skills"]["alpha"], {"owner": "sync"})

    def test_apply_target_preserves_unowned_in_v2(self) -> None:
        """When applying over a v2 manifest with unowned skills, they stay."""
        import json
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir) / "repo"
            (repo_root / "skills" / "shared" / "alpha").mkdir(parents=True)
            (repo_root / "skills" / "shared" / "alpha" / "SKILL.md").write_text("# alpha")
            (repo_root / "skills" / "claude").mkdir(parents=True)

            target_root = Path(tmp_dir) / "target"
            (target_root / "alpha").mkdir(parents=True)
            (target_root / "alpha" / "SKILL.md").write_text("# alpha")
            (target_root / "external-skill").mkdir(parents=True)
            (target_root / "external-skill" / "SKILL.md").write_text("# external")

            # Pre-existing v2 manifest with an unowned skill
            manifest_path = target_root / ".skill-sync-manifest.json"
            manifest_path.write_text(json.dumps({
                "version": 2, "target": "test", "kind": "claude",
                "skills": {
                    "alpha": {"owner": "sync"},
                    "external-skill": {"owner": "tracked:foo"},
                },
                "agents": [],
            }))

            config = sample_config(target_root)
            target_cfg = {"host": "wsl", "path": str(target_root), "kind": "claude", "enabled": True}
            plan = sync_skills.plan_push_target(repo_root, config, "test", target_cfg, "wsl")
            sync_skills.apply_target(plan, clean=False, backup=False)

            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["version"], 2)
            self.assertEqual(updated["skills"]["alpha"], {"owner": "sync"})
            self.assertEqual(updated["skills"]["external-skill"], {"owner": "tracked:foo"})

    def test_apply_target_migrates_v1_to_v2(self) -> None:
        """When applying over a v1 manifest, it gets upgraded to v2."""
        import json
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir) / "repo"
            (repo_root / "skills" / "shared" / "alpha").mkdir(parents=True)
            (repo_root / "skills" / "shared" / "alpha" / "SKILL.md").write_text("# alpha")
            (repo_root / "skills" / "claude").mkdir(parents=True)

            target_root = Path(tmp_dir) / "target"
            (target_root / "alpha").mkdir(parents=True)
            (target_root / "alpha" / "SKILL.md").write_text("# alpha")
            (target_root / "gstack").mkdir(parents=True)
            (target_root / "gstack" / "SKILL.md").write_text("# gstack")

            manifest_path = target_root / ".skill-sync-manifest.json"
            manifest_path.write_text(json.dumps({
                "version": 1, "target": "test", "kind": "claude",
                "skills": ["alpha", "gstack"], "agents": [],
            }))

            config = sample_config(target_root)
            target_cfg = {"host": "wsl", "path": str(target_root), "kind": "claude", "enabled": True}
            plan = sync_skills.plan_push_target(repo_root, config, "test", target_cfg, "wsl")
            sync_skills.apply_target(plan, clean=False, backup=False)

            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["version"], 2)
            self.assertEqual(updated["skills"]["alpha"], {"owner": "sync"})
            # gstack was in v1 list but not in source — should become unowned
            self.assertEqual(updated["skills"]["gstack"], {})


class OwnedSkillsHelperTests(unittest.TestCase):
    """Test _owned_skills_from_manifest edge cases."""

    def test_v2_only_sync_owner_returned(self) -> None:
        manifest = {
            "skills": {
                "a": {"owner": "sync"},
                "b": {"owner": "tracked:gstack"},
                "c": {},
            }
        }
        result = sync_skills._owned_skills_from_manifest(manifest, ["a", "b", "c"])
        self.assertEqual(result, {"a"})

    def test_v1_only_matching_desired_returned(self) -> None:
        manifest = {"skills": ["a", "b", "gstack"]}
        result = sync_skills._owned_skills_from_manifest(manifest, ["a", "b"])
        self.assertEqual(result, {"a", "b"})
        self.assertNotIn("gstack", result)

    def test_empty_manifest(self) -> None:
        result = sync_skills._owned_skills_from_manifest({}, ["a"])
        self.assertEqual(result, set())

    def test_v2_with_tracked_owner_not_returned(self) -> None:
        manifest = {"skills": {"gstack": {"owner": "tracked:gstack"}, "gstack-browse": {"owner": "tracked:gstack"}}}
        result = sync_skills._owned_skills_from_manifest(manifest, [])
        self.assertEqual(result, set())


class BuildManifestV2EdgeCaseTests(unittest.TestCase):
    def test_empty_existing_manifest(self) -> None:
        result = sync_skills.build_manifest_v2("t", "claude", ["a", "b"], {})
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["skills"]["a"], {"owner": "sync"})
        self.assertEqual(result["skills"]["b"], {"owner": "sync"})
        self.assertEqual(len(result["skills"]), 2)

    def test_v2_removes_sync_owned_skill_not_in_desired(self) -> None:
        existing = {"skills": {"old": {"owner": "sync"}, "keep": {"owner": "tracked:x"}}}
        result = sync_skills.build_manifest_v2("t", "claude", ["new"], existing)
        self.assertNotIn("old", result["skills"])  # was sync-owned, no longer desired
        self.assertEqual(result["skills"]["keep"], {"owner": "tracked:x"})  # tracked, preserved
        self.assertEqual(result["skills"]["new"], {"owner": "sync"})

    def test_v1_with_no_overlap(self) -> None:
        """v1 manifest where none of the listed skills are in desired → all become unowned."""
        existing = {"skills": ["gstack", "gstack-browse"]}
        result = sync_skills.build_manifest_v2("t", "claude", ["my-skill"], existing)
        self.assertEqual(result["skills"]["gstack"], {})
        self.assertEqual(result["skills"]["gstack-browse"], {})
        self.assertEqual(result["skills"]["my-skill"], {"owner": "sync"})

    def test_agents_preserved_from_existing(self) -> None:
        existing = {"skills": {}, "agents": ["code-reviewer.md"]}
        result = sync_skills.build_manifest_v2("t", "claude", ["a"], existing)
        self.assertEqual(result["agents"], ["code-reviewer.md"])


class TrackedManifestOwnershipTests(unittest.TestCase):
    def test_apply_tracked_ownership_creates_manifest_and_marks_current_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_root = Path(tmp_dir) / "target"
            target_root.mkdir()

            updated = sync_skills.apply_tracked_ownership(
                target_id="windows_codex",
                target_kind="codex",
                manifest_path=target_root / ".skill-sync-manifest.json",
                target_root=target_root,
                source_name="gstack",
                skill_names=["gstack", "gstack-browse"],
                apply=True,
            )

            self.assertEqual(updated["removed"], [])
            manifest = sync_skills.load_manifest(target_root / ".skill-sync-manifest.json")
            self.assertEqual(manifest["skills"]["gstack"], {"owner": "tracked:gstack"})
            self.assertEqual(manifest["skills"]["gstack-browse"], {"owner": "tracked:gstack"})

    def test_apply_tracked_ownership_removes_stale_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_root = Path(tmp_dir) / "target"
            target_root.mkdir()
            stale_dir = make_skill(target_root, "gstack-old", "# old\n")
            current_dir = make_skill(target_root, "gstack", "# current\n")
            current_subdir = make_skill(target_root, "gstack-browse", "# browse\n")
            self.assertTrue(stale_dir.exists())
            self.assertTrue(current_dir.exists())
            self.assertTrue(current_subdir.exists())

            sync_skills.write_json(
                target_root / ".skill-sync-manifest.json",
                {
                    "version": 2,
                    "target": "windows_codex",
                    "kind": "codex",
                    "skills": {
                        "gstack": {"owner": "tracked:gstack"},
                        "gstack-old": {"owner": "tracked:gstack"},
                        "other": {"owner": "tracked:other"},
                    },
                    "agents": [],
                },
            )

            updated = sync_skills.apply_tracked_ownership(
                target_id="windows_codex",
                target_kind="codex",
                manifest_path=target_root / ".skill-sync-manifest.json",
                target_root=target_root,
                source_name="gstack",
                skill_names=["gstack", "gstack-browse"],
                apply=True,
            )

            self.assertEqual(updated["removed"], ["gstack-old"])
            self.assertFalse((target_root / "gstack-old").exists())
            manifest = sync_skills.load_manifest(target_root / ".skill-sync-manifest.json")
            self.assertNotIn("gstack-old", manifest["skills"])
            self.assertEqual(manifest["skills"]["gstack"], {"owner": "tracked:gstack"})
            self.assertEqual(manifest["skills"]["gstack-browse"], {"owner": "tracked:gstack"})
            self.assertEqual(manifest["skills"]["other"], {"owner": "tracked:other"})


if __name__ == "__main__":
    unittest.main()
