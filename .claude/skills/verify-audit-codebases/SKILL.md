---
name: verify-audit-codebases
description: Pass/fail verification that an audit benchmark project's pinned source repos and commits actually match its audit findings — catching wrong repos, wrong contest editions, post-audit (already-fixed) commits, and missing in-scope repos. Use this whenever new projects are added to a ScaBench-style dataset (codebases with repo_url/commit + vulnerabilities with title/description), when curating or scraping Code4rena/Sherlock/Cantina audits, when repo links or commits in a dataset look suspicious, or before generating benchmark tasks (e.g. Harbor) or baselines from a dataset — even if the user only says "check the repos", "validate the new projects", or "make sure the links are right".
---

# Verify audit codebases

A benchmark project is only valid if the code an agent audits is the code the auditors audited. Scraped datasets get this wrong in a few recurring ways: the repo is from a different contest edition, the commit is the sponsor's *post-fix* head (so the bugs are gone), or the scope spans several repos and only one was pinned. The scoring LLM can't detect any of this, so it silently corrupts results. This skill turns the check into a deterministic pass/fail test plus a short triage playbook for failures.

## Inputs

A dataset JSON — a list of projects (or one project) shaped like:

```json
{"project_id": "...", "name": "...", "platform": "code4rena|sherlock|cantina",
 "codebases": [{"repo_url": "https://github.com/org/repo", "commit": "<sha | branch | blank>", "tarball_url": "..."}],
 "vulnerabilities": [{"finding_id": "...", "title": "...", "description": "...", "severity": "..."}]}
```

If the user gives repos and findings in another shape, convert them to this first (one project per audit, findings verbatim from the report).

## Step 1 — run the test

```bash
python3 .claude/skills/verify-audit-codebases/scripts/verify_codebases.py <dataset.json> \
  [--project <id> ...] --report <scratch>/verify_report.json --cache <scratch>/verify-cache \
  [--overrides <dataset-dir>/verify-overrides.json]
```

If a `verify-overrides.json` sits next to the dataset, pass it: it records pins a human verified by other means (see Step 3).

Use a scratch directory for `--cache` (snapshots are a few hundred MB for ~30 projects and are reused across runs). An authenticated `gh` CLI is strongly recommended — without it the Sherlock copy-record check and commit warnings are skipped. Exit code is 1 if any project fails.

What it checks per project (details in the script docstring):

- **T1 links** — the contest repo the findings link to most (`code-423n4/*`, `sherlock-audit/*`) is pinned, or a sponsor repo is pinned at exactly the commit the Sherlock contest repo records copying (`Copy <folder> @ <sha>`) *for the folder the findings link into*.
- **T2 context** — ≥80% of findings that name code have ≥50% of the named files/identifiers present in the pinned source. Tolerant of scraper damage (glued words like `inisExpired`, split filenames, "Submitted by" credits, PoC test names).
- **Warnings** (never fail) — non-SHA commits (`main`/blank), commit messages that look post-audit (`Merge ... sherlock-48`), repo redirects.

PASS = T1 and T2. Also run `--negative-control` once on a new dataset: it scores every project's findings against a different project's source and should report 0 passes. If anything passes there, T2 is too lenient for that data and you should lean on T1 and manual diffs.

## Step 2 — triage each FAIL

T2 alone can't tell "right repo, fixed commit" from "right commit" — identifiers survive fixes — so T1 failures and warnings matter even when T2 is green. Read `references/triage.md` for the full playbook; the common causes and how to confirm them:

| Symptom | Likely cause | Confirm with |
|---|---|---|
| T1: links to `code-423n4/YYYY-MM-x`, pinned a different `YYYY-MM-x` | wrong contest edition | findings' links; contest repo list on GitHub |
| T1: links to `sherlock-audit/...`, pinned sponsor repo, copy SHA ≠ pinned | sponsor commit is after the contest (often contains fixes) | `diff_trees.py` pinned vs contest folder; use the recorded copy SHA |
| T1: Sherlock contest repo not readable (404) | private contest repo | sponsor PR history: the base of the first `sherlock-*`/audit-fix PR is the latest pre-fix commit |
| warn: commit message mentions sherlock/audit | pinned the merge of an audit fix | same as above |
| T2 low, few source files, findings name other repos | multi-repo scope, only contest repo pinned | contest `README.md` "Scope" section lists repos + commits |
| warn: non-SHA commit on a contest repo | unpinned; fine only if no source changed after "Contest repo init" | list commits touching source (see triage.md) |

Compare code, not just names: a renamed file (e.g. `SolidlyV3AMO.sol` → `V3AMO.sol`) with identical contents is not a mismatch — `diff_trees.py --rename Solidly=` settles it.

## Step 3 — fix and re-verify

1. Write the corrected pins to a fixes file (`{project_id: [{"repo": "owner/name", "commit": "<full sha>"}]}`) — always full 40-char SHAs, one entry per in-scope repo.
2. Apply without reformatting the dataset:
   ```bash
   python3 .claude/skills/verify-audit-codebases/scripts/apply_codebases.py <dataset.json> fixes.json
   ```
   Prefer trying fixes on a scratch copy first, then applying to the real file once they pass.
3. Re-run Step 1 until every project passes. Check `git diff` touches only the intended `codebases` blocks.
4. If a pin is correct but T1 can't prove it (typically a private Sherlock contest repo), record it in `verify-overrides.json` next to the dataset rather than weakening the test:
   ```json
   {"<project_id>": {"commits": ["<full pinned sha>"], "reason": "<evidence; say if inferred>", "verified_by": "", "date": "YYYY-MM-DD"}}
   ```
   Overridden rows print `PASS*` with the reason, and fail again if the pin later changes. Overrides are for evidence the script can't fetch — never use one to silence a T2 failure or a diff that shows changed code.

## Step 4 — report

Report to the user as a pass/fail table (before → after), one row per changed project with: the problem, the evidence (link target, copy record, diff counts, PR names), and the new pin. Mark any pin that is inferred rather than recorded (e.g. from PR history when the contest repo is private) so a human can confirm it against the audit report. Mention downstream artifacts that are now stale (baseline results, generated benchmark tasks) for the changed projects.
