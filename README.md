# Agent Skill Sync

Source-of-truth repo for syncing local skill catalogs across same-machine agent installs.

## What This Repo Is

This repo is the canonical place to create, edit, review, and version skills.

The folders under `~/.codex/skills` and `~/.claude/skills` are not the primary place to work. They are deployment targets fed from this repo.

If you want a skill to exist long-term, it should live here first:

- `skills/shared` for skills that should go to every enabled target
- `skills/codex` for Codex-only skills
- `skills/claude` for Claude-only skills

## Operating Model

Use this repo like an install source and release source for local skills:

1. Create or edit skills in this repo.
2. Review the repo state and commit it when it looks right.
3. Push to your Git remote if you want that state preserved as the canonical remote version.
4. Deploy from this repo out to local installs with `--apply`.
5. Keep the deployment ticket that the script prints if you may want to roll back.

If you ever discover useful changes that were made directly in a live install, import them back into this repo with `--pull` first. After that, treat the repo as authoritative again.

This scaffold is set up for:

- Windows Codex
- WSL Codex
- WSL Claude

Windows Claude is included as a disabled placeholder because it does not currently have a plain `~/.claude/skills` directory on this machine.

## Source Of Truth

This repo is the source of truth.

Live installs under `~/.codex/skills` or `~/.claude/skills` are deployment targets, not the canonical place to edit skills. That keeps changes reviewable, versioned, and reproducible.

In short:

- Edit here
- Commit here
- Push here
- Deploy outward from here

## Layout

```text
agent-skill-sync/
  config/
    skill-sources.json
    targets.example.json
    targets.local.json
  scripts/
    manage_skill_sources.py
    sync_skills.py
    sync_windows.ps1
    sync_wsl.sh
  skills/
    shared/
    codex/
    claude/
```

- `skills/shared`: sync to every enabled target
- `skills/codex`: sync only to Codex targets
- `skills/claude`: sync only to Claude targets
- `config/targets.local.json`: machine-local target paths and enable flags
- `config/skill-sources.json`: tracked external skill source registry for repo-managed installs
- `config/skill-sources.local.json`: machine-local external skill source registry for plugin/path installs

## Installing External Skills Into This Repo

Use `scripts/manage_skill_sources.py` when you want to bring a skill into this repo from somewhere else and keep enough bookkeeping to update it later.

Supported source types:

- GitHub repo paths, tracked in `config/skill-sources.json` by default
- Local plugin or filesystem paths, tracked in `config/skill-sources.local.json` by default

Typical commands:

```powershell
python scripts/manage_skill_sources.py list
python scripts/manage_skill_sources.py scan-github --repo owner/repo
python scripts/manage_skill_sources.py scan-github --repo owner/repo --format json
python scripts/manage_skill_sources.py install-github-batch --repo owner/repo --select shared --select claude
python scripts/manage_skill_sources.py install-github-batch --repo owner/repo --select codex --copy-agents --register-codex-agents
python scripts/manage_skill_sources.py install-github --bucket codex --repo owner/repo --path path/to/skill
python scripts/manage_skill_sources.py install-plugin --bucket codex --path C:\path\to\skill
python scripts/manage_skill_sources.py update --key codex/skill-name
python scripts/manage_skill_sources.py update-all
```

## Scanning A Repo Before Import

Use `scan-github` to inventory a repository before installing anything into this repo.

It currently works in a batch-first way:

- `skills/<name>` are treated as shared skills
- `.claude/skills/<name>` and `claude/skills/<name>` are treated as Claude-specific skills
- `.codex/skills/<name>` and `codex/skills/<name>` are treated as Codex-specific skills
- agent assets are inventoried separately and shown as manual items
- unknown layouts are hidden by default unless you pass `--include-unknown`

Example:

```powershell
python scripts/manage_skill_sources.py scan-github --repo affaan-m/everything-claude-code
```

That gives you:

- a grouped skill inventory
- a batch-oriented install plan by bucket
- a separate list of agent assets that need dedicated handling

If the scan looks right, you can batch install the recognized skill groups:

```powershell
python scripts/manage_skill_sources.py install-github-batch --repo affaan-m/everything-claude-code --select claude --select shared
```

Current rule:

- skills can be batch installed
- agent assets are scanned and listed, but are opt-in for copy and registration

## Agent Defaults

Agents are handled more conservatively than skills.

Default behavior:

- scan inventories agent assets
- batch install does not copy agents unless you pass `--copy-agents`
- batch install does not modify `.codex/config.toml` unless you pass `--register-codex-agents`

Codex-specific rule:

- copied Codex agents go under `.codex/agents/`
- registration in `.codex/config.toml` is opt-in
- tool-managed Codex agent entries live inside a dedicated managed block in `.codex/config.toml`
- new registrations merge into that managed block so partial imports do not silently unregister older managed agents
- if registration rewrites `.codex/config.toml`, the previous file is backed up to `.codex/config.toml.agent-skill-sync.bak`
- existing unmanaged `[agents.<name>]` entries are not overwritten

Example:

```powershell
python scripts/manage_skill_sources.py install-github-batch --repo affaan-m/everything-claude-code --select shared --select claude --copy-agents --register-codex-agents
```

After install or update:

1. Review the imported skill in this repo.
2. Run `python scripts/sync_skills.py --check`.
3. Run `python scripts/sync_skills.py --apply` when you are ready to deploy outward.

## Which Path Should I Edit?

Edit the repo paths, not the live install paths.

Good:

- `skills/shared/<skill-name>`
- `skills/codex/<skill-name>`
- `skills/claude/<skill-name>`

Avoid editing by hand unless you are intentionally debugging drift:

- `C:/Users/redme/.codex/skills`
- `/home/redme/.codex/skills`
- `/home/redme/.claude/skills`

## Quick Start

Typical day-to-day workflow from Windows PowerShell:

```powershell
python scripts/sync_skills.py --check
python scripts/sync_skills.py --apply
# Save the printed ticket if you may want to restore this deployment later
```

Typical day-to-day workflow from WSL:

```bash
python3 scripts/sync_skills.py --check
python3 scripts/sync_skills.py --apply
# Save the printed ticket if you may want to restore this deployment later
```

If you are bootstrapping from an existing live install, use `--pull` first, review what came in, then sync outward normally.

If you are installing a skill from GitHub or a local plugin into this repo, use `manage_skill_sources.py` first, then sync outward with `sync_skills.py`.

From Windows PowerShell:

```powershell
python scripts/sync_skills.py --check
python scripts/sync_skills.py --apply
python scripts/sync_skills.py --pull --check --target windows_codex
python scripts/sync_skills.py --pull --apply --target windows_codex
python scripts/manage_skill_sources.py install-github --bucket codex --repo owner/repo --path skills/my-skill
python scripts/manage_skill_sources.py install-plugin --bucket codex --path C:\path\to\plugin-skill
python scripts/manage_skill_sources.py update --key codex/my-skill
```

From WSL:

```bash
python3 scripts/sync_skills.py --check
python3 scripts/sync_skills.py --apply
python3 scripts/sync_skills.py --pull --check --target wsl_codex
python3 scripts/sync_skills.py --pull --apply --target wsl_codex
python3 scripts/manage_skill_sources.py install-github --bucket codex --repo owner/repo --path skills/my-skill
python3 scripts/manage_skill_sources.py install-plugin --bucket codex --path /path/to/plugin-skill
python3 scripts/manage_skill_sources.py update --key codex/my-skill
```

Or use the wrappers:

```powershell
.\scripts\sync_windows.ps1 -Check
.\scripts\sync_windows.ps1
.\scripts\sync_windows.ps1 -Pull -Check -Target windows_codex
.\scripts\sync_windows.ps1 -Pull -Target windows_codex
.\scripts\sync_windows.ps1 -Rollback "<ticket-uuid>" -Check
.\scripts\sync_windows.ps1 -Rollback "<ticket-uuid>"
```

```bash
bash scripts/sync_wsl.sh --check
bash scripts/sync_wsl.sh --apply
bash scripts/sync_wsl.sh --pull --check --target wsl_codex
bash scripts/sync_wsl.sh --pull --apply --target wsl_codex
bash scripts/sync_wsl.sh --rollback "<ticket-uuid>" --check
bash scripts/sync_wsl.sh --rollback "<ticket-uuid>" --apply
```

## Current Targets

The local scaffolded config enables:

- `windows_codex` -> `C:/Users/redme/.codex/skills`
- `wsl_codex` -> `/home/redme/.codex/skills`
- `wsl_claude` -> `/home/redme/.claude/skills`

And keeps this disabled for now:

- `windows_claude` -> `C:/Users/redme/.claude/skills`

## Sync Behavior

- The sync script only applies targets that match the current host.
- Running on Windows syncs Windows targets.
- Running inside WSL syncs WSL targets.
- `--check` prints the plan without changing anything.
- `--apply` copies changed skills and writes a small managed manifest into the target root.
- Every push apply that changes one or more targets mints a deployment ticket UUID.
- Existing target skills are backed up by default before destructive push changes such as updates and `--clean` removals.
- Ticket metadata and backed-up skills are stored under `.skill-sync-tickets/<ticket-uuid>/` inside the target root.
- Use `--rollback <ticket-uuid>` to restore the pre-deploy state for matching host targets.
- Use `--no-backup` only when you intentionally want destructive push behavior with no rollback copy.
- `--clean` removes previously managed skills that no longer exist in the source catalog for that target.
- `--pull` imports valid live skills from a target back into this repo without changing the live install.
- Imported skills default into `skills/codex` or `skills/claude` based on the target kind.
- If an imported skill name already exists in the repo with different contents, it is reported as a conflict and left unchanged.

## Authoring And Deploying

For a new or updated skill:

1. Edit or add the skill in this repo.
2. Run `--check`.
3. Run `--apply`.
4. Save the printed deployment ticket.
5. Confirm the live targets look right.
6. Commit and push when you want that repo state preserved remotely.

By default, any existing target skill that gets replaced is first moved into that target's deployment ticket folder so you can roll back if needed.

## Deployment Tickets And Rollback

When a push apply changes a target, the script prints a ticket UUID.

Example:

```text
Ticket: 98272918-abd8-4787-9231-2c4d91a6f102
```

Keep that ticket if you may want to revert.

Preview a rollback:

```powershell
python scripts/sync_skills.py --rollback 98272918-abd8-4787-9231-2c4d91a6f102 --check
```

Apply a rollback:

```powershell
python scripts/sync_skills.py --rollback 98272918-abd8-4787-9231-2c4d91a6f102 --apply
```

The rollback restores the target to the pre-deploy state recorded for that ticket:

- skills added by that deployment are removed
- skills updated by that deployment are restored from the ticket backup
- skills removed by that deployment are restored from the ticket backup
- the previous managed manifest is restored

For an existing live skill that is not yet source-managed:

1. Run `--pull --check`.
2. Run `--pull --apply`.
3. Review what was imported into the repo.
4. Run normal `--check` and `--apply` to deploy the repo-managed state.

For a skill that comes from another repo or plugin and should still be tracked:

1. Install it into this repo with `scripts/manage_skill_sources.py`.
2. Confirm the source registry entry looks right.
3. Review the copied skill files under `skills/shared`, `skills/codex`, or `skills/claude`.
4. Run normal `--check` and `--apply` to deploy the repo-managed state.

## Bootstrapping Existing Installs

Use pull mode first when you want to turn an existing live skill catalog into source-managed repo content.

Example from Windows:

```powershell
python scripts/sync_skills.py --pull --check --target windows_codex
python scripts/sync_skills.py --pull --apply --target windows_codex
python scripts/sync_skills.py --check
python scripts/sync_skills.py --apply
```

Example from WSL:

```bash
python3 scripts/sync_skills.py --pull --check --target wsl_codex
python3 scripts/sync_skills.py --pull --apply --target wsl_codex
python3 scripts/sync_skills.py --check
python3 scripts/sync_skills.py --apply
```

## Adding Skills

1. Add a skill directory under one of:
   - `skills/shared/<skill-name>`
   - `skills/codex/<skill-name>`
   - `skills/claude/<skill-name>`
2. Make sure the skill contains a valid `SKILL.md`.
3. Run `python scripts/sync_skills.py --check`.
4. Run `python scripts/sync_skills.py --apply`.

## Notes

- This repo currently contains one shared helper skill: `skill-sync`.
- The helper skill is meant to be used while working from this source repo.
- Hidden directories such as `.system` are intentionally ignored during pull imports.
- Managed targets receive a `.skill-sync-manifest.json` file so the repo can track what it deployed.
- Managed deployment tickets and rollback backups are stored under `.skill-sync-tickets` in each target root.
- GitHub-installed skills are tracked in `config/skill-sources.json`.
- Local plugin/path installs are tracked in `config/skill-sources.local.json`, which is intentionally gitignored.
