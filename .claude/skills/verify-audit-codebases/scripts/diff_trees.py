#!/usr/bin/env python3
"""
Compare in-scope source between a pinned snapshot and the contest snapshot.

  diff_trees.py PINNED_DIR CONTEST_DIR [--rename OLD=NEW ...]

Libraries, tests, mocks and scripts are ignored; comments and whitespace are not compared. Paths are compared relative to each root,
so point CONTEST_DIR at the sponsor folder inside a Sherlock repo (e.g. <contest>/oku-custom-order-types).
--rename strips naming-only differences before comparing contents (e.g. --rename Solidly=).
"""

import argparse
import hashlib
import re
from pathlib import Path

EXT = {".sol", ".rs", ".move", ".cairo", ".go", ".vy"}
SKIP = ("lib/", "node_modules/", "test/", "tests/", "mock", "script/", "out/", "cache/")
# Comments and whitespace don't change behaviour; strip them so doc-only edits don't count.
COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def files(root: Path, renames: list[tuple[str, str]]) -> dict[str, str]:
    out = {}
    for f in root.rglob("*"):
        rel = f.relative_to(root).as_posix()
        if not (f.is_file() and f.suffix in EXT) or any(s in rel.lower() for s in SKIP):
            continue
        body = f.read_text(errors="ignore")
        for old, new in renames:
            rel, body = rel.replace(old, new), body.replace(old, new)
        if f.suffix != ".vy":
            body = COMMENT_RE.sub("", body)
        out[rel] = hashlib.sha1(" ".join(body.split()).encode()).hexdigest()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pinned", type=Path)
    ap.add_argument("contest", type=Path)
    ap.add_argument("--rename", action="append", default=[], help="OLD=NEW applied to paths and contents")
    args = ap.parse_args()
    renames = [tuple(r.split("=", 1)) for r in args.rename]
    pinned, contest = files(args.pinned, renames), files(args.contest, renames)

    same = [p for p in contest if pinned.get(p) == contest[p]]
    changed = sorted(p for p in contest if p in pinned and pinned[p] != contest[p])
    missing = sorted(p for p in contest if p not in pinned)
    extra = sorted(set(pinned) - set(contest))
    verdict = "IDENTICAL" if not changed and not missing else "DIFFERENT"
    print(f"{verdict}: contest files={len(contest)} identical={len(same)} changed={len(changed)} "
          f"missing_in_pinned={len(missing)} extra_in_pinned={len(extra)}")
    for label, items in (("changed", changed), ("missing", missing), ("extra", extra)):
        for p in items[:15]:
            print(f"  {label}: {p}")


if __name__ == "__main__":
    main()
