#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: summarize_basedpyright.py <basedpyright-output.json>", file=sys.stderr)
        return 2

    json_path = Path(sys.argv[1]).resolve()
    payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
    diagnostics = payload.get("generalDiagnostics", [])

    severity_counts = Counter(item.get("severity", "unknown") for item in diagnostics)
    rule_counts = Counter(item.get("rule", "unknown") for item in diagnostics)
    file_counts = Counter(Path(item["file"]).name for item in diagnostics if item.get("file"))

    print(f"Diagnostics: {len(diagnostics)}")
    print("By severity:")
    for severity, count in severity_counts.most_common():
        print(f"  {severity}: {count}")

    print("Top rules:")
    for rule, count in rule_counts.most_common(10):
        print(f"  {rule}: {count}")

    print("Top files:")
    for filename, count in file_counts.most_common(10):
        print(f"  {filename}: {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
