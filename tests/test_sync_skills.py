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
    sync_skills.write_json(repo_root / "config" / "targets.local.json", sample_config(target_path))
    return repo_root


def run_cli(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python", "scripts/sync_skills.py", *args],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=True,
    )


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

            result = run_cli(repo_root, "--apply", "--host", "windows")
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

            deploy = run_cli(repo_root, "--apply", "--host", "windows")
            match = re.search(r"Ticket: ([0-9a-f-]{36})", deploy.stdout)
            self.assertIsNotNone(match, deploy.stdout)
            assert match is not None
            ticket = match.group(1)

            self.assertTrue((target_root / "gamma").exists())
            self.assertEqual((target_root / "alpha" / "notes.txt").read_text(encoding="utf-8"), "new-alpha")

            rollback = run_cli(repo_root, "--rollback", ticket, "--apply", "--host", "windows")

            self.assertIn(f"Rollback complete for ticket {ticket}.", rollback.stdout)
            self.assertFalse((target_root / "gamma").exists())
            self.assertEqual((target_root / "alpha" / "notes.txt").read_text(encoding="utf-8"), "old-alpha")


if __name__ == "__main__":
    unittest.main()
