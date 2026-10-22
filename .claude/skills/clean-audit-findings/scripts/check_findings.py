#!/usr/bin/env python3
"""
Check cleaned finding text against the original dataset. Pass/fail per project.

  check_findings.py ORIGINAL CLEANED [--projects ID ...] [--gold DIR] [--log CLEAN_LOG] [--reviewed FILE]
                    [--json REPORT]

Per project (default: every project whose text changed):
  structure  same finding IDs, order and severities; no empty title or description            (fail)
             findings the --log records as "added" (missing from the original scrape) may follow the originals;
             they get the noise check only, as there is no original text to compare with
  noise      no credits / discussion / spill-over / PDF damage left in prose or titles          (fail)
             glued inline code is a heuristic, reported as a warning
  retention  each cleaned description still contains the original's content, measured as the share of the
             original's spacing-insensitive 12-char shingles (after removing known noise) found in the clean text.
             Below 0.80 for any finding, or a project mean below 0.90, fails.
  title      cleaned title continues or closely matches the original (cut-off and fused titles still match)
  gold       with --gold (a directory of <project_id>.json reference copies), word-level recall and precision of
             the cleaned description against the reference. Recall below 0.95 fails.
  sources    with --log, the same source used for two findings is a warning (likely a wrong match)
--reviewed takes {finding_id: reason} for flags a person has checked and accepted (e.g. the original carried spill-over
the retention check cannot recognise). Those findings' flags become warnings and the project shows PASS*.
Exits 1 when any project fails.
"""

import argparse
import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from findings_common import BLOCKING, DETECTORS, coverage, detect, strip_raw_noise, title_matches  # noqa: E402

MIN_FINDING_RETENTION, MIN_MEAN_RETENTION, MIN_GOLD_RECALL = 0.80, 0.90, 0.95
WORD = re.compile(r"[a-z0-9_]+")


def words(text):
    # Fence language labels (```solidity) and markdown heading text are formatting, not content.
    text = re.sub(r"```[a-zA-Z0-9_+-]*", " ", text.lower())
    return WORD.findall(text)


def gold_scores(clean, gold):
    a, b = words(gold), words(clean)
    if not a:
        return 1.0, 1.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    hit = sum(m.size for m in sm.get_matching_blocks())
    return hit / len(a), (hit / len(b) if b else 0.0)


def check_project(orig, clean, gold, sources, reviewed, added=frozenset()):
    res = {"project_id": orig["project_id"], "findings": len(clean["vulnerabilities"]), "fail": [], "warn": [],
           "noise": Counter(), "flagged": []}
    ov, cv = orig["vulnerabilities"], clean["vulnerabilities"]
    extra = cv[len(ov):]
    if [v["finding_id"] for v in ov] != [v["finding_id"] for v in cv[:len(ov)]] or any(
            v["finding_id"] not in added for v in extra):
        res["fail"].append("finding IDs or order changed")
        return res
    for b in extra:
        if not b["title"].strip() or not b["description"].strip():
            res["fail"].append(f"{b['finding_id']}: empty title or description")
        for k, hit in detect(b["title"], b["description"], prose_only=True).items():
            if hit:
                res["noise"][k] += 1
                res["flagged"].append({"finding_id": b["finding_id"], "flags": [k], "title": b["title"][:100],
                                       "source": sources.get(b["finding_id"], "")})
    if extra:
        res["warn"].append(f"{len(extra)} finding(s) added from the source: {', '.join(v['finding_id'] for v in extra)}")
    if any(a["severity"] != b["severity"] for a, b in zip(ov, cv)):
        res["fail"].append("severity changed")
    gold_map = {v["finding_id"]: v for v in gold["vulnerabilities"]} if gold else {}
    rets, recalls, precs, title_bad = [], [], [], 0
    for a, b in zip(ov, cv):
        fid, flags = b["finding_id"], []
        if not b["title"].strip() or not b["description"].strip():
            res["fail"].append(f"{fid}: empty title or description")
        hits = detect(b["title"], b["description"], prose_only=True)
        for k, hit in hits.items():
            if hit:
                res["noise"][k] += 1
                flags.append(k)
        ret = coverage(strip_raw_noise(a["description"]), b["description"])
        rets.append(ret)
        if ret < MIN_FINDING_RETENTION:
            flags.append(f"retention {ret:.2f}")
        if not title_matches(a["title"], b["title"]):
            title_bad += 1
            flags.append("title differs")
        if fid in gold_map:
            # The reference can carry the same noise (Nethermind's copy keeps a few sponsor comments), so strip it too.
            r, p = gold_scores(b["description"], strip_raw_noise(gold_map[fid]["description"]))
            recalls.append(r)
            precs.append(p)
            if r < MIN_GOLD_RECALL:
                flags.append(f"gold recall {r:.2f}")
        if flags and fid in reviewed:
            res["warn"].append(f"{fid}: accepted ({'; '.join(flags)}): {reviewed[fid]}")
            res["reviewed"] = res.get("reviewed", 0) + 1
            for k in hits:
                res["noise"][k] -= hits[k]
            rets[-1] = max(rets[-1], MIN_FINDING_RETENTION)
            if fid in gold_map:
                recalls[-1] = max(recalls[-1], MIN_GOLD_RECALL)
            if "title differs" in flags:
                title_bad -= 1
            flags = []
        if flags:
            res["flagged"].append({"finding_id": fid, "flags": flags, "title": b["title"][:100],
                                   "source": sources.get(fid, "")})
    for k in BLOCKING:
        if res["noise"][k]:
            res["fail"].append(f"{DETECTORS[k][0].lower()} left in {res['noise'][k]} finding(s)")
    if res["noise"]["glued_code"]:
        res["warn"].append(f"possible glued inline code in {res['noise']['glued_code']} finding(s)")
    low = sum(r < MIN_FINDING_RETENTION for r in rets)
    res["retention_mean"], res["retention_min"] = sum(rets) / len(rets), min(rets)
    if low:
        res["fail"].append(f"{low} finding(s) below {MIN_FINDING_RETENTION:.2f} retention")
    if res["retention_mean"] < MIN_MEAN_RETENTION:
        res["fail"].append(f"mean retention {res['retention_mean']:.2f} < {MIN_MEAN_RETENTION:.2f}")
    if title_bad:
        res["warn"].append(f"{title_bad} title(s) differ from the original beyond truncation/spacing")
    if recalls:
        res["gold_recall"], res["gold_precision"] = sum(recalls) / len(recalls), sum(precs) / len(precs)
        if any(r < MIN_GOLD_RECALL for r in recalls):
            res["fail"].append(f"{sum(r < MIN_GOLD_RECALL for r in recalls)} finding(s) below gold recall {MIN_GOLD_RECALL}")
    dup = [s for s, c in Counter(sources.get(v["finding_id"]) for v in cv).items() if s and c > 1]
    if dup:
        res["warn"].append(f"source reused by several findings: {dup[:3]}")
    res["noise"] = dict(res["noise"])
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original", type=Path)
    ap.add_argument("cleaned", type=Path)
    ap.add_argument("--projects", nargs="+")
    ap.add_argument("--gold", type=Path, help="directory of reference <project_id>.json files")
    ap.add_argument("--log", type=Path, help="provenance log from clean_findings.py")
    ap.add_argument("--reviewed", type=Path, help="JSON {finding_id: reason} of accepted flags")
    ap.add_argument("--json", type=Path, help="write the full report here")
    args = ap.parse_args()

    orig = {p["project_id"]: p for p in json.loads(args.original.read_text())}
    clean = {p["project_id"]: p for p in json.loads(args.cleaned.read_text())}
    if args.projects:
        ids = args.projects
    else:
        ids = [pid for pid in clean if pid in orig and clean[pid]["vulnerabilities"] != orig[pid]["vulnerabilities"]]
    unchanged = [pid for pid in clean if pid in orig and pid not in ids]
    if unchanged and not args.projects:
        print(f"skipped {len(unchanged)} unchanged project(s): {', '.join(unchanged)} (pass --projects to check them)")
    missing = [pid for pid in ids if pid not in orig or pid not in clean]
    if missing:
        raise SystemExit(f"missing from original or cleaned dataset: {missing}")
    gold_dir = args.gold
    if gold_dir and (gold_dir / "data" / "source_of_truth").is_dir():
        gold_dir = gold_dir / "data" / "source_of_truth"
    reviewed = json.loads(args.reviewed.read_text()) if args.reviewed else {}
    sources, added = {}, set()
    if args.log:
        log = json.loads(args.log.read_text())
        sources = {e["finding_id"]: e.get("source", "") for e in log}
        added = {e["finding_id"] for e in log if e.get("status") == "added"}

    report, failed = [], 0
    for pid in ids:
        gold_path = gold_dir / f"{pid}.json" if gold_dir else None
        gold = json.loads(gold_path.read_text()) if gold_path and gold_path.exists() else None
        r = check_project(orig[pid], clean[pid], gold, sources, reviewed, added)
        report.append(r)
        verdict = "FAIL" if r["fail"] else "PASS*" if r.get("reviewed") else "PASS"
        failed += verdict == "FAIL"
        extra = f" gold R={r['gold_recall']:.3f} P={r['gold_precision']:.3f}" if "gold_recall" in r else ""
        print(f"{verdict:5} {pid:58} n={r['findings']:3} retention mean={r.get('retention_mean', 0):.3f} "
              f"min={r.get('retention_min', 0):.3f}{extra}")
        for msg in r["fail"]:
            print(f"     fail: {msg}")
        for msg in r["warn"]:
            print(f"     warn: {msg}")
    print(f"\n{len(ids) - failed}/{len(ids)} projects pass")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
