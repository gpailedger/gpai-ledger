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
   `absence`: **`confirmed`** only when an independent vantage saw the absence
   too — the witness replayed a fresh 404/410 (`confirmed_by: ["witness"]`), or
   the operator fetched a 404/410 from a second network with
   `crawler/attest.py` (`confirmed_by: ["operator"]`);
   **`persistent`** when the absence has been observed from the runner alone on
   `PERSISTENT_AFTER_DAYS` distinct UTC dates with the most recent prior one
   within `ABSENCE_WINDOW_DAYS` (`confirmed_by: []`; same-day re-runs count
   once; any successful fetch resets the streak; plain errors neither reset nor
   count; calendar adjacency is not required so a single missed sweep does not
   break it). A persistent absence reddens the run and feeds the relocation
   hunt exactly like a confirmed one, but it is never called confirmed: one
   datacenter vantage cannot tell a removed document from an address being
   refused — on 23 Aug 2026 two documents that were live from every other
   network were 404 from the runner on two consecutive dates while the Archive
   was unreachable from it (events before that date labelled `confirmed` with
   `confirmed_by: ["consecutive-days"]` are what is now called persistent). A
   day on which a fresh witness saw the document live, or on which the operator
   attested it live, restarts the streak and vetoes this route until that
   sighting is older than `ABSENCE_WINDOW_DAYS` (recorded as
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

**What the site says.** From the first confirmation, the model page carries
"The provider's copy of this document no longer resolves", the date, and how it
was corroborated (an Internet Archive capture, a second network, or both); the
status table marks the row "(provider copy no longer resolves)". The status
badge stays *published* — the summary was published, and every archived version
with its hash and proof remains exactly where it was. The banner disappears by
itself as soon as the document is fetched again, a witness sees it live, or the
operator attests it live.

**Red run with a persistent absence — what to do.** From a network that is
not a datacenter (your own), run `python crawler/attest.py --source <id>`: it
fetches the archived URL through the sweep's guarded fetch and appends one
event — `live-attested` (status, size, SHA-256, whether the bytes equal the
archived version) or, on a 404/410, a second-vantage `confirmed` absence.
Nothing is minted; a changed document is captured by the sweep. Commit
`data/events.jsonl` as the project identity. A runner that stays blind to a
live document needs a route change (rendered fetch, alternative URL,
relocation), not repeated attestations: the single-vantage route reddens
again once the attestation is older than `ABSENCE_WINDOW_DAYS`.

Only **confirmed** and **persistent** absences count toward relocation-hunt
streaks. What reddens the run is narrower, so that red keeps meaning "new or
unresolved":

- the **first** confirmation of an absence in a streak — news, and worth a look;
- every **persistent** one — a single vantage, still unresolved, waiting for the
  operator to attest it from another network;
- a confirmed absence that has already been confirmed on an earlier date of the
  same streak does **not** redden the run again (`known_absence` in the sweep
  summary). By then the model page states that the provider's copy no longer
  resolves, with the date and the corroborating vantages, so repeating it daily
  would only teach the operator to ignore red. Any successful fetch, a live
  witness or an operator attestation clears the streak, and the next
  disappearance is news all over again.

Unconfirmed and contradicted absences are fully logged
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
- `retry_wayback.py` works the queue of captures with no witness of their own:
  a manifest with no `wayback` block at all (an early capture from before the
  save step existed — 43 of these were invisible to the tool until 28 Aug
  2026), a recorded failure, or a snapshot that is **older than the capture it
  is attached to**. That last case matters: Save Page Now does not always crawl
  anew, and a capture it hands back from months earlier witnesses the page's
  earlier state, not the document we stored (the corpus held one 222 days
  older). `wayback_save` therefore records `fresh` (the snapshot is no more
  than `WAYBACK_FRESH_SLACK_S` — one hour — older than the request) and
  `same_url` (the snapshot archives the address we asked for, not a redirect
  target such as a CDN or signed URL). Only a fresh snapshot counts as a
  witness; the rest stay in the queue. At most `MAX_PER_RUN` (25) are retried
  per sweep, captures with nothing archived first, and the pass also stops at a
  wall-clock budget (`GPAI_WAYBACK_BUDGET`, default 600 s): a refused save
  blocks until its own timeout, and 25 of those cost half an hour of sweep on
  28 Aug 2026. It stops sooner still after
  `TRANSPORT_FAILURES_BEFORE_STOP` (3) dropped connections or timeouts in a
  row — that is the Internet Archive refusing this runner rather than answering
  about the URLs, and hammering it is both futile and impolite. Such transport
  failures never count toward the per-target caps (`MAX_ATTEMPTS` answered
  attempts, `MAX_ATTEMPT_DAYS` distinct dates, signed URLs never), which then
  mark `gave_up`.

  **Known limitation, not a bug**: the Internet Archive frequently refuses or
  throttles GitHub-hosted runners. On 28 Aug 2026 every one of 25 save attempts
  timed out from CI while the same URLs saved fine from a residential network,
  and the same pattern appeared on 23 and 26 Aug. The backlog therefore drains
  only when the Archive is willing. OpenTimestamps is the primary provenance
  and is complete; Wayback is a best-effort second witness. If the backlog
  matters, the options are to run `python crawler/retry_wayback.py` from a
  non-datacenter network, or to obtain an archive.org account and use
  authenticated Save Page Now (higher limits, needs a secret in the repo). A retry never costs us evidence: if the new save fails, the
  older snapshot stays as the record and only `last_refresh_attempt` is added.
  `retry_wayback.py --verify` re-checks recorded snapshots and demotes dead
  ones so they re-enter the queue. Run `--verify` manually about once a
  month — Save Page Now acceptance does not guarantee durable indexing.
  The version page names what it has: a snapshot older than the capture is
  captioned as pre-existing, and one that followed a redirect says so.

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
   22 Aug 2026 pypdf 5→6 trial: 35/85 differed slightly, none under the old 0.995
   drift threshold — since 23 Aug the ledger's identity rule is the comparison to
   run; the AES-encrypted Adobe PDFs need `cryptography`).
3. Write the resolved set into `constraints.txt` (`pip freeze` minus the direct
   dependencies), commit both files, and watch the next sweep.
4. Do not bump Playwright, beautifulsoup4 or pyyaml casually: a rendering or
   HTML-extraction change mints noise versions across every rendered target.
5. No Dependabot: its merged PRs would add `dependabot[bot]` to the public
   contributor list, which must stay exactly `gpailedger` + `github-actions[bot]`.

## Restore unpushed evidence (the parachute artifact)

When the sweep's commit step cannot push (a rebase conflict on `state.json`
because something else was pushed during the run, a rejected push, a cancelled
or timed-out job), the workflow uploads the day's unpushed commit as a git
bundle artifact named `unpushed-evidence` (retention 30 days) and the run goes
red. The captures, proofs and events of that day exist only there until
restored:

```bash
gh run download <run-id> -n unpushed-evidence          # writes unpushed-evidence.bundle
git bundle verify unpushed-evidence.bundle
git fetch unpushed-evidence.bundle HEAD:refs/heads/parachute
git rebase main parachute      # data/events.jsonl union-merges; resolve state.json by hand
git checkout main && git merge --ff-only parachute && git push origin main
```

The bundle's commit already carries the sweep identity; do not re-author it.
Run `python crawler/verify_corpus.py` before pushing.

## Sweep budget and parse deadline

`run_capture.py` reads `GPAI_SWEEP_BUDGET` (seconds of wall clock for one sweep;
default 6000, clamped to 60–14400; checked between targets). Past the budget the
remaining targets are skipped, a run-level `sweep-budget-exhausted` event lists
the skipped keys, and the run goes red — everything captured before the budget
is still committed. `capture.py` reads `GPAI_EXTRACT_TIMEOUT` (seconds a PDF may
take to parse in its worker process; default 120, at most 900): a stalled parser
stores the bytes with the text omitted and a note. Both can be set as repository
Variables through the `env:` of `ledger.yml`. `GPAI_CHROMIUM_SANDBOX=0` disables
the renderer sandbox on a host that cannot provide one (never in CI).

## Relocation hunt bounds

`site_hunt.py` scores at most 40 candidates per source (PDF links first), compares
the first 20 000 identity characters of each text, stops scoring after 600 s per
source and says in `reports/hunt-latest.md` what it dropped. A relocation is
written to `relocations.json` only when the best candidate is byte-identical to an
archived version of the target, or is the only candidate at or above 0.98
similarity AND beats every sibling model's summary by 0.002 (summaries of one
provider follow one template and sit at 0.993–0.998 to each other). Anything else
is reported for human review. The hunt never crawls `aial.ie` or `archive.org`,
never follows a redirect off the provider's site, and reports
`recovered-at-recorded-location` when the dead URL answers 200 again.
