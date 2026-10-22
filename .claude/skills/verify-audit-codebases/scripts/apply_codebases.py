#!/usr/bin/env python3
"""
Replace the `codebases` of selected projects in a dataset JSON, keeping the file's formatting.

  apply_codebases.py DATASET FIXES.json

FIXES.json maps project_id -> list of {"repo": "owner/name", "commit": "<full sha>", "id": "<optional codebase_id>"}.
Only the listed projects change, so the git diff stays reviewable.
"""

import json
import sys
from pathlib import Path


def main() -> None:
    dataset, fixes_path = Path(sys.argv[1]), Path(sys.argv[2])
    raw = dataset.read_text()
    data = json.loads(raw)
    # Detect the existing style so the rewrite only touches changed lines.
    indent = next((i for i in (2, 4) for a in (True, False)
                   if json.dumps(data, indent=i, ensure_ascii=a) == raw.rstrip("\n")), 2)
    ascii_ = json.dumps(data, indent=indent, ensure_ascii=True) == raw.rstrip("\n")

    fixes = json.loads(fixes_path.read_text())
    unknown = set(fixes) - {p["project_id"] for p in data}
    if unknown:
        sys.exit(f"unknown project_id(s): {sorted(unknown)}")
    for p in data:
        if p["project_id"] not in fixes:
            continue
        p["codebases"] = [
            {
                "codebase_id": cb.get("id") or f"{p['name']}_{cb['commit'][:6]}",
                "repo_url": f"https://github.com/{cb['repo']}",
                "commit": cb["commit"],
                "tree_url": f"https://github.com/{cb['repo']}/tree/{cb['commit']}",
                "tarball_url": f"https://github.com/{cb['repo']}/archive/{cb['commit']}.tar.gz",
            }
            for cb in fixes[p["project_id"]]
        ]
        print(f"updated {p['project_id']}: {[cb['repo'] + '@' + cb['commit'][:10] for cb in fixes[p['project_id']]]}")
    dataset.write_text(json.dumps(data, indent=indent, ensure_ascii=ascii_) + ("\n" if raw.endswith("\n") else ""))


if __name__ == "__main__":
    main()
