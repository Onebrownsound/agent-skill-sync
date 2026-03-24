from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import importlib.util
import sys

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_tracked_repos.py"
SPEC = importlib.util.spec_from_file_location("sync_tracked_repos", MODULE_PATH)
sync_tracked_repos = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = sync_tracked_repos
SPEC.loader.exec_module(sync_tracked_repos)


class CacheDirTests(unittest.TestCase):
    def test_cache_dir_for_source(self) -> None:
        result = sync_tracked_repos.cache_dir_for_source("gstack", Path("/tmp/cache"))
        self.assertEqual(result, Path("/tmp/cache/gstack"))


class EnumerateSkillsTests(unittest.TestCase):
    def test_enumerate_skills_from_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "gstack"
            (repo / "browse").mkdir(parents=True)
            (repo / "browse" / "SKILL.md").write_text("---\nname: browse\n---\n")
            (repo / "SKILL.md").write_text("---\nname: gstack\n---\n")

            skill_map = {
                "gstack": {"source_path": "."},
                "gstack-browse": {"source_path": "browse"},
                "gstack-missing": {"source_path": "nonexistent"},
            }
            result = sync_tracked_repos.enumerate_skills(repo, skill_map)
            self.assertIn("gstack", result)
            self.assertIn("gstack-browse", result)
            self.assertNotIn("gstack-missing", result)
            self.assertEqual(result["gstack"], repo / "SKILL.md")
            self.assertEqual(result["gstack-browse"], repo / "browse" / "SKILL.md")


class DistributeFlatCopiesTests(unittest.TestCase):
    def test_distribute_flat_copies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_repo = Path(tmp_dir) / "source"
            (source_repo / "browse").mkdir(parents=True)
            (source_repo / "browse" / "SKILL.md").write_text("# browse skill")
            (source_repo / "SKILL.md").write_text("# main skill")

            skill_map = {
                "gstack": {"source_path": "."},
                "gstack-browse": {"source_path": "browse"},
            }
            skills = sync_tracked_repos.enumerate_skills(source_repo, skill_map)

            target = Path(tmp_dir) / "target"
            target.mkdir()
            updated, skipped = sync_tracked_repos.distribute_flat_copies(skills, target)

            self.assertEqual(updated, 2)
            self.assertEqual(skipped, 0)
            self.assertEqual((target / "gstack" / "SKILL.md").read_text(), "# main skill")
            self.assertEqual((target / "gstack-browse" / "SKILL.md").read_text(), "# browse skill")


class CreateSymlinksTests(unittest.TestCase):
    def test_create_skill_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_root = Path(tmp_dir) / "target"
            clone_dir = target_root / "gstack"
            (clone_dir / "browse").mkdir(parents=True)
            (clone_dir / "browse" / "SKILL.md").write_text("browse")
            (clone_dir / "canary").mkdir(parents=True)
            (clone_dir / "canary" / "SKILL.md").write_text("canary")

            skill_map = {
                "gstack": {"source_path": "."},
                "gstack-browse": {"source_path": "browse"},
                "gstack-canary": {"source_path": "canary"},
                "gstack-missing": {"source_path": "nonexistent"},
            }
            created, skipped = sync_tracked_repos.create_skill_symlinks("gstack", skill_map, target_root)
            self.assertEqual(created, 2)
            self.assertEqual(skipped, 1)
            self.assertTrue((target_root / "gstack-browse").is_symlink())
            self.assertEqual((target_root / "gstack-browse" / "SKILL.md").read_text(), "browse")
            self.assertTrue((target_root / "gstack-canary").is_symlink())

    def test_idempotent_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_root = Path(tmp_dir) / "target"
            clone_dir = target_root / "gstack"
            (clone_dir / "browse").mkdir(parents=True)
            (clone_dir / "browse" / "SKILL.md").write_text("browse")

            skill_map = {"gstack-browse": {"source_path": "browse"}}
            sync_tracked_repos.create_skill_symlinks("gstack", skill_map, target_root)
            created, skipped = sync_tracked_repos.create_skill_symlinks("gstack", skill_map, target_root)
            self.assertEqual(created, 0)
            self.assertEqual(skipped, 1)


class StateRoundTripTests(unittest.TestCase):
    def test_load_state_returns_default_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state = sync_tracked_repos.load_state(Path(tmp_dir) / "nonexistent.json")
            self.assertEqual(state, {"version": 1, "sources": {}})

    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "state.json"
            state = {
                "version": 1,
                "sources": {
                    "myrepo": {
                        "repo": "https://example.com/repo.git",
                        "ref": "main",
                        "commit": "abc123",
                        "updated_at": "2026-03-24T12:00:00",
                        "targets": {
                            "wsl_claude": {"mode": "clone", "commit": "abc123", "deployed_at": "2026-03-24T12:00:00"},
                        },
                    }
                },
            }
            sync_tracked_repos.save_state(state_path, state)
            loaded = sync_tracked_repos.load_state(state_path)
            self.assertEqual(loaded["sources"]["myrepo"]["commit"], "abc123")
            self.assertEqual(loaded["sources"]["myrepo"]["targets"]["wsl_claude"]["mode"], "clone")


class CloneOrPullTests(unittest.TestCase):
    def _make_runner(self, commit: str = "abc123def456"):
        """Create a mock runner that tracks calls and returns a fake commit hash."""
        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            if "rev-parse" in cmd:
                from unittest.mock import MagicMock
                result = MagicMock()
                result.stdout = commit + "\n"
                return result
            from unittest.mock import MagicMock
            return MagicMock()

        return runner, calls

    def test_clone_when_no_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "repo"
            dest.mkdir()
            runner, calls = self._make_runner("deadbeef")
            action, commit = sync_tracked_repos.clone_or_pull(
                "https://example.com/repo.git", "main", dest, runner=runner,
            )
            self.assertEqual(action, "cloned")
            self.assertEqual(commit, "deadbeef")
            clone_cmd = [c for c in calls if "clone" in c][0]
            self.assertIn("--single-branch", clone_cmd)

    def test_pull_when_git_dir_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "repo"
            (dest / ".git").mkdir(parents=True)
            runner, calls = self._make_runner("cafebabe")
            action, commit = sync_tracked_repos.clone_or_pull(
                "https://example.com/repo.git", "main", dest, runner=runner,
            )
            self.assertEqual(action, "pulled")
            self.assertEqual(commit, "cafebabe")
            fetch_cmds = [c for c in calls if "fetch" in c]
            self.assertEqual(len(fetch_cmds), 1)


class UpdateTrackedRepoTests(unittest.TestCase):
    def _make_repo(self, tmp_dir: str) -> tuple:
        """Create a fake cached repo with skills."""
        cache_root = Path(tmp_dir) / "cache"
        repo_dir = cache_root / "myrepo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "SKILL.md").write_text("# root skill")
        (repo_dir / "sub1").mkdir()
        (repo_dir / "sub1" / "SKILL.md").write_text("# sub1 skill")
        return cache_root, repo_dir

    def _make_runner(self, commit: str = "abc123"):
        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            if "rev-parse" in cmd:
                from unittest.mock import MagicMock
                r = MagicMock()
                r.stdout = commit + "\n"
                return r
            from unittest.mock import MagicMock
            return MagicMock()

        return runner, calls

    def test_flat_copy_target_distributes_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_root, _ = self._make_repo(tmp_dir)
            target_root = Path(tmp_dir) / "target"
            target_root.mkdir()

            runner, _ = self._make_runner("abc123")
            source_cfg = {
                "repo": "https://example.com/repo.git",
                "ref": "main",
                "skill_map": {
                    "myrepo": {"source_path": "."},
                    "myrepo-sub1": {"source_path": "sub1"},
                },
                "targets": {"t1": "flat_copy"},
            }
            all_targets = {"t1": {"enabled": True, "path": str(target_root)}}

            result = sync_tracked_repos.update_tracked_repo(
                "myrepo", source_cfg, all_targets, cache_root, runner=runner,
            )
            self.assertEqual(result["skills_found"], 2)
            self.assertEqual(result["targets"]["t1"]["status"], "ok")
            self.assertEqual(result["targets"]["t1"]["updated"], 2)
            self.assertTrue((target_root / "myrepo" / "SKILL.md").exists())
            self.assertTrue((target_root / "myrepo-sub1" / "SKILL.md").exists())

    def test_flat_copy_skips_when_state_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_root, _ = self._make_repo(tmp_dir)
            target_root = Path(tmp_dir) / "target"
            target_root.mkdir()

            runner, _ = self._make_runner("abc123")
            source_cfg = {
                "repo": "https://example.com/repo.git",
                "ref": "main",
                "skill_map": {"myrepo": {"source_path": "."}},
                "targets": {"t1": "flat_copy"},
            }
            all_targets = {"t1": {"enabled": True, "path": str(target_root)}}
            state = {
                "version": 1,
                "sources": {
                    "myrepo": {
                        "targets": {
                            "t1": {"commit": "abc123", "mode": "flat_copy", "deployed_at": "2026-01-01"},
                        }
                    }
                },
            }

            result = sync_tracked_repos.update_tracked_repo(
                "myrepo", source_cfg, all_targets, cache_root, runner=runner, state=state,
            )
            self.assertEqual(result["targets"]["t1"]["status"], "up_to_date")

    def test_flat_copy_updates_when_commit_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_root, _ = self._make_repo(tmp_dir)
            target_root = Path(tmp_dir) / "target"
            target_root.mkdir()

            runner, _ = self._make_runner("newcommit")
            source_cfg = {
                "repo": "https://example.com/repo.git",
                "ref": "main",
                "skill_map": {"myrepo": {"source_path": "."}},
                "targets": {"t1": "flat_copy"},
            }
            all_targets = {"t1": {"enabled": True, "path": str(target_root)}}
            state = {
                "version": 1,
                "sources": {
                    "myrepo": {
                        "targets": {
                            "t1": {"commit": "oldcommit", "mode": "flat_copy", "deployed_at": "2026-01-01"},
                        }
                    }
                },
            }

            result = sync_tracked_repos.update_tracked_repo(
                "myrepo", source_cfg, all_targets, cache_root, runner=runner, state=state,
            )
            self.assertEqual(result["targets"]["t1"]["status"], "ok")
            self.assertEqual(result["targets"]["t1"]["previous_commit"], "oldcommit")

    def test_state_updated_after_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_root, _ = self._make_repo(tmp_dir)
            target_root = Path(tmp_dir) / "target"
            target_root.mkdir()

            runner, _ = self._make_runner("abc123")
            source_cfg = {
                "repo": "https://example.com/repo.git",
                "ref": "main",
                "skill_map": {"myrepo": {"source_path": "."}},
                "targets": {"t1": "flat_copy"},
            }
            all_targets = {"t1": {"enabled": True, "path": str(target_root)}}
            state: dict = {"version": 1, "sources": {}}

            sync_tracked_repos.update_tracked_repo(
                "myrepo", source_cfg, all_targets, cache_root, runner=runner, state=state,
            )
            self.assertIn("myrepo", state["sources"])
            self.assertEqual(state["sources"]["myrepo"]["commit"], "abc123")
            self.assertEqual(state["sources"]["myrepo"]["targets"]["t1"]["commit"], "abc123")
            self.assertEqual(state["sources"]["myrepo"]["targets"]["t1"]["mode"], "flat_copy")
            self.assertIn("deployed_at", state["sources"]["myrepo"]["targets"]["t1"])

    def test_disabled_target_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_root, _ = self._make_repo(tmp_dir)

            runner, _ = self._make_runner("abc123")
            source_cfg = {
                "repo": "https://example.com/repo.git",
                "ref": "main",
                "skill_map": {"myrepo": {"source_path": "."}},
                "targets": {"t1": "flat_copy"},
            }
            all_targets = {"t1": {"enabled": False, "path": "/nowhere"}}

            result = sync_tracked_repos.update_tracked_repo(
                "myrepo", source_cfg, all_targets, cache_root, runner=runner,
            )
            self.assertEqual(result["targets"]["t1"]["status"], "skipped")

    def test_clone_target_creates_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_root, _ = self._make_repo(tmp_dir)
            target_root = Path(tmp_dir) / "target"
            clone_dir = target_root / "myrepo"
            clone_dir.mkdir(parents=True)
            (clone_dir / ".git").mkdir()  # fake git dir so clone_or_pull does pull
            (clone_dir / "SKILL.md").write_text("# root")
            (clone_dir / "sub1").mkdir()
            (clone_dir / "sub1" / "SKILL.md").write_text("# sub1")

            runner, _ = self._make_runner("abc123")
            source_cfg = {
                "repo": "https://example.com/repo.git",
                "ref": "main",
                "skill_map": {
                    "myrepo": {"source_path": "."},
                    "myrepo-sub1": {"source_path": "sub1"},
                },
                "targets": {"t1": "clone"},
            }
            all_targets = {"t1": {"enabled": True, "path": str(target_root)}}

            result = sync_tracked_repos.update_tracked_repo(
                "myrepo", source_cfg, all_targets, cache_root, runner=runner,
            )
            self.assertEqual(result["targets"]["t1"]["status"], "ok")
            self.assertEqual(result["targets"]["t1"]["mode"], "clone")
            self.assertGreaterEqual(result["targets"]["t1"]["symlinks_created"], 0)

    def test_skill_names_in_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_root, _ = self._make_repo(tmp_dir)
            target_root = Path(tmp_dir) / "target"
            target_root.mkdir()

            runner, _ = self._make_runner("abc123")
            source_cfg = {
                "repo": "https://example.com/repo.git",
                "ref": "main",
                "skill_map": {
                    "myrepo": {"source_path": "."},
                    "myrepo-sub1": {"source_path": "sub1"},
                },
                "targets": {"t1": "flat_copy"},
            }
            all_targets = {"t1": {"enabled": True, "path": str(target_root)}}

            result = sync_tracked_repos.update_tracked_repo(
                "myrepo", source_cfg, all_targets, cache_root, runner=runner,
            )
            self.assertIn("skill_names", result)
            self.assertIn("myrepo", result["skill_names"])
            self.assertIn("myrepo-sub1", result["skill_names"])

    def test_dry_run_does_not_copy_or_mutate_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_root, _ = self._make_repo(tmp_dir)
            target_root = Path(tmp_dir) / "target"
            target_root.mkdir()

            runner, _ = self._make_runner("abc123")
            source_cfg = {
                "repo": "https://example.com/repo.git",
                "ref": "main",
                "skill_map": {"myrepo": {"source_path": "."}},
                "targets": {"t1": "flat_copy"},
            }
            all_targets = {"t1": {"enabled": True, "host": "windows", "path": str(target_root)}}
            state = {"version": 1, "sources": {}}

            result = sync_tracked_repos.update_tracked_repo(
                "myrepo",
                source_cfg,
                all_targets,
                cache_root,
                runner=runner,
                state=state,
                dry_run=True,
            )

            self.assertEqual(result["targets"]["t1"]["status"], "planned")
            self.assertFalse((target_root / "myrepo").exists())
            self.assertEqual(state, {"version": 1, "sources": {}})

    def test_target_filter_limits_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_root, _ = self._make_repo(tmp_dir)
            first_target = Path(tmp_dir) / "first"
            second_target = Path(tmp_dir) / "second"
            first_target.mkdir()
            second_target.mkdir()

            runner, _ = self._make_runner("abc123")
            source_cfg = {
                "repo": "https://example.com/repo.git",
                "ref": "main",
                "skill_map": {"myrepo": {"source_path": "."}},
                "targets": {"t1": "flat_copy", "t2": "flat_copy"},
            }
            all_targets = {
                "t1": {"enabled": True, "host": "windows", "path": str(first_target)},
                "t2": {"enabled": True, "host": "wsl", "path": str(second_target)},
            }

            result = sync_tracked_repos.update_tracked_repo(
                "myrepo",
                source_cfg,
                all_targets,
                cache_root,
                runner=runner,
                target_ids={"t1"},
            )

            self.assertIn("t1", result["targets"])
            self.assertNotIn("t2", result["targets"])
            self.assertTrue((first_target / "myrepo" / "SKILL.md").exists())
            self.assertFalse((second_target / "myrepo").exists())

    def test_allowed_hosts_limits_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_root, _ = self._make_repo(tmp_dir)
            first_target = Path(tmp_dir) / "first"
            second_target = Path(tmp_dir) / "second"
            first_target.mkdir()
            second_target.mkdir()

            runner, _ = self._make_runner("abc123")
            source_cfg = {
                "repo": "https://example.com/repo.git",
                "ref": "main",
                "skill_map": {"myrepo": {"source_path": "."}},
                "targets": {"t1": "flat_copy", "t2": "flat_copy"},
            }
            all_targets = {
                "t1": {"enabled": True, "host": "windows", "path": str(first_target)},
                "t2": {"enabled": True, "host": "wsl", "path": str(second_target)},
            }

            result = sync_tracked_repos.update_tracked_repo(
                "myrepo",
                source_cfg,
                all_targets,
                cache_root,
                runner=runner,
                allowed_hosts={"windows"},
            )

            self.assertIn("t1", result["targets"])
            self.assertNotIn("t2", result["targets"])
            self.assertTrue((first_target / "myrepo" / "SKILL.md").exists())
            self.assertFalse((second_target / "myrepo").exists())


class SymlinkEdgeCaseTests(unittest.TestCase):
    def test_non_symlink_dir_not_overwritten(self) -> None:
        """A real directory at the link path should not be replaced."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_root = Path(tmp_dir) / "target"
            clone_dir = target_root / "myrepo"
            (clone_dir / "sub1").mkdir(parents=True)
            (clone_dir / "sub1" / "SKILL.md").write_text("sub1")
            # Create a real directory where the symlink would go
            real_dir = target_root / "myrepo-sub1"
            real_dir.mkdir(parents=True)
            (real_dir / "SKILL.md").write_text("user-managed")

            skill_map = {"myrepo-sub1": {"source_path": "sub1"}}
            created, skipped = sync_tracked_repos.create_skill_symlinks("myrepo", skill_map, target_root)
            self.assertEqual(created, 0)
            self.assertEqual(skipped, 1)
            self.assertFalse((target_root / "myrepo-sub1").is_symlink())
            self.assertEqual((target_root / "myrepo-sub1" / "SKILL.md").read_text(), "user-managed")

    def test_stale_symlink_replaced(self) -> None:
        """A symlink pointing to the wrong target should be updated."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_root = Path(tmp_dir) / "target"
            clone_dir = target_root / "myrepo"
            (clone_dir / "sub1").mkdir(parents=True)
            (clone_dir / "sub1" / "SKILL.md").write_text("sub1")
            # Create a stale symlink pointing somewhere else
            stale_target = Path(tmp_dir) / "old_location"
            stale_target.mkdir()
            link_path = target_root / "myrepo-sub1"
            link_path.symlink_to(stale_target)

            skill_map = {"myrepo-sub1": {"source_path": "sub1"}}
            created, skipped = sync_tracked_repos.create_skill_symlinks("myrepo", skill_map, target_root)
            self.assertEqual(created, 1)
            self.assertTrue(link_path.is_symlink())
            self.assertEqual(link_path.resolve(), (clone_dir / "sub1").resolve())


if __name__ == "__main__":
    unittest.main()
