from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "source_imprints.py"
SPEC = importlib.util.spec_from_file_location("source_imprints", MODULE_PATH)
source_imprints = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = source_imprints
SPEC.loader.exec_module(source_imprints)


def make_skill(root: Path, name: str, body: str = "# Skill\n") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


class SourceImprintTests(unittest.TestCase):
    def test_refresh_imprint_tree_ignores_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            repo_root = root / "repo"
            source_root = root / "source"
            skill = make_skill(source_root, "demo")
            (skill / ".git").mkdir()
            (skill / ".git" / "config").write_text("[core]\n", encoding="utf-8")
            (skill / "extra.txt").write_text("hello", encoding="utf-8")

            imprint = source_imprints.refresh_imprint_tree(
                repo_root=repo_root,
                source_id="tracked__demo",
                source_tree=skill,
                ignore_names={".git"},
            )

            self.assertTrue((imprint / "SKILL.md").is_file())
            self.assertTrue((imprint / "extra.txt").is_file())
            self.assertFalse((imprint / ".git").exists())

    def test_materialize_skill_applies_overlay_file_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            imprint = make_skill(root / "imprint", "demo", "# Base\n")
            (imprint / "notes.txt").write_text("base", encoding="utf-8")
            overlay = root / "overlay"
            overlay.mkdir(parents=True)
            (overlay / "notes.txt").write_text("overlay", encoding="utf-8")
            dest = root / "dest" / "demo"

            source_imprints.materialize_skill(
                imprint_tree=imprint,
                overlay_tree=overlay,
                dest=dest,
            )

            self.assertEqual((dest / "notes.txt").read_text(encoding="utf-8"), "overlay")
            self.assertTrue((dest / "SKILL.md").is_file())

    def test_materialization_status_detects_updates(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            imprint = make_skill(root / "imprint", "demo", "# Base\n")
            overlay = root / "overlay"
            overlay.mkdir(parents=True)
            dest = root / "dest" / "demo"

            self.assertEqual(
                source_imprints.materialization_status(
                    imprint_tree=imprint,
                    overlay_tree=overlay,
                    dest=dest,
                ),
                "add",
            )

            source_imprints.materialize_skill(imprint_tree=imprint, overlay_tree=overlay, dest=dest)
            self.assertEqual(
                source_imprints.materialization_status(
                    imprint_tree=imprint,
                    overlay_tree=overlay,
                    dest=dest,
                ),
                "unchanged",
            )

            (overlay / "README.md").write_text("overlay", encoding="utf-8")
            self.assertEqual(
                source_imprints.materialization_status(
                    imprint_tree=imprint,
                    overlay_tree=overlay,
                    dest=dest,
                ),
                "update",
            )


if __name__ == "__main__":
    unittest.main()
