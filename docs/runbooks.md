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

`analyze_drift.py` runs in the daily sweep; a `DRIFT-CANDIDATE` verdict in
`reports/drift-latest.md` means comparable captures differ below the 0.995
similarity threshold.

1. Confirm the diff from the stored `extracted.txt` pair, sha-pinned (never
   compare by path order).
2. Classify: substantive disclosure change vs layout/date churn. Single-word
   changes can be substantive (the Muse Spark June→July date extension) — read
   the actual opcodes, do not filter by length.
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
