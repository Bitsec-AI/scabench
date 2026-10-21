#!/usr/bin/env python3
"""
Pass/fail check that each audit project's pinned source matches its audit findings.

Input: a dataset JSON (list of projects, or a single project) in ScaBench format:
  {"project_id", "codebases": [{"repo_url", "commit", "tarball_url"?}],
   "vulnerabilities": [{"finding_id", "title", "description", "severity"}]}

Per project:
  T1 links    The contest repo most linked from the findings (code-423n4/*, sherlock-audit/*)
              is pinned, OR a sponsor repo is pinned at a commit that the contest repo records
              copying ("Copy <folder> @ <sha>") for a folder the findings link into.
  T2 context  >= 80% of findings that name code (files/identifiers) have >= 50% of those names
              present in the pinned source.
  PASS = T1 and T2. A T1 failure can be accepted via --overrides when a human has verified the
  pin by other means (e.g. private contest repo); such rows print PASS* and re-fail if the pin changes.
  Warnings (unpinned refs, fix-like commit messages, redirects) never fail.

Exit code 1 if any project fails. Requires network; `gh` (authenticated) enables commit
resolution, Sherlock copy records and commit-message warnings.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from collections import Counter
from pathlib import Path

EXT = ("sol", "rs", "move", "cairo", "go", "vy")
FILE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\.(" + "|".join(EXT) + r")\b")
IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")
LINK_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/(?:blob|tree)/[\w.-]+/([\w.-]*)")
CREDITS_RE = re.compile(r"^\s*Submitted by.*?(?:\n\s*\n|$)", re.S)
COPY_RE = re.compile(r"^Copy (\S+) @ ([0-9a-f]{40})")
# Sponsor commits named after audit work ("Merge ... sherlock-48", "Small changes from audit").
FIXLIKE_RE = re.compile(r"sherlock|code4rena|\bc4\b|cantina|\baudit\b|\b[HM]-\d+\b", re.I)
CONTEST_OWNERS = {"code-423n4", "sherlock-audit"}
NOISE_FILES = {"test.sol", "console.sol", "console2.sol", "t.sol", "script.sol", "vm.sol"}
SHA_RE = re.compile(r"[0-9a-f]{7,40}")


# ---------------------------------------------------------------- github helpers

def have_gh() -> bool:
    return shutil.which("gh") is not None and subprocess.run(
        ["gh", "auth", "status"], capture_output=True).returncode == 0


def gh_api(path: str):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else None


def repo_of(url: str) -> str:
    return re.sub(r"^https://github\.com/|\.git$|/$", "", url.strip())


def resolve_commit(repo: str, ref: str, use_gh: bool) -> tuple[str, dict]:
    """Return (sha, info). Blank/branch refs resolve like `git clone` would, then get pinned."""
    info: dict = {}
    if not SHA_RE.fullmatch(ref or ""):
        info["unpinned_ref"] = ref or "<blank>"
        out = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}", ref or "HEAD"],
                             capture_output=True, text=True, timeout=60).stdout.split()
        if not out:
            raise RuntimeError(f"cannot resolve {ref or 'HEAD'} of {repo}")
        ref = out[0]
    if use_gh:
        c = gh_api(f"repos/{repo}/commits/{ref}")
        if c:
            info.update(date=c["commit"]["committer"]["date"],
                        message=c["commit"]["message"].split("\n")[0][:120])
            ref = c["sha"]
        r = gh_api(f"repos/{repo}")
        if r and r["full_name"].lower() != repo.lower():
            info["redirects_to"] = r["full_name"]
    return ref, info


def contest_copies(contest_repo: str, use_gh: bool) -> list[tuple[str, str]] | None:
    """Sherlock contest repos record 'Copy <folder> @ <sha>' commits. None = repo not readable."""
    if not use_gh:
        return None
    commits = gh_api(f"repos/{contest_repo}/commits?per_page=100")
    if commits is None:
        return None
    out = []
    for c in commits:
        m = COPY_RE.match(c["commit"]["message"])
        if m:
            out.append((m.group(1), m.group(2)))
    return out


# ---------------------------------------------------------------- source + findings

def fetch(repo: str, sha: str, tarball_url: str | None, cache: Path) -> Path:
    dest = cache / f"{repo.replace('/', '__')}@{sha}"
    if dest.exists():
        return dest
    url = tarball_url or f"https://github.com/{repo}/archive/{sha}.tar.gz"
    tmp = dest.with_suffix(".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with urllib.request.urlopen(url, timeout=300) as resp, tarfile.open(fileobj=resp, mode="r|gz") as tar:
        for m in tar:
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1] or not (m.isfile() or m.isdir()):
                continue
            target = tmp / parts[1]
            if ".." in Path(parts[1]).parts:
                continue
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                f = tar.extractfile(m)
                if f:
                    target.write_bytes(f.read())
    tmp.rename(dest)
    return dest


def source_index(dirs: list[Path]) -> tuple[set[str], set[str]]:
    names, idents = set(), set()
    for d in dirs:
        for f in d.rglob("*"):
            if f.is_file() and f.suffix.lstrip(".") in EXT:
                names.add(f.name.lower())
                try:
                    idents.update(IDENT_RE.findall(f.read_text(errors="ignore")))
                except OSError:
                    pass
    return names, idents


def code_like(tok: str) -> bool:
    return bool("_" in tok.strip("_") or re.search(r"[a-z][A-Z]", tok)) and not tok.isupper()


def is_test_ident(t: str) -> bool:
    return t.lower().startswith(("test", "setup")) or "Test" in t or t.startswith("vm_")


def refs(text: str) -> tuple[set[str], set[str]]:
    text = CREDITS_RE.sub("", text)  # auditor handles are not code
    files = {f"{a}.{b}".lower() for a, b in FILE_RE.findall(text)} - NOISE_FILES
    idents = {t for t in IDENT_RE.findall(text) if code_like(t) and not is_test_ident(t)}
    return files, idents


def file_hit(f: str, names: set[str]) -> bool:
    # Scrapers split ("Risk\nParameter.sol") or glue ("inLiquidRon.sol") names.
    return f in names or any(n.endswith(f) or f.endswith(n) for n in names if len(n) >= 7)


def ident_hit(t: str, idents: set[str]) -> bool:
    # Scrapers glue English words onto inline code ("inisExpired", "callsFoowhich").
    n = len(t)
    return any(t[i:n - j] in idents for i in range(9) for j in range(9) if n - j - i >= 4)


# ---------------------------------------------------------------- per-project check

def check_project(p: dict, codebases: list[dict], src_dirs: list[Path], use_gh: bool,
                  copies_cache: dict, override: dict | None = None) -> dict:
    vulns = p["vulnerabilities"]
    text_all = "\n".join(v.get("title", "") + "\n" + v.get("description", "") for v in vulns)
    joined = text_all.replace("\n", "")  # links get split across lines by scrapers

    contest_links = Counter()
    linked_folders: dict[str, set[str]] = {}
    for owner, repo, first_seg in LINK_RE.findall(joined):
        if owner.lower() in CONTEST_OWNERS:
            key = f"{owner}/{repo}".lower()
            contest_links[key] += 1
            if len(first_seg) >= 3:
                linked_folders.setdefault(key, set()).add(first_seg.lower())
    top = contest_links.most_common(1)[0][0] if contest_links else None

    pinned = {(cb["repo"].lower(), cb["sha"]) for cb in codebases}
    declared = {r for r, _ in pinned} | {cb["info"].get("redirects_to", "").lower() for cb in codebases}

    t1_reason = "no contest-repo links in findings"
    copies = None
    if top is None:
        t1 = True
    elif top in declared:
        t1, t1_reason = True, f"findings link to pinned {top}"
    elif not top.startswith("sherlock-audit/"):
        t1, t1_reason = False, f"findings link to {top} but pinned {sorted(r for r, _ in pinned)}"
    else:
        if top not in copies_cache:
            copies_cache[top] = contest_copies(top, use_gh)
        copies = copies_cache[top]
        if copies is None:
            t1, t1_reason = False, f"findings link to {top}, which is not pinned and not readable (cannot verify copy)"
        else:
            folders = linked_folders.get(top, set())
            relevant = [(fo, sha) for fo, sha in copies
                        if not folders or any(fo.lower().startswith(s) or s.startswith(fo.lower()) for s in folders)]
            ok = [(fo, sha) for fo, sha in relevant if any(sha == s for _, s in pinned)]
            t1 = bool(ok)
            t1_reason = (f"pinned commit is the copy recorded by {top} ({ok[0][0]} @ {ok[0][1][:10]})" if ok else
                         f"findings link to {top}; pinned commit(s) not among its recorded copies "
                         f"{[f'{fo}@{sha[:10]}' for fo, sha in relevant] or copies or 'none'}")

    overridden = False
    if not t1 and override:
        accepted = {c.lower() for c in override.get("commits", [])}
        if accepted and accepted <= {sha for _, sha in pinned} | {f"{r}@{sha}" for r, sha in pinned}:
            t1, overridden = True, True
            t1_reason += f" | ACCEPTED by override: {override.get('reason', 'no reason given')}"
        else:
            t1_reason += " | override present but pinned commits differ from the accepted ones"

    names, idents = source_index(src_dirs)
    per = []
    for v in vulns:
        files, ids = refs(v.get("title", "") + "\n" + v.get("description", ""))
        n = len(files) + len(ids)
        if not n:
            continue
        hit_f = {f for f in files if file_hit(f, names)}
        hit_i = {i for i in ids if ident_hit(i, idents)}
        ratio = (len(hit_f) + len(hit_i)) / n
        per.append({"finding_id": v.get("finding_id"), "ratio": round(ratio, 2), "ok": ratio >= 0.5,
                    "missing_files": sorted(files - hit_f)[:8], "missing_idents": sorted(ids - hit_i)[:12]})
    matched = sum(f["ok"] for f in per)
    frac = matched / len(per) if per else 0.0
    t2 = bool(per) and frac >= 0.8

    warnings = []
    for cb in codebases:
        i = cb["info"]
        if "unpinned_ref" in i:
            warnings.append(f"{cb['repo']}: commit '{i['unpinned_ref']}' is not a SHA; resolved to {cb['sha'][:10]} "
                            "(verify the repo had no source changes after the contest started)")
        # Contest repos get README/report commits mentioning audits; only sponsor repos matter here.
        contest_repo = cb["repo"].split("/")[0].lower() in CONTEST_OWNERS
        if i.get("message") and not contest_repo and FIXLIKE_RE.search(i["message"]):
            warnings.append(f"{cb['repo']}@{cb['sha'][:10]}: commit message looks post-audit: '{i['message']}'")
        if i.get("redirects_to"):
            warnings.append(f"{cb['repo']} redirects to {i['redirects_to']}")
    if not per:
        warnings.append("no finding names any code; T2 cannot be evaluated")

    return {
        "project_id": p["project_id"], "pass": t1 and t2, "overridden": overridden,
        "t1_links": t1, "t1_reason": t1_reason, "top_contest_link": top, "contest_links": dict(contest_links),
        "linked_folders": {k: sorted(v) for k, v in linked_folders.items()},
        "t2_context": t2, "findings_with_context": len(per), "findings_matched": matched,
        "match_frac": round(frac, 2), "source_files": len(names),
        "codebases": [{"repo": cb["repo"], "sha": cb["sha"], **cb["info"]} for cb in codebases],
        "warnings": warnings, "findings": per,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--project", nargs="+", help="only these project_ids")
    ap.add_argument("--cache", type=Path, default=Path(".cache/verify-codebases"),
                    help="where source snapshots are kept (reused across runs)")
    ap.add_argument("--report", type=Path, help="write full JSON report here")
    ap.add_argument("--negative-control", action="store_true",
                    help="score each project's findings against ANOTHER project's source; all should FAIL")
    ap.add_argument("--overrides", type=Path,
                    help='JSON {project_id: {"commits": ["<pinned sha>"], "reason": "..."}} accepting T1 for human-verified pins')
    ap.add_argument("--no-github", action="store_true", help="skip gh API calls (weaker T1, no warnings)")
    args = ap.parse_args()

    data = json.loads(args.dataset.read_text())
    projects = data if isinstance(data, list) else [data]
    if args.project:
        projects = [p for p in projects if p["project_id"] in set(args.project)]
    use_gh = not args.no_github and have_gh()
    if not use_gh:
        print("note: gh unavailable/disabled - Sherlock copy records and commit warnings are skipped", file=sys.stderr)
    args.cache.mkdir(parents=True, exist_ok=True)

    resolved = []
    for p in projects:
        cbs = []
        for cb in p["codebases"]:
            repo = repo_of(cb["repo_url"])
            sha, info = resolve_commit(repo, cb.get("commit", ""), use_gh)
            tarball = cb.get("tarball_url") if SHA_RE.fullmatch(cb.get("commit") or "") else None
            print(f"  fetch {repo}@{sha[:10]}", file=sys.stderr)
            cbs.append({"repo": repo, "sha": sha, "info": info, "dir": fetch(repo, sha, tarball, args.cache)})
        resolved.append(cbs)

    overrides = json.loads(args.overrides.read_text()) if args.overrides else {}
    copies_cache: dict = {}
    rows = []
    for i, p in enumerate(projects):
        cbs = resolved[(i + 7) % len(projects)] if args.negative_control and len(projects) > 1 else resolved[i]
        override = None if args.negative_control else overrides.get(p["project_id"])
        rows.append(check_project(p, cbs, [cb["dir"] for cb in cbs], use_gh, copies_cache, override))

    if args.report:
        args.report.write_text(json.dumps(rows, indent=1))
    title = "NEGATIVE CONTROL (findings vs unrelated source; every row should FAIL)" if args.negative_control else "RESULTS"
    print(f"\n{title}\n{'RESULT':6} {'T1':3} {'T2':3} {'context':>12}  project")
    for r in rows:
        result = ("PASS*" if r["overridden"] else "PASS") if r["pass"] else "FAIL"
        print(f"{result:6} {'ok' if r['t1_links'] else 'X':3} {'ok' if r['t2_context'] else 'X':3} "
              f"{r['findings_matched']:>4}/{r['findings_with_context']:<3}{r['match_frac']:>4.0%}  {r['project_id']}")
        if not r["t1_links"] or r["overridden"]:
            print(f"{'':16}T1: {r['t1_reason']}")
        if not r["t2_context"] and r["findings_with_context"]:
            worst = sorted(r["findings"], key=lambda f: f["ratio"])[:2]
            for f in worst:
                print(f"{'':16}T2: {f['finding_id']} missing {f['missing_files'][:4]} {f['missing_idents'][:5]}")
        for w in r["warnings"]:
            print(f"{'':16}warn: {w}")
    n_pass = sum(r["pass"] for r in rows)
    n_over = sum(r["pass"] and r["overridden"] for r in rows)
    print(f"\n{n_pass}/{len(rows)} projects pass" + (f" ({n_over} via override - see PASS* rows)" if n_over else ""))
    if args.negative_control:
        return 1 if n_pass else 0
    return 0 if n_pass == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
