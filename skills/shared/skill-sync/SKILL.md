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
# Catalog sync (shared/codex/claude skills)
python scripts/sync_skills.py --check
python scripts/sync_skills.py --apply
python scripts/sync_skills.py --apply --clean
python scripts/sync_skills.py --apply --target windows_codex

# Git sources (gstack) + catalog sync in one command
python scripts/sync_skills.py --update-sources --check
python scripts/sync_skills.py --update-sources --apply

# Update everything across all hosts from WSL
python scripts/sync_skills.py --update-sources --host all --apply

# Migrate v1 manifests to v2 (one-time)
python scripts/sync_skills.py --migrate-manifests
```

Host-specific wrappers:

```powershell
.\scripts\sync_windows.ps1 -Check
.\scripts\sync_windows.ps1
.\scripts\sync_windows.ps1 -UpdateSources -Check
```

```bash
bash scripts/sync_wsl.sh --check
bash scripts/sync_wsl.sh --apply
bash scripts/sync_wsl.sh --update-sources --apply
```

## Cross-Runtime Support

WSL can sync Windows targets directly via `/mnt/c/` path translation. No need to run from Windows for Windows targets. Use `--host all` or `--host windows` from WSL.

## Tracked Repos

Whole repos deployed directly to targets. Configured in `config/targets.local.json` under `tracked_repos`. The `--update-sources` flag:

1. Clones/pulls each tracked repo into `.tracked-repos-cache/`
2. For **clone** targets (WSL): pulls the repo at `<skills_dir>/<name>` and creates symlinks for sub-skills
3. For **flat_copy** targets (Windows): copies SKILL.md files as flat skill directories
4. Records deployed SHA per target in `config/tracked-repos-state.local.json`
5. Skips flat_copy targets already at the current commit

This contrasts with **snapshot skills** (managed by `manage_skill_sources.py`), where individual skills are extracted from a repo into the catalog.

## Owner-Aware Manifests (v2)

Manifests track skill ownership:
- `{"owner": "sync"}` — snapshot skills managed by catalog sync
- `{"owner": "tracked:gstack"}` — skills from a tracked repo
- `{}` — unowned, not managed by either system

The sync will never remove skills it doesn't own. Use `--migrate-manifests` to upgrade v1 manifests.

## Catalog Rules

- Put cross-platform skills in `skills/shared`.
- Put Codex-only skills in `skills/codex`.
- Put Claude-only skills in `skills/claude`.
- Each skill directory must contain a valid `SKILL.md`.

## Safety

- Treat the repo as the only place to edit source skills.
- Do not hand-edit managed copies under `~/.codex/skills` or `~/.claude/skills` unless you are intentionally debugging drift.
- Prefer `--check` before every `--apply`.
- The sync only removes skills it originally installed (owner tracking). It will not touch gstack or other externally-managed skills.
