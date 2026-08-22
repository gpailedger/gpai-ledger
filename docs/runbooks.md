# Operational runbooks

Procedures for the corpus operations that are not part of the automated sweep.
Everything here preserves the ledger's core invariant: **nothing is ever silently
removed or altered** — every operation leaves an event in `data/events.jsonl` and
the prune rule (content-identity with a retained neighbor) means no content
is ever lost.

## Pruning a noise capture

A capture qualifies as noise only if its canonical text is identical to a
neighboring version of the same target (byte churn without content change:
banners, re-serialization). Never prune a content-bearing version.

```
python crawler/prune_capture.py <source_id> <target_slug> <capture_ts> --reason "..."
```

The tool refuses anything that fails the text-identity test, repairs
`state.json`, and appends a `pruned-noise` event carrying the pruned file's
sha256, a precise timestamp, and the reason. `verify_corpus.py` (C4) fails the
build if a prune appears without those fields. Do not delete capture
directories by hand.

## Provider objection / dispute

When a provider objects to full-text archiving (contact address is on the About
page), or any party disputes a capture:

1. Acknowledge within a few days; record the correspondence date.
2. For an archiving objection: add the source id with a short public reason to
   `RESTRICTED_SOURCES` in `crawler/build_registry.py`, then rebuild the registry
   (the flag lives in code so registry rebuilds preserve it; it lands in
   `sources.json` as a `restricted` field). From the next site build, version pages for
   that source publish structured facts only — hashes, sizes, text length, inner
   files, OTS proof, Wayback link — and the document bytes and extracted text are
   no longer served. Captures continue (the hash chain must not break).
3. For a factual dispute (e.g. "this was never published"): the manifest, OTS
   proof, and Wayback snapshot are the record; reply with the verification steps
   from the Methodology page. If our record is actually wrong, publish a dated
   correction note on the affected report/page — never edit history.
4. Log the outcome as an event (`outcome: "dispute"`, with a note) so the
   append-only log carries the dispute's existence.

## Drift events (a provider changed a summary)

`analyze_drift.py` runs in the daily sweep and maintains two things:

- **`reports/version-diffs.json`** — a durable ledger with one record per
  consecutive version pair of every target (`source::target::from_dir>to_dir`):
  `identical-text` (bytes changed, the extracted text did not — re-serialisation),
  `changed` (with `word_delta`, `similarity` and the word-level opcodes in
  `changes`), `method-changed` (the text differs but the two captures were made
  with different capture methods — rendering, frames or consent handling — so
  the difference is not evidence of a content change) or `no-text`. Stored
  extracts are compared first; when they differ,
  BOTH captures are re-extracted from their stored bytes with the current
  extractor before the verdict is given, so a change of extractor version can
  never read as a provider edit (`compared_via` names the tool used on each
  side — pypdf for PDF/ZIP, beautifulsoup4 for HTML, a plain decode for
  markdown/text — and `same_tool` says whether it was one tool on both sides).
  When a side cannot be re-extracted (bytes missing, extractor failure or
  deadline) the stored extracts decide and the verdict is `changed-unverified`:
  a candidate that the site reports as "not verified as a content change" and
  never publishes as one. Identity is case-sensitive on Unicode word
  characters, keeps tick/cross glyphs (the template's boxes) and digit
  separators (1.5 and 1,5 are not 15), and ignores spacing, hyphenation and
  other punctuation; a block of whole words that only moved — four or more
  words anywhere, or a shorter block of two or more that is the entire deleted
  or inserted run (a running header on another page) — is counted under
  `moved_words`, not `word_delta`; a transposition inside a word is an edit,
  and a phrase two rewritten paragraphs happen to share counts as changed. Each record names
  the rule version it was computed under; if the rule changes, the record is
  recomputed and the earlier verdict kept under `prior_verdicts`. The site
  takes every "content changed" row note and every `/changes/` (Atom) entry
  from this ledger, falling back to the stored text hashes (or, for documents
  without text, the byte hashes) only for a pair that has no record yet.
- **`reports/drift-latest.md` / `.json`** — the live-vs-archive view per
  published source, overwritten each run: `identical-bytes`; `same-content`
  (extracted text identical under the same treatment); `near-identical`
  (similarity ≥ 0.995 but the text differs — `word_delta` and the opcodes are
  listed: a one-word edit in a long document lands here, never under
  `same-content`); `DRIFT-CANDIDATE` (similarity < 0.995); and the structural
  verdicts `capture-method-change`, `bundle-covered`, `inpage-baseline`,
  `format-mismatch`, `incomplete`. Every row that has a document capture also
  carries `self_history`: the newest version's ledger verdict against its own
  previous version.

Procedure:

1. `near-identical`, `DRIFT-CANDIDATE`, `self_history: changed` and
   `changed-unverified` are candidates, never conclusions. Read the opcodes (`changes`) and confirm from
   the two `extracted.txt` files, sha-pinned (never compare by path order).
2. Classify: substantive disclosure change vs layout/date churn. Single-word
   changes can be substantive (the Muse Spark June→July date extension) — the
   ledger surfaces them; do not filter by length.
3. If substantive: write a dated report under `reports/`, quoting old/new with
   both capture ids and hashes.
4. Never overwrite a verdict: corrections to an earlier report are appended as
   dated correction notes.

## Scope repack (excluded members in a bundle)

Provider bundles may contain documents outside Art. 53 scope (including
confidential-marked material). `derived_targets.py` filters these at capture
time via `capture.filter_zip_art53`: excluded members are recorded by name and
SHA-256 in the manifest (`members_not_stored`) but their bytes are never stored
or served. If scope rules change, a repack of an existing capture must append a
`scope-repack` event carrying `prior_sha256` (verify_corpus C4 uses it to
account for the replaced capture directory).

## Absence claims (404/410 on a previously captured target)

A 404 from a single vantage point is not evidence of absence — GitHub-hosted
runners (Azure, US) have been observed receiving intermittent 404s for a document
that was live from every other network (MAI Image 2, 22 Aug 2026). For a target
with a prior stored capture, `run_capture.py` therefore:

1. **Re-checks once** after `GPAI_RECHECK_DELAY` seconds (default 45, clamped to
   0–300), using the same capture method (plain fetch or browser render). If the
   re-check fails for a reason that is NOT a 404/410 (timeout, 5xx, 403…), the
   event is a **plain error** — it reddens the run like any network failure and
   carries both observations, but it is never treated as an absence claim.
2. **Asks an independent witness** — `capture.wayback_witness`: a fresh Save Page
   Now capture read back through its replay. Only a replay carrying a
   `Memento-Datetime` **not older than the request** counts (Wayback redirects to
   the *nearest* capture and SPN can return a deduplicated one): fresh 200 ⇒
   `live`, fresh 404/410 ⇒ `absent`, anything else ⇒ `inconclusive` (a stale
   snapshot, an un-replayable capture, an SPN refusal or rate limit — the reason
   is recorded). The witness is the Internet Archive's crawler: a *second
   datacenter vantage*, not a residential one. The witness record carries SPN's
   own answer (`spn_status`) separately from the replay it may have redirected
   to. At most `MAX_WITNESSES_PER_RUN` witnesses are consulted per run, and an
   SPN refusal with 429 stops further witnesses for the run; whenever no witness
   is consulted, `witness_skipped` records why (`no-wayback`, `budget`,
   `rate-limited`).
3. **Classifies and records** the event with `vantage`, both `observations`
   (status, diagnostic headers, error — never backfilled across observations),
   `witness` (or `witness_skipped`), `absent_on` (the distinct UTC dates of the
   current unbroken absence streak, including today; the event's `ts` and these
   dates come from one clock reading), and
   `absence`: **`confirmed`** when the witness replayed a fresh 404/410
   (`confirmed_by: ["witness"]`) OR when the absence has been observed on
   `CONFIRM_AFTER_DAYS` distinct UTC dates with the most recent prior one within
   `ABSENCE_WINDOW_DAYS` (`confirmed_by: ["consecutive-days"]` — same vantage
   point, weaker than the witness; same-day re-runs count once; any successful
   fetch resets the streak; plain errors neither reset nor count; calendar
   adjacency is not required so a single missed sweep does not break it; a day
   on which a fresh witness saw the document live restarts the streak and vetoes
   this route until that sighting is older than `ABSENCE_WINDOW_DAYS` — the
   witness outranks the runner's own vantage, and the veto is recorded as
   `last_live_witness`);
   **`contradicted`** when a fresh witness saw the document live — the runner
   cannot see what other networks can (these events carry `contradicted_on` and
   `consecutive_contradicted_days` instead of `absent_on`); **`unconfirmed`**
   otherwise.
   Several registry sources can share one URL: if a later source fetches that
   URL live in the same run, every claim already recorded for it in that run is
   superseded by a `recheck-recovered` event (`recovered_by` names the source
   whose fetch succeeded, `prior_absence` what it supersedes) and the run's
   health gate is corrected — the run itself holds the proof the 404 was
   transient.

Only **confirmed** absences count toward the health gate and toward
relocation-hunt streaks. Unconfirmed and contradicted absences are fully logged
and do not redden the run — except that contradictions on
`CONTRADICTED_ALERT_DAYS` distinct dates of the current streak (the most recent
prior one within `ABSENCE_WINDOW_DAYS`; an inconclusive day in between does not
reset the count) redden it with a distinct "vantage problem" failure, without
claiming absence. Neither confirmation route is a residential observation: a
published dark-window report must cite at least two independent observations,
at least one of them non-datacenter (the operator's own check).

Observed live on 22 Aug 2026: a replay of an archived 404 returns HTTP 404 with
`Memento-Datetime`; a never-archived URL replays as 404 *without* it; SPN
answered 523 with no capture for a dead origin (hence the day route).

## OTS / Wayback self-healing

Both run inside the daily sweep and need no operator action:

- `upgrade_ots.py` first stamps any capture whose original submission failed
  (`restamped_at` recorded — a late stamp still proves existence no later than
  its date), then upgrades pending attestations to bitcoin anchors.
- `retry_wayback.py` retries failed saves (bounded, then marked `gave_up`);
  `retry_wayback.py --verify` re-checks recorded snapshots and demotes dead
  ones so they re-enter the retry queue. Run `--verify` manually about once a
  month — Save Page Now acceptance does not guarantee durable indexing.

## Registry refresh red (a tracked source id would disappear)

`build_registry.py` rebuilds `crawler/sources.json` from AIAL's eval metadata
every sweep and **fails closed** if the rebuilt registry would drop a source id
the committed registry tracks (an upstream rename or removal): the step fails,
the committed registry stays in force, the sweep and the deploy proceed
normally, and the run is red until the change is handled. The failure message
names the missing ids.

1. Find the cause in the AIAL repo (`evals/*.yaml`): renamed file, changed
   `organization` string, or removed model.
2. A rename or re-labelling: map it explicitly (`MODEL_NAME_OVERRIDES` /
   the id derivation) so the existing id — and its permalinks — survive.
3. A genuine removal: add the id to `RETIRED_SOURCE_IDS` (id → dated reason).
   The refresh then carries the source's last committed entry forward flagged
   `retired`, the sweep stops fetching it, and the site keeps its pages and
   permalinks with a "no longer tracked" note and drops it from the tracked
   count — never unpublished.
4. Never let a stale "per AIAL's assessment" attribution stand: that is the
   reason the refresh fails closed instead of carrying the source forward.

## Dependency refresh

Direct dependencies are pinned in `crawler/requirements.txt`; every transitive
package is pinned in `crawler/constraints.txt`; the test and lint tools (pytest,
ruff) are pinned in the workflow install lines. All three workflows install with
`-r crawler/requirements.txt -c crawler/constraints.txt`, so a run never
resolves anything to "latest". Refresh deliberately — quarterly, or when an
advisory names a pinned package:

1. In a fresh scratch venv (never the operator's base environment):
   `pip install -r crawler/requirements.txt` with the intended bumps, then
   `pip check`.
2. Run the full test suite there, then re-extract every archived PDF with the
   new extractor and compare canonical text against the stored extracts (the
   22 Aug 2026 pypdf 5→6 trial: 35/85 differed slightly, none under the 0.995
   drift threshold; the AES-encrypted Adobe PDFs need `cryptography`).
3. Write the resolved set into `constraints.txt` (`pip freeze` minus the direct
   dependencies), commit both files, and watch the next sweep.
4. Do not bump Playwright, beautifulsoup4 or pyyaml casually: a rendering or
   HTML-extraction change mints noise versions across every rendered target.
5. No Dependabot: its merged PRs would add `dependabot[bot]` to the public
   contributor list, which must stay exactly `gpailedger` + `github-actions[bot]`.
