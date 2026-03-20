#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def candidate_python_paths(start: Path) -> list[Path]:
    roots = [start, *start.parents]
    rel_paths = (
        Path(".venv/Scripts/python.exe"),
        Path(".venv/bin/python"),
        Path("venv/Scripts/python.exe"),
        Path("venv/bin/python"),
    )
    candidates: list[Path] = []
    for root in roots:
        for rel_path in rel_paths:
            candidate = root / rel_path
            if candidate.is_file():
                candidates.append(candidate)
    return candidates


def has_basedpyright(python_path: Path) -> bool:
    result = subprocess.run(
        [str(python_path), "-c", "import basedpyright"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def resolve_command() -> list[str]:
    cwd = Path.cwd().resolve()
    for python_path in [*candidate_python_paths(cwd), Path(sys.executable)]:
        if python_path.is_file() and has_basedpyright(python_path):
            return [str(python_path), "-m", "basedpyright"]

    executable = shutil.which("basedpyright")
    if executable:
        return [executable]

    raise SystemExit(
        "basedpyright is not installed in a nearby virtualenv, the current Python interpreter, "
        "or on PATH."
    )


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description="Run basedpyright from the best available Python environment.")
    parser.add_argument(
        "--json-path",
        help="Write basedpyright JSON output to this file and return the same exit code.",
    )
    return parser.parse_known_args()


def main() -> int:
    args, passthrough = parse_args()
    command = [*resolve_command(), *passthrough]

    if args.json_path:
        if "--outputjson" not in passthrough:
            command.append("--outputjson")
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        Path(args.json_path).write_text(completed.stdout, encoding="utf-8")
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        return completed.returncode

    completed = subprocess.run(command, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
