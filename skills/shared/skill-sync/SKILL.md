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

Tracked repos are refreshed into repo-owned imprints before normal deployment. They are configured in `config/targets.local.json` under `tracked_repos`. The `--update-sources` flag:

1. Clones/pulls each tracked repo into `.tracked-repos-cache/`
2. Writes a repo-owned imprint under `sources/tracked__<name>/imprint/`
3. Updates the tracked source registry in `config/tracked-skill-sources.json`
4. Materializes the selected tracked skills into `skills/shared`, `skills/codex`, or `skills/claude`
5. Runs the normal catalog sync so targets receive those skills as standard `owner: "sync"` entries

`config/tracked-repos-state.local.json` only tracks machine-local refresh state. The source-of-truth content lives in the repo-owned imprint and generated catalog.
The imprint tree and generated tracked catalog outputs are local build artifacts and may be gitignored; regenerate them with `python scripts/sync_skills.py --update-sources --check` or `--apply`.

## Owner-Aware Manifests (v2)

Manifests track skill ownership:
- `{"owner": "sync"}` — skills deployed from the repo catalog, including tracked repo imprints
- `{}` — unowned, not managed by sync

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
- The sync only removes skills it originally installed (owner tracking). It will not touch unowned skills.
