# Operational runbooks

Procedures for the corpus operations that are not part of the automated sweep.
Everything here preserves the ledger's core invariant: **nothing is ever silently
removed or altered** — every operation leaves an event in `data/events.jsonl` and
the prune rule (content-identity with a retained neighbour) means no content
is ever lost.

## Pruning a noise capture

A capture qualifies as noise only if its canonical text is identical to a
neighbouring version of the same target (byte churn without content change:
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
   from the About page. If our record is actually wrong, publish a dated
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

## OTS / Wayback self-healing

Both run inside the daily sweep and need no operator action:

- `upgrade_ots.py` first stamps any capture whose original submission failed
  (`restamped_at` recorded — a late stamp still proves existence no later than
  its date), then upgrades pending attestations to bitcoin anchors.
- `retry_wayback.py` retries failed saves (bounded, then marked `gave_up`);
  `retry_wayback.py --verify` re-checks recorded snapshots and demotes dead
  ones so they re-enter the retry queue. Run `--verify` manually about once a
  month — Save Page Now acceptance does not guarantee durable indexing.
