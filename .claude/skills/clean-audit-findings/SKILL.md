---
name: clean-audit-findings
description: Replace scraped audit-finding titles and descriptions in a ScaBench-style dataset with clean text from the original source (Code4rena report pages, Sherlock judging-repo issues, or an existing cleaned copy such as Nethermind's auditagent-scoring-algo ground truth), then verify that nothing was lost and no noise remains. Use this whenever new projects are scraped or added to a dataset, before building benchmark tasks, ground truth or baselines from one, or when finding text shows scraper damage — "Submitted by … also found by" credits, sponsor/judge comments, QA-report or C4-disclaimer text tacked onto a finding, glued inline code ("inisExpired()"), or PDF damage (titles cut off like "collat-", fused words like "Asingledepositorcangriefother", split identifiers like "ERC 20"). Also use it when the user says "clean the findings", "fix the descriptions", "re-scrape the text" or just that the finding text looks messy.
---

# Clean audit findings

The finding text in a benchmark dataset is the ground truth an LLM judge reads when it decides whether an agent found a bug. Scrapers mangle it in predictable ways: they keep contributor credit lists and post-judging discussion, glue the next report section onto the last finding, fuse inline code into neighbouring words, and, for PDF-sourced reports, cut titles at the first line break and run words together. Noise costs judge tokens, and some of it actively misleads (a judge's comment arguing severity, or a QA report about other bugs attached to one finding). Patching scraped text with regexes can't restore lost spaces or truncated titles, so this skill **re-fetches each finding from its original source** and checks the result against the scraped text to prove nothing was dropped.

## What "clean" means

- **Title:** the complete title, with inline code in backticks.
- **Description:** the finding's own body as Markdown, including the warden's text, code blocks, PoC and mitigation, and nothing else. No credits, no sponsor/judge/warden comments or mitigation status, and no text from other report sections (QA intro, other findings, mitigation review, disclaimer).
- **Unchanged:** finding IDs, order and severities. Only `title` and `description` change.

## Inputs

A dataset JSON: a list of projects shaped like `{"project_id", "platform", "codebases", "vulnerabilities": [{"finding_id", "severity", "title", "description"}]}`. Code4rena finding IDs must keep the report slug, as in `2024-12-lambowin_M-10`, because that is how the report page is found.

## Step 1: clean into a scratch copy

```bash
uv run --with beautifulsoup4 python .claude/skills/clean-audit-findings/scripts/clean_findings.py <dataset.json> \
  --projects <id> ... | --platform code4rena sherlock | --all \
  [--nethermind <auditagent-scoring-algo checkout>] \
  --out <scratch>/cleaned.json --cache <scratch>/clean-cache
```

Sources, in order of preference (details and quirks in `references/sources.md`):

| Source | Used when | What the script does |
|---|---|---|
| Nethermind copy | `--nethermind` is given and `data/source_of_truth/<project_id>.json` exists | adopts title and description verbatim. The IDs must match exactly, or the script refuses |
| Code4rena | `platform == code4rena` | fetches `code4rena.com/reports/<slug>`, takes the finding's section up to the next finding, converts it to Markdown, and drops the credits paragraph and everything from the first sponsor/judge comment on |
| Sherlock | `platform == sherlock` | finds the judging repo from the findings' `Source:` links (or the repo-name prefix), fetches all issues with `gh`, matches each finding by its (possibly truncated) title, and takes the issue Markdown minus its header, "Discussion" and empty template sections |
| anything else | e.g. Cantina | left unchanged and reported (no fetcher yet) |

Requirements: `beautifulsoup4` for Code4rena and an authenticated `gh` for Sherlock (unauthenticated API limits are too low). Fetches are cached, so reruns are cheap. The script writes a provenance log (`<out>.clean-log.json`) with a source URL and status for every finding. It exits 2 if any finding couldn't be resolved; add `--allow-partial` to keep going.

When a source is gone (a deleted or private judging repo, say), the finding keeps its original text and the log marks it `failed`. Don't hand-write or regex-patch a replacement: report it to the user, who may know another source or accept the old text.

## Step 2: check, then read the flags

```bash
python3 .claude/skills/clean-audit-findings/scripts/check_findings.py <dataset.json> <scratch>/cleaned.json \
  --projects <id> ... --log <scratch>/cleaned.clean-log.json \
  [--gold <dir of reference copies>] [--reviewed <dataset-dir>/clean-reviewed.json] --json <scratch>/check.json
```

By default only projects whose text changed are checked (it prints which unchanged ones it skipped). Pass `--projects` with every project ID to check the whole dataset, including projects kept as they were. A project passes when:
- its structure is unchanged;
- no blocking noise remains (credits, discussion, spill-over, run-together words, split identifiers, page numbers, cut-off or fused titles);
- every finding keeps at least 80% of the original's content (a project mean of at least 90%). Content is measured with spacing-insensitive character shingles after removing known noise from the original.

`--gold` adds word-level recall against a reference copy; recall must be at least 0.95.

Retention and detectors are heuristics, so read every flagged finding rather than trusting the verdict. Common situations:

| Flag | Usual cause | What to do |
|---|---|---|
| low retention, cleaned text much shorter | the section ended early (an unusual heading or rule inside the finding) or matched the wrong source | compare against the source page/issue, fix the parser or matching, rerun |
| low retention, cleaned text correct | the *original* carried spill-over the checker doesn't recognise (e.g. the non-critical items that followed the last low) | confirm on the source, then record it in `clean-reviewed.json` |
| `title differs` warning | source title differs from the PDF title (contracts renamed in the report, editorial fixes) | confirm the description is the same finding; a warning, not a failure |
| blocking detector on cleaned text | real leftover noise, or source text that genuinely contains it (an author writing "ERC 20") | fix the cleaning rule, or record the verbatim case as reviewed |
| `source reused` warning | two findings matched the same issue/section | almost always a wrong match: fix it |
| log note "used the report's M-05 by label" + low retention | the scraped finding was the wrong text (e.g. a Mitigation Review "Unmitigated" entry) | check the report: if the label's section is the audit finding, record it as reviewed and tell the user the ground truth changed |

`clean-reviewed.json` maps `finding_id` to a reason, e.g. `{"2025-01-iq-ai_L-05": "original included [N-01]..[N-03] non-critical items; source section ends at [N-01]"}`. Reviewed findings print `PASS*`. Use it only for cases you confirmed against the source, never to wave through text you haven't read.

Don't relax a threshold to make a project pass. If a check misfires on correct text, either fix the check for that pattern (and make sure it still passes on the other projects) or record a reviewed exception.

## Step 3: apply

Once everything passes, either rerun with `--in-place` or copy the cleaned file over the dataset; the script keeps the file's JSON style, so the diff is just the changed findings. Keep the provenance log next to the dataset (e.g. `<dataset-dir>/clean-log.json`) and commit it, so every finding's source is recorded. Then rebuild anything derived from finding text, such as generated benchmark tasks (Harbor: `cd adapters/scabench && uv run scabench --overwrite`) and cached ground-truth files.

## Step 4: report

Report to the user:
- a per-project table with source, findings cleaned / adopted / unresolved, retention, and verdict;
- every unresolved finding and the reason (e.g. "judging repo returns 404");
- every reviewed exception and the evidence for it;
- notable source disagreements, such as titles that differ from the scraped report (renamed contracts) and severity mismatches between the issue and the dataset (the dataset severity is kept).

## Evals

`evals/evals.json` holds the regression set: cleaning the uncleaned Code4rena projects, re-sourcing the Sherlock projects, and cleaning Code4rena projects that have a Nethermind reference copy, graded against that reference. Re-run them after changing the parser or matching rules.
