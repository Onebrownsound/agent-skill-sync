---
name: basedpyright
description: Run, configure, and interpret basedpyright for Python repositories. Use when the user asks to type-check Python code with basedpyright, install or tune basedpyright in `pyproject.toml` or `pyrightconfig.json`, triage basedpyright diagnostics, or apply safe fixes for Python type errors, optional-member errors, missing imports, and migration noise.
---

# BasedPyright

## Overview

Use this skill to run basedpyright consistently, turn large diagnostic dumps into a short action plan, and make pragmatic repo-local config changes when a codebase is not ready for strict defaults.

## Workflow

1. Run basedpyright from the repo root. Prefer `scripts/run_basedpyright.py` so the command automatically uses a nearby virtualenv when possible.
2. If the repo already has `[tool.basedpyright]` in `pyproject.toml` or a `pyrightconfig.json`, run with no target path first and honor the repo config.
3. If the repo has no config yet, start narrow: run against the package directory or changed files rather than the entire repo.
4. Fix environment and config problems before code problems.
5. Fix errors before warnings unless the user explicitly wants warning cleanup.

## Commands

Resolve `scripts/...` relative to this skill directory, not the repo.

```bash
python scripts/run_basedpyright.py
python scripts/run_basedpyright.py legacy_voice_pipeline
python scripts/run_basedpyright.py legacy_voice_pipeline\integrations
python scripts/run_basedpyright.py --json-path .local\basedpyright.json
python scripts/summarize_basedpyright.py .local/basedpyright.json
```

When the repo uses a project-local virtualenv, the runner prefers:

- `.venv/Scripts/python.exe`
- `.venv/bin/python`
- `venv/Scripts/python.exe`
- `venv/bin/python`

If none of those contain `basedpyright`, it falls back to the current Python interpreter and then the `basedpyright` executable on `PATH`.

## Triage Order

### 1. Environment and config

Handle these first:

- `reportMissingImports`
- wrong interpreter or wrong venv
- missing optional dependencies
- incorrect include/exclude globs
- repo accidentally scanning generated folders, vendored code, or virtualenvs

Prefer fixing the environment before changing application code.

### 2. Real type errors

Prioritize these next:

- `reportArgumentType`
- `reportReturnType`
- `reportAssignmentType`
- `reportOptionalMemberAccess`
- `reportOptionalCall`

These usually indicate real defects or missing narrowing.

### 3. Migration noise

Handle these only after the error set is stable unless the user explicitly wants stricter cleanup:

- `reportUnusedImport`
- `reportUnusedCallResult`
- `reportAny`
- `reportExplicitAny`
- broad unknown-type warnings in legacy JSON-heavy code

## Fix Heuristics

### Missing imports

- If the import is required for normal repo operation, install or declare the dependency.
- If the import belongs to an optional integration, prefer a pragmatic config choice or an import guard over forcing a runtime dependency into the base package.
- If the import is local and should resolve, verify the repo root, package layout, and interpreter first.

### Optional access or optional call

- Add a guard clause, early return, or explicit narrowing.
- Avoid papering over `None` with a cast unless the invariant is already enforced nearby.

### Dataclass typing friction

- Tighten helper signatures first.
- Use overloads, protocols, or narrower parameter types before falling back to `cast`.

## Config Guidance

Keep basedpyright config in `pyproject.toml` when the repo already uses `pyproject.toml`.

Good baseline fields:

- `include`
- `exclude`
- `pythonVersion`
- `venvPath`
- `venv`

For legacy codebases, it is reasonable to start with a pragmatic baseline like:

- `typeCheckingMode = "basic"`
- `reportMissingImports = "warning"`
- `reportMissingTypeStubs = "none"`

Tighten toward `standard` or `recommended` only after the repo is stable enough to make the extra signal worth the noise.

## Repo Note

In `legacy-voice-pipeline`, the repo already has `[tool.basedpyright]` in `pyproject.toml` and `basedpyright` installed in `.venv`, so the default command from the repo root is enough:

```bash
python scripts/run_basedpyright.py
```

## Resources

- `scripts/run_basedpyright.py`: locate the best interpreter and run basedpyright
- `scripts/summarize_basedpyright.py`: shrink JSON output into counts by severity, rule, and file
