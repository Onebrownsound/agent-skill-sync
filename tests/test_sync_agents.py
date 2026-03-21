from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_agents.py"
SPEC = importlib.util.spec_from_file_location("sync_agents", MODULE_PATH)
sync_agents = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = sync_agents
SPEC.loader.exec_module(sync_agents)


class HelperTests(unittest.TestCase):
    def test_split_managed_codex_agent_block_returns_empty_block_when_absent(self) -> None:
        prefix, block, suffix = sync_agents.split_managed_codex_agent_block("[agents.existing]\npath = \".codex/agents/existing.toml\"\n")
        self.assertEqual(prefix, "[agents.existing]\npath = \".codex/agents/existing.toml\"\n")
        self.assertEqual(block, "")
        self.assertEqual(suffix, "")

    def test_split_managed_codex_agent_block_rejects_malformed_markers(self) -> None:
        with self.assertRaises(ValueError):
            sync_agents.split_managed_codex_agent_block(sync_agents.MANAGED_AGENTS_BEGIN + "\n[agents.reviewer]\n")

    def test_is_exact_managed_codex_agent_section_requires_only_section_and_path(self) -> None:
        exact = "[agents.reviewer]\npath = \".codex/agents/reviewer.toml\"\n"
        partial = "[agents.reviewer]\npath = \".codex/agents/reviewer.toml\"\nmodel = \"custom\"\n"
        self.assertTrue(sync_agents.is_exact_managed_codex_agent_section(exact, "reviewer"))
        self.assertFalse(sync_agents.is_exact_managed_codex_agent_section(partial, "reviewer"))

    def test_render_target_codex_config_deduplicates_exact_sections_from_prefix_and_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            config_path = Path(root_dir) / "config.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[agents.reviewer]",
                        'path = ".codex/agents/reviewer.toml"',
                        "",
                        sync_agents.MANAGED_AGENTS_BEGIN,
                        "[agents.old]",
                        'path = ".codex/agents/old.toml"',
                        sync_agents.MANAGED_AGENTS_END,
                        "",
                        "[agents.reviewer]",
                        'path = ".codex/agents/reviewer.toml"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            rendered = sync_agents.render_target_codex_config(config_path, ["reviewer"])

            self.assertEqual(rendered["registered"], ["reviewer"])
            self.assertEqual(rendered["skipped"], [])
            self.assertEqual(rendered["new_content"].count("[agents.reviewer]"), 1)

    def test_codex_agent_name_uses_filename_stem(self) -> None:
        self.assertEqual(sync_agents.codex_agent_name("reviewer.toml"), "reviewer")
        self.assertEqual(sync_agents.codex_agent_name("my-agent.config.toml"), "my-agent.config")


if __name__ == "__main__":
    unittest.main()
