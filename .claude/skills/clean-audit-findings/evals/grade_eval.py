#!/usr/bin/env python3
"""
Grade one eval run of clean-audit-findings. Writes <run_dir>/grading.json in the skill-creator format.

  grade_eval.py EVAL_ID INPUT.json RUN_DIR [--gold DIR]

RUN_DIR/outputs/cleaned.json and RUN_DIR/outputs/notes.md are the run's deliverables. --gold is the Nethermind
source_of_truth directory (needed for evals with "gold": true).
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
from check_findings import MIN_GOLD_RECALL, MIN_FINDING_RETENTION, check_project  # noqa: E402
from findings_common import BLOCKING, DETECTORS, squash  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("eval_id", type=int)
    ap.add_argument("input", type=Path)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--gold", type=Path)
    args = ap.parse_args()

    spec = next(e for e in json.loads((HERE / "evals.json").read_text())["evals"] if e["id"] == args.eval_id)
    targets, skip = spec["targets"], set(spec["unresolvable"])
    out = args.run_dir / "outputs"
    exp = []

    def expect(text, passed, evidence):
        exp.append({"text": text, "passed": bool(passed), "evidence": evidence})

    orig_list = json.loads(args.input.read_text())
    orig = {p["project_id"]: p for p in orig_list}
    try:
        clean_list = json.loads((out / "cleaned.json").read_text())
        clean = {p["project_id"]: p for p in clean_list}
        ok = [p["project_id"] for p in clean_list] == [p["project_id"] for p in orig_list]
        expect("cleaned.json is valid and has the same projects in the same order", ok,
               f"{len(clean_list)} projects" + ("" if ok else " (project list differs)"))
    except Exception as e:  # noqa: BLE001
        expect("cleaned.json is valid and has the same projects in the same order", False, f"unreadable: {e}")
        return write(args.run_dir, exp)

    others = [pid for pid in orig if pid not in targets and clean.get(pid) != orig[pid]]
    expect("Projects outside the request are unchanged", not others, f"changed: {others[:5]}" if others else "none changed")

    # "reviewed" lists findings where the reference or the scraped original is known to be wrong (see evals.json).
    results = {pid: check_project(orig[pid], clean[pid], load_gold(args.gold, pid) if spec["gold"] else None, {},
                                  spec.get("reviewed", {}))
               for pid in targets}
    struct = [f"{pid}: {m}" for pid, r in results.items() for m in r["fail"] if "changed" in m or "empty" in m]
    expect("Finding IDs, order and severities are unchanged; no empty titles/descriptions", not struct,
           "; ".join(struct[:5]) or "all target projects intact")

    resolvable = [pid for pid in targets if pid not in skip]
    changed = [pid for pid in resolvable if clean[pid]["vulnerabilities"] != orig[pid]["vulnerabilities"]]
    expect("Every resolvable target project was actually rewritten", len(changed) == len(resolvable),
           f"{len(changed)}/{len(resolvable)} rewritten")

    noisy = {pid: {DETECTORS[k][0]: n for k, n in results[pid]["noise"].items() if k in BLOCKING and n}
             for pid in resolvable}
    noisy = {pid: v for pid, v in noisy.items() if v}
    expect("No credits, discussion, spill-over or PDF damage left in resolvable projects", not noisy,
           json.dumps(noisy)[:400] if noisy else "clean")

    # Glued inline code: only checkable where clean text backticks code (Code4rena); Sherlock authors often don't,
    # and plain camelCase identifiers (onDropMessage) look like glue to the regex.
    if all(pid.startswith("code4rena_") for pid in resolvable):
        rx = DETECTORS["glued_code"][1]
        glued = [v["finding_id"] for pid in resolvable for v in clean[pid]["vulnerabilities"]
                 if rx.search(re.sub(r"`[^`]*`", " ", v["title"]))]
        expect("No title has inline code glued to neighbouring words", not glued,
               f"{len(glued)} titles: {glued[:5]}" if glued else "none")

    if spec["gold"]:
        gold_titles = {v["finding_id"]: v["title"] for pid in targets
                       for v in (load_gold(args.gold, pid) or {"vulnerabilities": []})["vulnerabilities"]}
        diff = [v["finding_id"] for pid in targets for v in clean[pid]["vulnerabilities"]
                if v["finding_id"] in gold_titles and squash(v["title"]) != squash(gold_titles[v["finding_id"]])]
        expect("Titles match Nethermind's titles (ignoring spacing and markup)", not diff,
               f"{len(diff)} differ: {diff[:5]}" if diff else f"all {len(gold_titles)} match")

    low = {pid: [f["finding_id"] for f in results[pid]["flagged"] if any(x.startswith("retention") for x in f["flags"])]
           for pid in resolvable}
    low = {k: v for k, v in low.items() if v}
    mean = sum(results[pid]["retention_mean"] for pid in resolvable) / max(len(resolvable), 1)
    expect(f"No finding loses content (retention >= {MIN_FINDING_RETENTION} for every finding)", not low,
           f"mean retention {mean:.3f}; low: {json.dumps(low)[:300]}" if low else f"mean retention {mean:.3f}")

    for fid, bad_title in spec.get("expect_titles_not", {}).items():
        pid = next(pid for pid in targets if any(v["finding_id"] == fid for v in clean[pid]["vulnerabilities"]))
        title = next(v["title"] for v in clean[pid]["vulnerabilities"] if v["finding_id"] == fid)
        expect(f"{fid} holds the audit's own finding, not the Mitigation Review entry '{bad_title}'",
               title.strip() != bad_title, f"title: {title[:90]!r}")

    if skip:
        notes = (out / "notes.md").read_text() if (out / "notes.md").exists() else ""
        untouched = all(clean[pid] == orig[pid] for pid in skip)
        mentioned = all(pid.split("_")[1].split("-")[0] in notes.lower() or pid in notes for pid in skip)
        expect("Unresolvable projects are left as they were (not patched or invented) and reported in notes.md",
               untouched and mentioned, f"unchanged={untouched}, mentioned in notes={mentioned}")

    if spec["gold"]:
        rec = {pid: round(results[pid].get("gold_recall", 0), 3) for pid in targets}
        bad = {pid: [f["finding_id"] for f in results[pid]["flagged"] if any(x.startswith("gold") for x in f["flags"])]
               for pid in targets}
        bad = {k: v for k, v in bad.items() if v}
        expect(f"Descriptions match Nethermind's cleaned copies (word recall >= {MIN_GOLD_RECALL} per finding)",
               not bad, f"mean recall {json.dumps(rec)}; below: {json.dumps(bad)}")

    return write(args.run_dir, exp)


def load_gold(gold_dir, pid):
    path = gold_dir / f"{pid}.json"
    return json.loads(path.read_text()) if path.exists() else None


def write(run_dir, exp):
    passed = sum(e["passed"] for e in exp)
    grading = {"expectations": exp,
               "summary": {"passed": passed, "failed": len(exp) - passed, "total": len(exp),
                           "pass_rate": round(passed / len(exp), 3) if exp else 0.0}}
    (run_dir / "grading.json").write_text(json.dumps(grading, indent=2) + "\n")
    print(json.dumps(grading["summary"]))


if __name__ == "__main__":
    main()
