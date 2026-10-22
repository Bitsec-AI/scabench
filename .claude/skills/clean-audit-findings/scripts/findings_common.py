"""
Shared helpers for clean_findings.py and check_findings.py: noise markers, detectors, diffing and fuzzy matching.
Standard library only.
"""

import difflib
import re
from collections import Counter

# ---------------------------------------------------------------------------
# Tokens: whitespace and backticks are ignored, so re-spacing and restored inline-code markup never count as edits.
TOK = re.compile(r"\w+|[^\w\s`]")


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


# ---------------------------------------------------------------------------
# Noise markers, searched in token strings (tokens joined by single spaces). Earlier entries win at the same token.
MARKERS = [
    # Text glued on from elsewhere in the report: mitigation-review findings, the QA intro, the C4 disclaimer.
    ("spillover", re.compile(r"\bSubmitted by \S+ (?:, )?Severity :|\bC4 is an open organization\b"
                             r"|\bFor this audit , \d+ reports? w")),
    ("credits", re.compile(r"\bSubmitted by\b|\balso found by\b|\bFound by\b")),
    ("discussion", re.compile(
        r"\( judge \)|\) (?:confirmed|acknowledged|disputed|commented|marked|decreased|increased|disagreed)\b"
        r"|\b(?:confirmed|acknowledged|disputed) and commented\b|\bCode4rena mitigation\b"
        r"|\bMitigation (?:confirmed|review)\b|\bStatus :")),
]
REMOVED_KINDS = ["credits", "discussion", "spillover", "other"]


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
    """Token diff of a -> b. Returns (segments, removed chars per kind); segments are [kind, text] with kind in
    eq / fmt (same characters, re-spaced) / ins / credits / discussion / spillover / other."""
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
            push("fmt", stitch(b_text, db))
            continue
        for kind, s, e in categorize(da) if da else []:
            part = da[s:e]
            push(kind, stitch(a_text, part))
            removed[kind] += part[-1][2] - part[0][1]
        if db:
            push("ins", stitch(b_text, db))
    return segs, removed


# ---------------------------------------------------------------------------
# Detectors for noise left in finding text. Validated against Nethermind's cleaned copies (credits / discussion /
# spill-over: 100% precision and recall on 188 findings; glued code: 100% precision, ~36% recall, so a lower bound).
DETECTORS = {
    "credits": ("Contributor credits", re.compile(r"^\s*\**Submitted by|\bFound by\b")),
    "discussion": ("Sponsor / judge discussion", re.compile(
        r"\((?:judge|[^()\n]{1,80})\)\s*(?:confirmed|acknowledged|disputed|commented|marked|decreased|increased|"
        r"disagreed)|sherlock-admin|protocol has acknowledged|Code4rena mitigation|\bMitigation (?:confirmed|review)\b"
        r"|(?m:^[A-Z][\w.\-]*(?: [\w.\-]+){0,3} (?:confirmed|acknowledged|disputed|commented|marked as \w[\w ]*)\s*:?\s*$)")),
    "spillover": ("Report spill-over", re.compile(r"C4 is an open organization|For this audit, \d+ reports? were submitted")),
    "glued_code": ("Glued inline code", re.compile(
        r"\b(?:the|The|in|In|of|on|to|for|and|call|uses|via|through|that|by|from|with|Both)[A-Z_][A-Za-z0-9_]*"
        r"|\(\)[A-Za-z]")),
    "run_together": ("Run-together words", None),
    "split_identifiers": ("Split identifiers", re.compile(r"\b(?:ERC|uint|int|bytes|V) \d+\b|#L \d")),
    "pdf_artifacts": ("Page numbers", re.compile(r"[ \n]\d{1,3}\n\n(?=[a-z(])|,→")),
    "damaged_title": ("Title cut off or fused", re.compile(
        r"(?:\b(?i:in|of|the|to|for|and|a|an|with|on|by|from|when|if|that|is|due|are)|[a-z]-)$"
        r"|\b(?![A-Za-z]*(?:collateral|ization|abilit|bilit|ational))[A-Za-z][a-z]{19,}\b")),
}
RUN_TOGETHER = re.compile(r"\b[a-z]{20,}\b")
# Detectors whose hits mean the text is not clean. glued_code is a heuristic and can fire on legit prose.
BLOCKING = ("credits", "discussion", "spillover", "run_together", "split_identifiers", "pdf_artifacts", "damaged_title")


def strip_code(text):
    """Drop fenced and inline code so detectors only look at prose (code legitimately has `uint 256`-like text rarely,
    and identifiers longer than 20 lowercase letters often)."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", " ", text)


def detect(title, desc, prose_only=False):
    """Return {detector: bool}. prose_only=True ignores code blocks (use on cleaned Markdown)."""
    body = strip_code(desc) if prose_only else desc
    t = strip_code(title) if prose_only else title
    hits = {}
    for key, (_, rx) in DETECTORS.items():
        if key == "run_together":
            hits[key] = len(set(RUN_TOGETHER.findall(body))) >= 5
        elif key == "damaged_title":
            # Judge the ending on the title with inline code kept (a title may end in `fn()`), fused words without it.
            hits[key] = bool(rx.search(re.sub(r"`[^`]*`", "CODE", title).strip())) if prose_only \
                else bool(rx.search(title.strip()))
        elif key == "split_identifiers":
            # One "ERC 20" can be the author's own spelling; PDF damage splits identifiers throughout.
            hits[key] = len(rx.findall(t)) + len(rx.findall(body)) >= 2
        elif key == "glued_code":
            hits[key] = bool(rx.search(t) or rx.search(body))
        else:
            hits[key] = bool(rx.search(body))
    return hits


# ---------------------------------------------------------------------------
# Fuzzy matching helpers.
def squash(text):
    """Lowercase alphanumerics only: immune to lost spaces, split identifiers and markup."""
    return re.sub(r"[^0-9a-z]", "", text.lower())


def title_matches(truncated, full):
    """True when a (possibly cut-off, fused or split) title is a prefix of, or close to, the full title."""
    a, b = squash(truncated), squash(full)
    if not a or not b:
        return False
    # Only the scraped title may be the truncated one; a much shorter full title ("Precision loss") is not a match.
    return b.startswith(a) or (a.startswith(b) and len(b) >= 0.9 * len(a)) \
        or difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def shingles(text, k=12):
    s = squash(text)
    return {s[i:i + k] for i in range(0, max(len(s) - k + 1, 1))}


def coverage(reference, candidate, k=12):
    """Share of the reference's character shingles that appear in the candidate (spacing-insensitive)."""
    ref = shingles(reference, k)
    return len(ref & shingles(candidate, k)) / len(ref) if ref else 1.0


# Text that is noise in scraped (or imperfectly cleaned) findings and should not count against content retention.
# Patterns that cut "to end" rely on noise always trailing the finding body in these reports.
RAW_NOISE = [
    # Sherlock credits: comma-separated handles over several PDF lines (runs before the Source rule, which could
    # otherwise swallow the "Found" of a wrapped line).
    re.compile(r"\bFound\s+by\s*\n?(?:[^\n]*,[ \t]*\n)*[^\n]*"),
    re.compile(r"^\s*Source:\s*https?://\S+(?:\s+\S+)?", re.M),                  # Sherlock source line (often wrapped)
    re.compile(r"^\s*Submitted by[\s\S]*?(?=\n\n|$)"),                            # C4 credits paragraph
    re.compile(r"(?s)\s\S{1,40}\s*\((?:judge|warden|[^()\n]{1,60})\)\s*"
               r"(?:confirmed|acknowledged|disputed|commented|marked|decreased|increased|disagreed)\b.*"),
    re.compile(r"(?s)\s\S+ mitigated\s*:?\s*(?:\n|Status\s*:).*"),
    # Sponsor status without "(Sponsor)": "Kinetiq disputed and commented:", "Blackhole commented:".
    re.compile(r"(?s)(?:^|\s)[A-Z][\w.\-]*(?: [\w.\-]+){0,2} (?:disputed|acknowledged|confirmed|commented|"
               r"marked as \w+)(?: and commented)?\s*:.*"),
    re.compile(r"(?s)For this audit, \d+ reports? were submitted.*"),
    re.compile(r"(?s)C4 is an open organization.*"),
    re.compile(r"(?s)(?:Following the C4 audit, \d+ wardens|During the (?:mitigation )?review, |"
               r"The table below provides|surfaced \d+ new (?:high|medium)).*"),           # mitigation-review intro
    re.compile(r"(?s)\n\s*Discussion\s*\n.*"),
    re.compile(r"[ \n]\d{1,3}\n\n(?=[a-z(])"),                                           # PDF page numbers
]


def strip_raw_noise(text):
    for rx in RAW_NOISE:
        text = rx.sub("\n", text)
    return text
