#!/usr/bin/env python3
"""
Compare ScaBench finding text with Nethermind's cleaned copy and render an HTML report.

  compare_descriptions.py --nethermind PATH [--dataset JSON] [--out report.html] [--summary summary.json]

PATH is a checkout of https://github.com/NethermindEth/auditagent-scoring-algo (its data/source_of_truth
holds cleaned copies of 11 ScaBench projects). The scorer is pinned at a62a15d1ea254158dd5cda4bea8ce8cf5791fb27.

For the overlapping projects every finding is diffed token by token. Whitespace and backticks are ignored,
so formatting-only edits (glued words, re-spaced code) separate cleanly from removed text, and each removed
run is labelled by the marker it contains (credits, sponsor/judge discussion, report spill-over). For all
projects, heuristic detectors count the same noise in the raw ScaBench text; they are validated against the
Nethermind diff where both exist.
"""

import argparse
import difflib
import html
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DATASET = HERE.parent / "curated-2025-08-18.json"
SCORER_REF = "a62a15d1ea254158dd5cda4bea8ce8cf5791fb27"

# Backticks are dropped from tokens so restored inline-code markup counts as formatting.
TOK = re.compile(r"\w+|[^\w\s`]")
# Markers searched in a removed run (tokens joined by single spaces). Order sets precedence at equal offsets.
MARKERS = [
    # Mitigation-review findings ("Submitted by X Severity : Medium …") are whole extra findings, not credits.
    ("spillover", re.compile(r"\bSubmitted by \S+ (?:, )?Severity :|\bC4 is an open organization\b"
                             r"|\bFor this audit , \d+ reports? w")),
    ("credits", re.compile(r"\bSubmitted by\b|\balso found by\b|\bFound by\b")),
    ("discussion", re.compile(
        r"\( judge \)|\) (?:confirmed|acknowledged|disputed|commented|marked|decreased|increased|disagreed)\b"
        r"|\b(?:confirmed|acknowledged|disputed) and commented\b|\bCode4rena mitigation\b"
        r"|\bMitigation (?:confirmed|review)\b|\bStatus :")),
]
REMOVED_KINDS = ["credits", "discussion", "spillover", "other"]

# Heuristic detectors over raw ScaBench text (all projects).
DETECTORS = {
    "credits": ("Contributor credits", "“Submitted by …, also found by …” / “Found by”",
                re.compile(r"^\s*Submitted by|\bFound by\b")),
    "discussion": ("Sponsor / judge discussion", "post-contest comments such as “X (judge) commented”",
                   re.compile(r"\((?:judge|[^()\n]{1,80})\)\s*(?:confirmed|acknowledged|disputed|commented|marked|"
                              r"decreased|increased|disagreed)|sherlock-admin|protocol has acknowledged|"
                              r"Code4rena mitigation|\bMitigation (?:confirmed|review)\b")),
    "spillover": ("Report spill-over", "C4 disclaimer footer, QA report or mitigation-review findings appended to a finding",
                    re.compile(r"C4 is an open organization|For this audit, \d+ reports? were submitted")),
    "glued_code": ("Glued inline code", "inline code fused to neighbouring words, e.g. “inisExpired()”, “()will”",
                   re.compile(r"\b(?:the|The|in|In|of|on|to|for|and|call|uses|via|through|that|by|from|with|Both)"
                              r"[A-Z_][A-Za-z0-9_]*|\(\)[A-Za-z]")),
    "run_together": ("Run-together words", "PDF extraction lost word spacing (5+ distinct lowercase runs of 20+ letters)",
                     None),
    "split_identifiers": ("Split identifiers", "identifiers broken apart, e.g. “ERC 20”, “uint 256”, “#L 508”",
                          re.compile(r"\b(?:ERC|uint|int|bytes|V) \d+\b|#L \d")),
    "pdf_artifacts": ("Page numbers", "PDF page numbers mid-sentence or “,→” wrap glyphs",
                      re.compile(r"[ \n]\d{1,3}\n\n(?=[a-z(])|,→")),
    "damaged_title": ("Title cut off or fused", "title ends mid-word (“collat-”) or on a dangling word, or fuses words "
                      "(“Asingledepositorcangriefother”)",
                      re.compile(r"(?:\b(?i:in|of|the|to|for|and|a|an|with|on|by|from|when|if|that|is|due|are)|[a-z]-)$"
                                 r"|\b[A-Za-z][a-z]{17,}\b")),
}
RUN_TOGETHER = re.compile(r"\b[a-z]{20,}\b")


def tokens(text):
    return [(m.group(), m.start(), m.end()) for m in TOK.finditer(text)]


def stitch(text, toks):
    """Join tokens, keeping a space only where the source had whitespace between them."""
    out = []
    for k, (t, s, _) in enumerate(toks):
        if k and re.search(r"\s", text[toks[k - 1][2]:s]):
            out.append(" ")
        out.append(t)
    return "".join(out)


def categorize(run):
    """Split a removed token run into (kind, start, end) slices by the first marker in each part."""
    joined, offs, pos = "", [], 0
    for t, _, _ in run:
        offs.append(pos)
        joined += t + " "
        pos += len(t) + 1
    hits = sorted((m.start(), order, kind) for order, (kind, rx) in enumerate(MARKERS) for m in rx.finditer(joined))
    cuts, last, seen = [], None, set()
    for start, _, kind in hits:
        idx = max(i for i, o in enumerate(offs) if o <= start)
        if idx in seen:  # the highest-precedence marker at a token wins
            continue
        seen.add(idx)
        if kind != last:
            cuts.append((idx, kind))
            last = kind
    if not cuts:
        return [("other", 0, len(run))]
    # A short lead-in ("0xnev ( judge ) commented" starts with the name) belongs to the first marker.
    if cuts[0][0] <= 6:
        cuts[0] = (0, cuts[0][1])
    else:
        cuts.insert(0, (0, "other"))
    bounds = [c[0] for c in cuts] + [len(run)]
    return [(kind, bounds[i], bounds[i + 1]) for i, (_, kind) in enumerate(cuts) if bounds[i] < bounds[i + 1]]


def diff_text(a_text, b_text):
    a, b = tokens(a_text), tokens(b_text)
    sm = difflib.SequenceMatcher(None, [t[0] for t in a], [t[0] for t in b], autojunk=False)
    segs, removed = [], Counter()

    def push(kind, text):
        if not text:
            return
        if segs and segs[-1][0] == kind:
            segs[-1][1] += " " + text
        else:
            segs.append([kind, text])

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            push("eq", stitch(a_text, a[i1:i2]))
            continue
        da, db = a[i1:i2], b[j1:j2]
        if da and db and "".join(t[0] for t in da) == "".join(t[0] for t in db):
            push("fmt", stitch(b_text, db))  # same characters, only spacing differs (glued words)
            continue
        for kind, s, e in categorize(da) if da else []:
            part = da[s:e]
            push(kind, stitch(a_text, part))
            removed[kind] += part[-1][2] - part[0][1]
        if db:
            push("ins", stitch(b_text, db))
    return segs, removed


def title_change(a, b):
    """same / backticks (markup only) / spacing (glued words split apart) / editorial (wording changed)."""
    if a == b:
        return "same"
    if a == b.replace("`", ""):
        return "backticks"
    return "spacing" if re.sub(r"[\s`]", "", a) == re.sub(r"[\s`]", "", b) else "editorial"


def detect(v):
    desc, title = v.get("description") or "", v.get("title") or ""
    hits = {}
    for key, (_, _, rx) in DETECTORS.items():
        if key == "run_together":
            hits[key] = len(set(RUN_TOGETHER.findall(desc))) >= 5
        elif key == "damaged_title":
            hits[key] = bool(rx.search(title.strip()))
        elif key in ("glued_code", "split_identifiers"):
            hits[key] = bool(rx.search(title) or rx.search(desc))
        else:
            hits[key] = bool(rx.search(desc))
    return hits


def build(dataset, nm_dir):
    projects = json.loads(Path(dataset).read_text())
    nm = {}
    for f in sorted(Path(nm_dir).glob("*.json")):
        d = json.loads(f.read_text())
        nm[d["project_id"]] = d

    overlap_projects, findings = [], []
    sev_pairs, title_kinds = Counter(), Counter()
    validation = {k: Counter() for k in ("credits", "discussion", "spillover", "glued_code_title")}
    for p in projects:
        pid = p["project_id"]
        if pid not in nm:
            continue
        ours = {v["finding_id"]: v for v in p["vulnerabilities"]}
        agg = {"project_id": pid, "findings": 0, "chars_scabench": 0, "chars_nethermind": 0,
               "removed": Counter(), "severity_relabel": 0,
               **{"title_" + k: 0 for k in ("same", "backticks", "spacing", "editorial")},
               "ids_match": set(ours) == {v["finding_id"] for v in nm[pid]["vulnerabilities"]}}
        for v in nm[pid]["vulnerabilities"]:
            o = ours[v["finding_id"]]
            segs, removed = diff_text(o["description"], v["description"])
            tsegs, _ = diff_text(o["title"], v["title"])
            tk = title_change(o["title"], v["title"])
            title_kinds[tk] += 1
            agg["findings"] += 1
            agg["chars_scabench"] += len(o["description"])
            agg["chars_nethermind"] += len(v["description"])
            agg["removed"].update(removed)
            agg["title_" + tk] += 1
            if o["severity"] != v["severity"]:
                sev_pairs[(o["severity"], v["severity"])] += 1
                agg["severity_relabel"] += 1
            hits = detect(o)
            for k in ("credits", "discussion", "spillover"):
                validation[k][(hits[k], removed[k] > 0)] += 1
            glued = bool(DETECTORS["glued_code"][2].search(o["title"]))
            validation["glued_code_title"][(glued, tk in ("spacing", "editorial"))] += 1
            findings.append({
                "project_id": pid, "finding_id": v["finding_id"],
                "severity": o["severity"], "severity_nethermind": v["severity"],
                "title": o["title"], "title_nethermind": v["title"], "title_change": tk, "title_segs": tsegs,
                "description": o["description"], "description_nethermind": v["description"],
                "segs": segs, "removed": {k: removed[k] for k in REMOVED_KINDS},
            })
        agg["removed"] = {k: agg["removed"][k] for k in REMOVED_KINDS}
        overlap_projects.append(agg)

    scan = []
    for p in projects:
        counts = Counter()
        for v in p["vulnerabilities"]:
            for k, hit in detect(v).items():
                counts[k] += hit
        scan.append({"project_id": p["project_id"], "platform": p["project_id"].split("_")[0],
                     "findings": len(p["vulnerabilities"]), "in_nethermind": p["project_id"] in nm,
                     "counts": {k: counts[k] for k in DETECTORS}})

    def score(c):
        tp, fp, fn = c[(True, True)], c[(True, False)], c[(False, True)]
        return {"tp": tp, "fp": fp, "fn": fn, "tn": c[(False, False)],
                "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None}

    tot_s = sum(a["chars_scabench"] for a in overlap_projects)
    removed_tot = Counter()
    for a in overlap_projects:
        removed_tot.update(a["removed"])
    summary = {
        "generated": date.today().isoformat(),
        "dataset": str(Path(dataset).name),
        "scorer_ref": SCORER_REF,
        "overlap_projects": len(overlap_projects),
        "overlap_findings": len(findings),
        "all_projects": len(projects),
        "all_findings": sum(len(p["vulnerabilities"]) for p in projects),
        "ids_match_all": all(a["ids_match"] for a in overlap_projects),
        "chars_scabench": tot_s,
        "chars_nethermind": sum(a["chars_nethermind"] for a in overlap_projects),
        "removed": {k: removed_tot[k] for k in REMOVED_KINDS},
        "title_changes": dict(title_kinds),
        "severity_relabels": [{"scabench": a, "nethermind": b, "count": n} for (a, b), n in sev_pairs.items()],
        "findings_with_insertions": sum(any(s[0] == "ins" for s in f["segs"]) for f in findings),
        "inserted_text": sorted({s[1] for f in findings for s in f["segs"] if s[0] == "ins"}),
        "detector_validation": {k: score(c) for k, c in validation.items()},
    }
    return summary, overlap_projects, findings, scan


def render(summary, overlap_projects, findings, scan, template):
    detectors = [{"key": k, "label": lab, "help": hlp} for k, (lab, hlp, _) in DETECTORS.items()]
    data = {"summary": summary, "projects": overlap_projects, "findings": findings, "scan": scan,
            "detectors": detectors}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return template.replace("/*__DATA__*/null", blob).replace("__GENERATED__", html.escape(summary["generated"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nethermind", required=True, type=Path,
                    help="auditagent-scoring-algo checkout, or its data/source_of_truth directory")
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--out", type=Path, default=HERE / "report.html")
    ap.add_argument("--summary", type=Path, default=HERE / "summary.json")
    args = ap.parse_args()
    nm_dir = args.nethermind / "data" / "source_of_truth" if (args.nethermind / "data").is_dir() else args.nethermind

    summary, overlap_projects, findings, scan = build(args.dataset, nm_dir)
    template = (HERE / "report_template.html").read_text()
    args.out.write_text(render(summary, overlap_projects, findings, scan, template))
    args.summary.write_text(json.dumps({"summary": summary, "projects": overlap_projects, "scan": scan}, indent=2) + "\n")
    rm = summary["removed"]
    print(f"{summary['overlap_findings']} findings in {summary['overlap_projects']} projects; "
          f"removed chars {sum(rm.values())}/{summary['chars_scabench']} {rm}; titles {summary['title_changes']}")
    print(f"wrote {args.out} and {args.summary}")


if __name__ == "__main__":
    main()
