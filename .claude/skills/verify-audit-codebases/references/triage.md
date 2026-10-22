# Triage playbook

Commands assume an authenticated `gh` CLI. `$SK` = `.claude/skills/verify-audit-codebases/scripts`.
Snapshots from the verifier live in its `--cache` dir as `owner__repo@sha/`.

## Contents
1. Wrong contest edition (Code4rena)
2. Sponsor repo pinned instead of the Sherlock copy
3. Contest repo private / 404
4. Multi-repo scope
5. Unpinned contest repo (`main` / blank)
6. Things that look wrong but aren't

---

## 1. Wrong contest edition (Code4rena)

Protocols return for several contests (`2024-08-superposition`, `2024-10-superposition`). The project's findings link to the edition they came from. Pin that repo at a commit with no source changes after "Contest repo init" (see §5) — the code-423n4 repo *is* the audited snapshot.

## 2. Sponsor repo pinned instead of the Sherlock copy

Sherlock contest repos (`sherlock-audit/YYYY-MM-name`) contain the sponsor repo as a subfolder and record what they copied:

```bash
gh api "repos/sherlock-audit/<contest>/commits?per_page=100" \
  --jq '.[] | .commit.message | split("\n")[0] | select(test("^Copy "))'
# Copy oku-custom-order-types @ b84e5725f4d1e0a1ee9048baf44e68d2e53ec971
```

- Pick the copy whose folder the findings link into (links look like `.../blob/main/<folder>/...`). A contest can copy two different repos (e.g. `govstaking` and `staker`); only the linked one was audited.
- If a folder was copied more than once, prefer the copy made before the contest start; if contents are identical (check with `diff_trees.py`) either is fine.
- Confirm the pinned commit is wrong by diffing code, not dates:
  ```bash
  python3 $SK/diff_trees.py <cache>/<sponsor>@<pinned> <contest-snapshot>/<folder>
  ```
  `changed`/`missing` in core contracts ⇒ repin to the recorded copy SHA (it exists in the sponsor repo: `gh api repos/<sponsor>/commits/<sha>`).
- Pinning the `sherlock-audit` repo itself is also valid (it is what the findings link to); pinning the sponsor repo at the copy SHA keeps the original layout.

## 3. Contest repo private / 404

Use the sponsor's fix PRs, which Sherlock/C4 teams usually name after the issue:

```bash
gh api "repos/<sponsor>/pulls?state=closed&per_page=100" \
  --jq '.[] | select(.head.ref|test("sherlock|audit|c4|fix")) | "\(.number) \(.head.ref) base=\(.base.sha[0:10]) merged=\(.merged_at[0:10])"'
```

The `base` of the earliest audit-fix PR is the latest commit without any fixes — the best available pin. Check which contract files changed between it and the old pin (`gh api repos/<sponsor>/compare/<a>...<b> --jq '.files[].filename'`). Flag this pin as *inferred* in the report; the audit report PDF usually states the exact commit. Since T1 can't verify it, record it in `verify-overrides.json` (see SKILL.md Step 3) with this evidence in `reason`.

## 4. Multi-repo scope

Symptom: T2 low, the pinned snapshot has few source files, findings link to or name files from other repos (`initia-labs/movevm`, `x/.../keeper.go`). Read the contest README scope:

```bash
sed -n '/^# Scope/,/^## /p' <cache>/<contest-repo>@<sha>/README.md
```

Pin every in-scope repo at the listed commit (for "diff from A to B" scopes, pin B). Keep the contest repo too if it contains in-scope modules. Scraped `codebase_id`s sometimes carry the right SHA prefix attached to the wrong repo — a useful hint.

## 5. Unpinned contest repo (`main` / blank)

Contest repos get README/report commits for months; that's harmless. Verify no *source* changed after init, then pin the current HEAD SHA:

```bash
R=code-423n4/<contest>
gh api "repos/$R/commits?per_page=100" --jq '.[] | "\(.sha) \(.commit.committer.date[0:10]) \(.commit.message|split("\n")[0][0:60])"' |
while read sha date msg; do
  n=$(gh api repos/$R/commits/$sha --jq '[.files[]?.filename | select(test("\\.(sol|rs|go|move|cairo|vy)$"))] | length')
  [ "$n" != 0 ] && echo "$date ${sha:0:12} src_files=$n $msg"
done
```

Only "Contest repo init" (plus pre-start fixes) should touch source. If later source commits exist, pin the last commit before the contest's first finding/report activity.

## 6. Things that look wrong but aren't

- **Repo redirects** (`Uniswap/minimal-delegation` → `Uniswap/calibur`, `withtally/staker` → `ScopeLift/staker`): GitHub renames; the commit is what matters.
- **Renamed files with identical code**: use `diff_trees.py --rename Old=New`.
- **Missing library code** (`lib/forge-std`, OpenZeppelin): tarballs omit git submodules. Only a problem if a submodule *is* the in-scope code (check `.gitmodules` against the README scope).
- **Findings linking to external libraries** (OpenZeppelin, Uniswap) — ignored by T1 by design.
- **Findings with no code names** (some Cantina reports) — excluded from T2; rely on T1/warnings and spot-check.
