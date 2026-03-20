---
name: skill-sync
description: Synchronize a source-of-truth skill repo to local Codex and Claude skill directories on the same machine. Use when working inside the `agent-skill-sync` repo to check sync status, apply sync to Windows or WSL targets, clean previously managed skills, or add a new shared, Codex-only, or Claude-only skill and publish it to local installs.
---

# Skill Sync

## Overview

Use this skill when you are inside the source-of-truth sync repo and need to push skill changes out to local agent installs in a controlled way.

## Workflow

1. Confirm you are in the source repo root that contains `config/targets.local.json` and `scripts/sync_skills.py`.
2. Run a dry check first.
3. Review planned adds, updates, and removals.
4. Apply the sync only after the plan looks correct.
5. Use `--clean` only when you intentionally want to remove previously managed skills that no longer exist in the source catalog.

## Commands

From the repo root:

```bash
python scripts/sync_skills.py --check
python scripts/sync_skills.py --apply
python scripts/sync_skills.py --apply --clean
python scripts/sync_skills.py --apply --target windows_codex
```

Host-specific wrappers:

```powershell
.\scripts\sync_windows.ps1 -Check
.\scripts\sync_windows.ps1
```

```bash
bash scripts/sync_wsl.sh --check
bash scripts/sync_wsl.sh --apply
```

## Catalog Rules

- Put cross-platform skills in `skills/shared`.
- Put Codex-only skills in `skills/codex`.
- Put Claude-only skills in `skills/claude`.
- Each skill directory must contain a valid `SKILL.md`.

## Safety

- Treat the repo as the only place to edit source skills.
- Do not hand-edit managed copies under `~/.codex/skills` or `~/.claude/skills` unless you are intentionally debugging drift.
- Prefer `--check` before every `--apply`.
