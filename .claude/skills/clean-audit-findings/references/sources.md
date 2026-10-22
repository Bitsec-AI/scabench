# Finding sources and their quirks

Notes gathered while cleaning `curated-2025-08-18` (31 projects). Read the section for the platform you're working on when a project fails the check or the parser needs changing.

## Contents
- Code4rena report pages
- Sherlock judging issues
- Nethermind reference copies
- Cantina
- What the scraped text looks like (for recognising noise)

## Code4rena report pages

URL: `https://code4rena.com/reports/<slug>`, where `<slug>` is the finding ID without its `_H-01` suffix. The old `code-423n4/code423n4.com` repo no longer holds report Markdown, so the page is the source.

Structure: the report body is the `div.report-contents` (a second, smaller `markdown-body` exists, so use the class, not the first match). Its direct children are:
- `h1` sections: Overview, Summary, Scope, Severity Criteria, *High Risk Findings (n)*, *Medium Risk Findings (n)*, *Low Risk and Non-Critical Issues*, sometimes *Mitigation Review*, then *Disclosures*.
- One `h2` per finding, labelled `[H-01] Title` / `[M-03] Title`. QA items use `[01]`, `[L-01]`, `[QA-01]` or `[N-01]` (non-critical).
- Inside a finding: paragraphs, `pre` code blocks (`data-language` holds the language), lists, blockquotes, tables, and sub-headings, usually `h3` but **sometimes `h2`** ("Proof of Concept" at `h2` in lambowin).

Parsing rules that matter:
- A finding ends at the next **labelled** `h2` or any `h1`. An unlabelled `h2` is a sub-heading. Don't end on `<hr>`: starknet-perpetual H-02 has a rule before its PoC.
- The first paragraph is usually `Submitted by X, also found by …` (credits). Drop it.
- Post-judging discussion starts with a bold paragraph such as `Owl (Liquid Ron) confirmed and commented via duplicate issue S-174:`, `0xsomeone (judge) increased severity to High`, `KupiaSec (warden) commented`, or `Liquid Ron mitigated:`, followed by blockquotes, and ends with `Status: Mitigation confirmed …`. Newer reports (2025) drop the "(Sponsor)" tag and use a short, fully bold line: `Blackhole commented:`, `Kinetiq disputed and commented:`, `Virtuals marked as informative`. Everything from the first such paragraph is discussion. It always trails the finding body.
- **Mitigation Review reuses labels.** A report's *Mitigation Review* section can have `h2`s like `[M-05] Unmitigated`. ScaBench's scraper let these overwrite the audit's own findings: bakerfi M-01, M-07 and M-16 and blackhole M-05 and M-10 are titled "Unmitigated" in `curated-2025-08-18` and hold mitigation-review text. Only take labelled `h2`s under the High/Medium/Low `h1`s, and keep the first section for a label. When the scraped title is junk and no section's title matches, the H/M label is authoritative: use it, note it in the log, and expect the checker to flag low retention, because the original was a different text.
- QA numbering doesn't line up with ScaBench's `L-n` everywhere. forte-float128 restarts `[01]` for each file (Float128.sol, then Ln.sol) and repeats the title "Recommendations/how to fix". Map `L-n` to the n-th QA item, verify by title, and when titles collide pick the section whose text best overlaps the scraped description.
- The scrape can also miss QA items outright: forte-float128's `L-01`–`L-06` are the Ln.sol items, and its three Float128.sol items were missing. Add such items after the existing findings with new IDs (`L-07`…), never renumber, and log them with `"status": "added"` so `check_findings.py` accepts them.
- ScaBench's scraped *last low* often swallowed the non-critical `[N-xx]` items that follow it, and the last finding of a section often carries the QA intro ("For this audit, N reports were submitted…"), mitigation-review findings ("Submitted by X Severity: Medium …") or the C4 disclaimer. The section boundary removes all of these.

## Sherlock judging issues

Sherlock reports are PDFs (that's where ScaBench's damaged text came from), but each finding is a GitHub issue in `sherlock-audit/<contest>-judging`. The report's per-finding `Source:` line links to it.

Issue body format (2024–2025 contests):
```
<Watson handle>

<High|Medium>

# <Full title>

### Summary
…
```
Older ones use `## Summary` / `## Vulnerability Detail`. The issue title is `<handle> - <title>`. After escalations, Sherlock bots append `## Discussion` with sherlock-admin comments; cut everything from there. Template sections that were left empty contain `_No response_`; drop them.

Matching problems seen in practice:
- **Broken links:** the PDF wraps URLs (`…/2024-08-perennial-v 2-update-3-judgin\ng/issues/27`) or truncates them (`…/2024-08-morphl`). Join whitespace inside the URL; failing that, search `org:sherlock-audit` repos by the name prefix for one ending in `-judging`.
- **Quoted links to other contests** (morph cites an Optimism judging issue): accept a linked repo only when several findings point at it.
- **Private audits:** the issues live in the contest repo itself, not `-judging` (tally: `sherlock-audit/2024-11-tally`), with no handle header.
- **Renamed contracts:** the report can rename contracts the issues still use the old name for (axion's PDF says "V 3 AMO" where issues say `SolidlyV3AMO`), so title matching fails. Always treat the issue a finding links to as a candidate, and rank it first.
- **Duplicates:** several issues can share a title. Prefer the linked one, then ones not labelled Duplicate/Invalid/Excluded, then the best description overlap.
- **Deleted or private judging repos** (idle-finance 2024-12 returns 404): no re-fetchable source. Report it and leave the original text.

The severity in the issue header is what the Watson *submitted*; judges re-grade often (about 30 of 117 findings here), so a mismatch is normal. Keep the dataset severity; the log notes the difference.

## Nethermind reference copies

`NethermindEth/auditagent-scoring-algo`, `data/source_of_truth/<project_id>.json` (scorer pinned at `a62a15d1…`), covers 11 ScaBench projects (9 Code4rena, 2 Cantina) with the same finding IDs. Format: whitespace collapsed to one line, code fences inline (```` ```code``` ````), backticks restored, `informational` written as `info`.

It is good but not perfect, so treat it as a reference, not truth (in `curated-2025-08-18` these findings were re-fetched instead of adopted):
- **Wrong findings:** bakerfi M-01, M-07 and M-16 are the Mitigation Review's "Unmitigated" entries, inherited from the ScaBench scrape, not the audit's findings.
- **Sponsor comments left in:** iq-ai H-01 ("tom2o17 (IQ AI) disputed and commented …"); kinetiq H-01, H-02, M-04 and M-05 ("Kinetiq disputed and commented:") and L-17 ("Kinetiq commented:").
- **Mitigation-review introduction left in:** bakerfi L-09, iq-ai L-05 and liquid-ron L-02 still end with it.
- **Extra items:** iq-ai L-05 still includes the `[N-01]`–`[N-03]` non-critical items.

Elsewhere, the fetch-and-clean path reaches 0.999–1.000 word recall against it.

## Cantina

Both Cantina projects in the dataset have a Nethermind copy. Their scraped text has no credits or discussion, only glued inline code ("TheConsensusRegistrycontract"). There is no fetcher yet. A new Cantina project needs a source (the report page or PDF on cantina.xyz) before it can be cleaned properly.

## What scraped text looks like

| Noise | Example (raw scraped text) |
|---|---|
| credits | `Submitted by\n0x04bytes\n, also found by\n056Security\n, 0rpse (\n1\n,\n2\n), …` |
| discussion | `blockchainstar12 (Coded Estate) acknowledged and commented\n:\n\nActually, the platform …`, `Blackhole mitigated\n\nStatus:\nMitigation confirmed.` |
| spill-over | `For this audit, 9 reports were submitted by wardens detailing low risk …` (a whole QA report), `C4 is an open organization governed by …` |
| glued code | `inisExpired()`, `killGauge()will lead`, `TheVoterUpgradeableV2.createV3Gaugefunction` |
| PDF damage (Sherlock) | titles `Market coordinator can steal all market collat-`, `Asingledepositorcangriefother`; body `Anuncheckedbatchreversionwillcause…`, `uint 256`, `Safe ERC 20.safe Transfer`, page numbers mid-sentence |
