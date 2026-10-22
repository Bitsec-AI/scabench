#!/usr/bin/env python3
"""
Replace scraped finding titles/descriptions in a ScaBench-style dataset with clean text from the original source.

  clean_findings.py DATASET (--projects ID ... | --platform NAME ... | --all)
                    [--nethermind DIR] [--out PATH | --in-place] [--log PATH] [--cache DIR]
                    [--judging-repo PROJECT_ID=owner/repo ...] [--allow-partial]

Sources, in order of preference:
  nethermind  an already-cleaned copy (auditagent-scoring-algo data/source_of_truth/<project_id>.json), adopted verbatim
  code4rena   the published report page https://code4rena.com/reports/<slug>, converted to Markdown, minus
              credits, sponsor/judge discussion and anything outside the finding's own section
  sherlock    the finding's GitHub issue in the judging repo (Markdown), minus the header lines and "Discussion"
Other platforms are left unchanged and reported, since there is no re-fetchable source yet.

Finding IDs, order and severities never change; only `title` and `description` do. Every finding gets a provenance
entry in the log. Exits 2 when any selected finding could not be resolved, unless --allow-partial.
Needs `beautifulsoup4` for Code4rena (`uv run --with beautifulsoup4 python ...`) and an authenticated `gh` for Sherlock.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from findings_common import coverage, title_matches  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (scabench clean-audit-findings)"}


# ---------------------------------------------------------------------------
# Fetching with an on-disk cache, so reruns and checks don't refetch.
def cached(path, fetch):
    path = Path(path)
    if path.exists():
        return path.read_text()
    text = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return text


def http_get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - retried, then surfaced
            if i == tries - 1:
                raise RuntimeError(f"GET {url}: {e}") from e
            time.sleep(2 * (i + 1))


def gh_api(path):
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def gh_json_lines(path):
    out = subprocess.run(["gh", "api", "--paginate", path, "-q", ".[] | @json"],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path}: {out.stderr.strip()}")
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Code4rena: report HTML -> Markdown per finding.
C4_LABEL = re.compile(r"^\[\s*(?:(?P<hm>[HM])-(?P<n>\d+)|(?:L|QA|N|NC)?-?(?P<q>\d+))\s*\]\s*(?P<title>.*)$", re.S)
# A bold paragraph that opens the post-judging thread: "Owl (Liquid Ron) confirmed and commented", "X mitigated:", ...
C4_DISCUSSION = re.compile(
    r"^.{0,120}?\((?:judge|warden|validator|lookout|[^()]{1,60})\)\s*(?:confirmed|acknowledged|disputed|commented|marked|"
    r"decreased|increased|disagreed|validated|invalidated|labeled|labelled)\b|^.{0,80}\bmitigated\b|^Status\s*:", re.I | re.S)
C4_CREDITS = re.compile(r"^\s*Submitted by\b", re.I)
# Newer reports write sponsor status as a short, fully bold line with no "(Sponsor)": "Blackhole commented:",
# "Virtuals marked as informative", "Blackhole disputed".
C4_STATUS = re.compile(r"^[\w .\-]{1,40}\s(?:confirmed|acknowledged|disputed|commented|mitigated|marked as [\w -]+)\b"
                       r"[^\n]{0,40}$", re.I)
# Only these h1 sections hold the audit's own findings; "Mitigation Review" reuses labels like "[M-05] Unmitigated".
C4_FINDING_H1 = re.compile(r"^(High|Medium|Low|QA|Quality|Gas)", re.I)


def md_inline(node, in_code=False):
    from bs4 import NavigableString, Tag

    parts = []
    for c in node.children:
        if isinstance(c, NavigableString):
            parts.append(str(c) if in_code else re.sub(r"\s+", " ", str(c)))
        elif isinstance(c, Tag):
            if c.name == "br":
                parts.append("\n")
            elif c.name == "code" and not in_code:
                inner = c.get_text()
                parts.append(f"`{inner}`" if inner.strip() else inner)
            elif c.name in ("img", "svg", "script", "style"):
                continue
            elif c.name == "a" and "anchor" in (c.get("class") or []):
                continue  # heading permalink icon
            elif c.name in ("em", "i") and glued_to_words(c):
                # The report's Markdown renderer read `a_b_c` as emphasis; put the eaten underscores back.
                parts.append(f"_{md_inline(c, in_code)}_")
            else:
                parts.append(md_inline(c, in_code))
    return "".join(parts)


def glued_to_words(tag):
    from bs4 import NavigableString

    prev, nxt = tag.previous_sibling, tag.next_sibling
    return (isinstance(prev, NavigableString) and re.search(r"\w$", str(prev)) is not None
            and isinstance(nxt, NavigableString) and re.match(r"\w", str(nxt)) is not None)


def md_blocks(nodes):
    """Convert a sequence of block nodes to Markdown paragraphs."""
    from bs4 import NavigableString, Tag

    out = []
    for n in nodes:
        if isinstance(n, NavigableString):
            t = re.sub(r"\s+", " ", str(n)).strip()
            if t:
                out.append(t)
            continue
        if not isinstance(n, Tag):
            continue
        name = n.name
        if name in ("h2", "h3", "h4", "h5", "h6"):
            # Some reports put a finding's sub-headings (Proof of Concept, ...) at h2; render them all as ###.
            out.append("#" * max(3, int(name[1])) + " " + md_inline(n).strip())
        elif name == "p":
            t = re.sub(r"[ \t]*\n[ \t]*", "\n", md_inline(n)).strip()
            if t:
                out.append(t)
        elif name == "pre":
            lang = n.get("data-language") or ""
            code = n.get_text().strip("\n")
            out.append(f"```{lang}\n{code}\n```")
        elif name in ("ul", "ol"):
            items = []
            start = int(n.get("start", 1)) if str(n.get("start", 1)).isdigit() else 1  # lists continued after code
            for i, li in enumerate(n.find_all("li", recursive=False), start):
                body = md_blocks(li.contents) if li.find(["p", "pre", "ul", "ol"]) else md_inline(li).strip()
                bullet = f"{i}. " if name == "ol" else "- "
                items.append(bullet + body.replace("\n", "\n   "))
            out.append("\n".join(items))
        elif name == "blockquote":
            out.append("\n".join("> " + line if line else ">" for line in md_blocks(n.contents).split("\n")))
        elif name == "table":
            rows = [[md_inline(c).strip().replace("|", "\\|") for c in tr.find_all(["th", "td"])] for tr in n.find_all("tr")]
            if rows:
                lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
                lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                out.append("\n".join(lines))
        elif name in ("div", "details", "section", "figure"):
            inner = md_blocks(n.contents)
            if inner:
                out.append(inner)
        else:
            t = md_inline(n).strip()
            if t:
                out.append(t)
    return "\n\n".join(out)


def c4_sections(html):
    """Map 'H-01' / 'M-03' / 'Q1'.. (QA items in order) -> (title_markdown, [block nodes])."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    body = soup.find(class_="report-contents")
    if body is None:
        raise RuntimeError("report-contents not found")
    sections, h1, qa_n, cur = {}, "", 0, None
    for node in body.children:
        name = getattr(node, "name", None)
        m = C4_LABEL.match(md_inline(node).strip()) if name == "h2" else None
        if name == "h1" or m:  # not <hr>: some findings contain rules before their PoC
            cur = None
        if name == "h1":
            h1 = node.get_text(" ", strip=True)
        elif m:
            if not C4_FINDING_H1.match(h1):
                continue
            if m.group("hm"):
                key = f"{m.group('hm')}-{int(m.group('n')):02d}"
            elif re.match(r"(Low|QA|Quality)", h1, re.I):
                qa_n += 1
                key = f"Q{qa_n}"
            else:
                continue
            if key in sections:  # never let a repeated label replace the first section
                continue
            cur = sections[key] = (m.group("title").strip(), [])
        elif cur is not None and name is not None:
            cur[1].append(node)
    return sections


def c4_clean_blocks(blocks):
    """Drop leading credits and everything from the first post-judging discussion paragraph on."""
    kept, started = [], False
    for b in blocks:
        text = b.get_text(" ", strip=True)
        if not started and b.name == "p" and C4_CREDITS.match(text):
            continue
        if b.name == "p" and b.find(["strong", "b"]) and C4_DISCUSSION.match(text):
            break
        bold = " ".join(x.get_text(" ", strip=True) for x in b.find_all(["strong", "b"])) if b.name == "p" else ""
        if bold and bold == text and C4_STATUS.match(text):
            break
        if b.name == "p" and C4_DISCUSSION.match(text) and re.match(r"^Status\s*:", text):
            break
        started = started or bool(text)
        kept.append(b)
    return kept


def clean_code4rena(project, cache):
    results = {}
    by_slug = {}
    for v in project["vulnerabilities"]:
        slug, label = v["finding_id"].rsplit("_", 1)
        by_slug.setdefault(slug, []).append((label, v))
    for slug, items in by_slug.items():
        url = f"https://code4rena.com/reports/{slug}"
        try:
            sections = c4_sections(cached(Path(cache) / "code4rena" / f"{slug}.html", lambda: http_get(url)))
        except Exception as e:  # noqa: BLE001
            for _, v in items:
                results[v["finding_id"]] = {"status": "failed", "source": url, "note": str(e)}
            continue
        qa_keys = sorted((k for k in sections if k.startswith("Q")), key=lambda k: int(k[1:]))
        for label, v in items:
            m = re.match(r"([HML])-(\d+)$", label)
            key = None
            if m and m.group(1) in "HM":
                key = f"{m.group(1)}-{int(m.group(2)):02d}"
            elif m:
                key = f"Q{int(m.group(2))}"
            # Verify by title. QA numbering can differ from ScaBench's L-n (some reports restart [01] per file and
            # repeat titles), so otherwise take the title match whose text best overlaps the scraped description.
            if key not in sections or not title_matches(v["title"], sections[key][0]):
                pool = qa_keys if label.startswith("L") else [k for k in sections if not k.startswith("Q")]
                matches = [k for k in pool if title_matches(v["title"], sections[k][0])]
                key = max(matches, key=lambda k: coverage(v["description"], md_blocks(sections[k][1])), default=None)
            note = ""
            if key is None and m and m.group(1) in "HM" and f"{m.group(1)}-{int(m.group(2)):02d}" in sections:
                # H/M labels are unique in the audit sections, so the label wins when the scraped title is junk
                # (e.g. "Unmitigated", scraped from the Mitigation Review section that reuses the label).
                key = f"{m.group(1)}-{int(m.group(2)):02d}"
                note = f"scraped title {v['title']!r} matched no section; used the report's {key} by label - verify"
            if key is None:
                results[v["finding_id"]] = {"status": "failed", "source": url, "note": "no section with a matching title"}
                continue
            title, blocks = sections[key]
            results[v["finding_id"]] = {"status": "cleaned", "source": f"{url}#{key}", "title": title,
                                        "description": md_blocks(c4_clean_blocks(blocks)), "note": note}
    return results


# ---------------------------------------------------------------------------
# Sherlock: judging-repo issues.
SH_LINK = re.compile(r"github\.com/sherlock-audit/([\w.\-\s]+?)/issues/\s*(\d+)")
SH_PREFIX = re.compile(r"Source:\s*https?://github\.com/sherlock-audit/([\w.-]+)")
SH_HEADER = re.compile(r"^(?P<handle>[^\n]{1,80})\n\n(?P<sev>Critical|High|Medium|Low|Informational)\n\n#\s*(?P<title>[^\n]+)\n",
                       re.I)
SH_DISCUSSION = re.compile(r"\n#{1,3}\s*Discussion\s*\n", re.I)


def sherlock_links(desc):
    return [(re.sub(r"\s", "", repo), int(num)) for repo, num in SH_LINK.findall(desc)]


def sherlock_repos(project, overrides):
    """Repos to search: explicit overrides, repos most findings link to, else a judging repo named by the Source prefix."""
    n = len(project["vulnerabilities"])
    counts = Counter(repo for v in project["vulnerabilities"] for repo in {r for r, _ in sherlock_links(v["description"])})
    repos = list(overrides) + [r for r, c in counts.items() if c >= max(2, 0.2 * n)]
    if not repos:
        prefixes = Counter(m for v in project["vulnerabilities"] for m in SH_PREFIX.findall(v["description"]))
        if prefixes:
            prefix = prefixes.most_common(1)[0][0]
            items = gh_api(f"search/repositories?q={prefix}+org:sherlock-audit+in:name&per_page=50")["items"]
            repos = [r["name"] for r in items if r["name"].startswith(prefix) and r["name"].endswith("-judging")]
    return list(dict.fromkeys(f"sherlock-audit/{r}" if "/" not in r else r for r in repos))


def sherlock_issue_text(issue):
    body = (issue.get("body") or "").replace("\r\n", "\n").strip("\n") + "\n"
    title, sev = issue["title"], None
    m = SH_HEADER.match(body)
    if m:
        title, sev = m.group("title").strip(), m.group("sev")
        body = body[m.end():]
    body = SH_DISCUSSION.split(body, 1)[0]
    return title.strip(), tidy_issue_markdown(body), sev


def tidy_issue_markdown(body):
    """Drop issue-template leftovers: '_No response_' sections, empty headings, <details>/<summary> wrappers."""
    body = re.sub(r"(?m)^#{1,6}[^\n]*\n\s*_No response_[ \t]*$\n?", "", body)
    body = re.sub(r"</?details>[ \t]*\n?", "", body)
    body = re.sub(r"<summary>(.*?)</summary>", r"\1", body, flags=re.S)
    lines, out = body.split("\n"), []
    for i, line in enumerate(lines):
        m = re.match(r"(#{1,6})\s", line)
        if m:
            nxt = next((x for x in lines[i + 1:] if x.strip()), "")
            n = re.match(r"(#{1,6})\s", nxt)
            if not nxt or (n and len(n.group(1)) <= len(m.group(1))):
                continue  # heading with nothing under it
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def clean_sherlock(project, cache, overrides):
    results = {}
    repos = sherlock_repos(project, overrides)
    if not repos:
        return {v["finding_id"]: {"status": "failed", "source": "", "note": "no judging repo found; pass --judging-repo"}
                for v in project["vulnerabilities"]}
    issues, errors = [], []
    for repo in repos:
        try:
            rows = json.loads(cached(Path(cache) / "sherlock" / f"{repo.replace('/', '__')}.json", lambda: json.dumps(
                [{k: i.get(k) for k in ("number", "title", "body", "html_url")} | {"labels": [lb["name"] for lb in i["labels"]]}
                 for i in gh_json_lines(f"repos/{repo}/issues?state=all&per_page=100") if "pull_request" not in i])))
        except RuntimeError as e:  # deleted or private repo
            errors.append(str(e))
            continue
        for i in rows:
            t, b, sev = sherlock_issue_text(i)
            issues.append({"repo": repo, **i, "clean_title": t, "clean_body": b, "severity": sev})
    used = set()
    for v in project["vulnerabilities"]:
        linked = {(r, n) for r, n in sherlock_links(v["description"])}
        # The issue the report links to is always a candidate: PDF titles can differ from issue titles (renamed contracts).
        cands = [i for i in issues if title_matches(v["title"], i["clean_title"])
                 or (i["repo"].split("/")[1], i["number"]) in linked]
        if not cands:
            # Badly fused titles: fall back to description overlap.
            scored = sorted(((coverage(v["description"], i["clean_body"]), i) for i in issues), key=lambda x: -x[0])
            cands = [i for s, i in scored[:1] if s >= 0.5]

        def rank(i):
            dup = any(re.search(r"duplicate|invalid|excluded", lb, re.I) for lb in i["labels"])
            return ((i["repo"].split("/")[1], i["number"]) in linked, not dup,
                    coverage(v["description"], i["clean_body"]), -i["number"])

        cands = sorted(cands, key=rank, reverse=True)
        if not cands:
            results[v["finding_id"]] = {"status": "failed", "source": ",".join(repos),
                                        "note": "; ".join(errors) or "no matching issue"}
            continue
        best = cands[0]
        note = []
        if (best["repo"], best["number"]) in used:
            note.append("issue already used by another finding")
        used.add((best["repo"], best["number"]))
        if best["severity"] and best["severity"].lower() != v["severity"].lower():
            note.append(f"issue severity {best['severity']} vs dataset {v['severity']} (dataset kept)")
        results[v["finding_id"]] = {"status": "cleaned", "source": best["html_url"], "title": best["clean_title"],
                                    "description": best["clean_body"], "note": "; ".join(note)}
    return results


# ---------------------------------------------------------------------------
def adopt_nethermind(project, gold_dir):
    path = Path(gold_dir) / f"{project['project_id']}.json"
    if not path.exists():
        return None
    gold = {v["finding_id"]: v for v in json.loads(path.read_text())["vulnerabilities"]}
    ids = [v["finding_id"] for v in project["vulnerabilities"]]
    if set(ids) != set(gold):
        raise SystemExit(f"{project['project_id']}: finding IDs differ from {path}; refusing to adopt")
    return {fid: {"status": "adopted", "source": f"nethermind:{path.name}", "title": gold[fid]["title"],
                  "description": gold[fid]["description"]} for fid in ids}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", type=Path)
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--projects", nargs="+")
    sel.add_argument("--platform", nargs="+")
    sel.add_argument("--all", action="store_true")
    ap.add_argument("--nethermind", type=Path, help="source_of_truth dir (or repo root) of auditagent-scoring-algo")
    out = ap.add_mutually_exclusive_group(required=True)
    out.add_argument("--out", type=Path)
    out.add_argument("--in-place", action="store_true")
    ap.add_argument("--log", type=Path, help="provenance log (default: <out>.clean-log.json)")
    ap.add_argument("--cache", type=Path, default=Path(".cache/clean-audit-findings"))
    ap.add_argument("--judging-repo", action="append", default=[], metavar="PROJECT_ID=owner/repo")
    ap.add_argument("--allow-partial", action="store_true", help="exit 0 even if some findings could not be resolved")
    args = ap.parse_args()

    raw = args.dataset.read_text()
    data = json.loads(raw)
    ascii_ = json.dumps(data, indent=2, ensure_ascii=True) == raw.rstrip("\n")
    gold_dir = args.nethermind
    if gold_dir and (gold_dir / "data" / "source_of_truth").is_dir():
        gold_dir = gold_dir / "data" / "source_of_truth"
    overrides = {}
    for item in args.judging_repo:
        pid, repo = item.split("=", 1)
        overrides.setdefault(pid, []).append(repo)

    chosen = [p for p in data if args.all or (args.projects and p["project_id"] in args.projects)
              or (args.platform and p.get("platform", p["project_id"].split("_")[0]) in args.platform)]
    missing = set(args.projects or []) - {p["project_id"] for p in chosen}
    if missing:
        raise SystemExit(f"unknown project_id(s): {sorted(missing)}")

    log, failed = [], 0
    for p in chosen:
        platform = p.get("platform") or p["project_id"].split("_")[0]
        results = adopt_nethermind(p, gold_dir) if gold_dir else None
        if results is None:
            if platform == "code4rena":
                results = clean_code4rena(p, args.cache)
            elif platform == "sherlock":
                results = clean_sherlock(p, args.cache, overrides.get(p["project_id"], []))
            else:
                results = {v["finding_id"]: {"status": "unchanged", "source": "",
                                             "note": f"no fetcher for platform {platform}"} for v in p["vulnerabilities"]}
        counts = Counter()
        for v in p["vulnerabilities"]:
            r = results.get(v["finding_id"], {"status": "failed", "note": "not resolved"})
            counts[r["status"]] += 1
            entry = {"project_id": p["project_id"], "finding_id": v["finding_id"], "status": r["status"],
                     "source": r.get("source", ""), "note": r.get("note", "")}
            if r["status"] in ("cleaned", "adopted"):
                entry["title_before"] = v["title"]
                v["title"], v["description"] = r["title"], r["description"]
            else:
                failed += r["status"] == "failed"
            log.append(entry)
        print(f"{p['project_id']:60} {dict(counts)}")

    dest = args.dataset if args.in_place else args.out
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2, ensure_ascii=ascii_) + "\n")
    log_path = args.log or dest.with_suffix(".clean-log.json")
    log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {dest} and {log_path}; {failed} finding(s) unresolved")
    if failed and not args.allow_partial:
        sys.exit(2)


if __name__ == "__main__":
    main()
