# GPAI Ledger

A versioned public record of what AI companies say they trained on: EU AI Act
**Article 53(1)(d)** training-data summaries, checked on a daily schedule, stored
with full version
history and per-capture provenance (SHA-256, OpenTimestamps proofs, triggered Wayback
saves).

**Current coverage:** every Art. 53(1)(d) summary this project has located, tracked at document level; models tracked with no summary located — most per AIAL's scoping assessment (models AIAL assesses, under its four-step scoping test, as requiring a public summary that it could not discover; not a legal determination), some from this project's own monitoring with obligation status not assessed; and the Commission's template documents and discovery watch pages. Live counts are on the site index, generated from the corpus at every build.

See `reports/` for findings.

## Layout

- `crawler/` — capture engine + provenance rig
  - `build_registry.py` — builds `sources.json` from AIAL eval metadata (attributed) + verified direct URLs
  - `run_capture.py` — capture sweep: fetch → hash → dedupe → store → Wayback → OpenTimestamps
  - `meta_hub.py` — Meta transparency-hub extractor (documents behind rotating signed CDN URLs)
  - `derived_targets.py` — documents whose URLs are re-mined from a watched page each run
  - `retry_wayback.py` / `upgrade_ots.py` — provenance self-healing (failed saves; bitcoin anchoring)
  - `analyze_drift.py` — live-vs-archive comparison (word/char-level, extraction-noise-proof)
  - `harvest_eval_history.py` — every historical state of AIAL's evaluations, read from
    their public git history (a sweep sees only today's state; grades are revised in
    place), plus the earlier-version pages they publish
  - `verify_corpus.py` — full corpus integrity verifier (hashes, proofs, state/disk/event consistency; runs in CI)
  - `prune_capture.py` — the only sanctioned capture-removal tool (refuses content-bearing versions; logs provenance)
  - `capture.py` — core library (fetching incl. conditional GETs, rendering, extraction, `store_new_version` write path)
- `data/` — the corpus
  - `captures/<provider>__<model>/<kind>-<url-hash>/<utc-ts>/` — raw bytes, `extracted.txt`, `manifest.json`, `.ots` proof
  - `state.json` — version index; `events.jsonl` — append-only check log
- `site/` — static permalink site generator (`/ledger/<provider>/<model>/v/<capture>/`) + reader-lens lint
- `tests/` — offline pytest suite (fixture corpora, mocked network; runs in CI before every sweep)
- `.github/workflows/` — `ledger.yml` daily pipeline (05:47 UTC), `hunt.yml` weekly
  relocation/discovery hunt, `verify.yml` weekly read-only integrity check

## Running (Linux/macOS/Windows, Python 3.12)

```
pip install -r crawler/requirements.txt -c crawler/constraints.txt   # direct + transitive pins
python -m playwright install --with-deps chromium   # --with-deps needed on Linux

# optional: refresh the registry from AIAL metadata (sources.json is committed,
# so this step can be skipped)
git clone --depth 1 https://github.com/AIAccountabilityLab/gpai-training-transparency.git /tmp/aial-repo
python crawler/build_registry.py /tmp/aial-repo

python -m pytest tests/                  # offline test suite
python crawler/run_capture.py            # full sweep
python crawler/run_capture.py --only <source-id> --no-wayback --no-ots   # one source, locally
python crawler/attest.py --source <source-id>   # record what YOUR network sees for a 404 the runner reports
python crawler/meta_hub.py               # Meta hub editions
python crawler/derived_targets.py        # re-mined-URL documents
python crawler/analyze_drift.py
python crawler/harvest_eval_history.py   # AIAL evaluation history (idempotent; needs a GitHub token)
python site/build.py                     # regenerate site into site/dist/
```

Captures identify themselves as `GPAI-Ledger/0.1` with a contact address. The crawler
archives public regulatory disclosures; quoted material is attributed. Some captures
are held but not served on the site — they get structured-facts treatment instead:
hashes, sizes, text length and provenance metadata (including the Wayback link),
without the document bytes or extracted text. That applies to a provider who objects
to full-text archiving, and to a third party's own research (see *Rights* below).

## Publishing and bootstrap

The corpus was bootstrapped 11–21 August 2026 under manually triggered runs; the
automated daily schedule (`.github/workflows/ledger.yml`, 05:47 UTC) runs in GitHub
Actions from 22 August 2026, the day the repository and site went public. Re-captures
whose bytes changed but whose content
is identical (banner churn, re-serialization) may be pruned via
`crawler/prune_capture.py`; every prune is logged in `data/events.jsonl` — prunes
made by the tool carry the pruned file's hash and reason; the curation-time prunes
of 11–19 August 2026 carry the capture directory, whose hash is in the matching
`new` event — and the tool's rule only ever removes captures whose content is
identical to a retained neighboring version. Pruned captures are not in this
repository's history, which begins on 21 August 2026.
`crawler/verify_corpus.py` re-verifies the full corpus (hashes, proofs, state/disk
consistency) in CI on every run.

## Provenance semantics

- **Documents** (PDF/ZIP/DOCX/markdown) are stored as fetched, byte-exact; the
  manifest records the SHA-256, HTTP metadata and the triggered Wayback save outcome,
  and an OpenTimestamps proof sits beside the bytes. `upgrade_ots.py` upgrades pending
  calendar attestations to bitcoin anchors on later runs.
- **Rendered captures**: targets flagged `"render": true` (JS-only pages) store a
  serialized post-JavaScript DOM — including substantive cross-origin iframes — and
  are marked `"rendered": true` in the manifest. These are derived renderings, not
  origin bytes; change detection for them uses a canonical (whitespace-collapsed)
  text hash.
- **A recorded Wayback save means Save Page Now accepted the request**; durable
  presence in the public index is a separate fact that only a later CDX lookup
  establishes.
- **Signed URLs in manifests:** some providers serve documents only through expiring
  signed URLs (AWS pre-signed, Meta CDN tokens). Manifests record the fetch URL as
  used, signatures included — these are time-limited, publicly-served access tokens
  for public documents, kept because the exact fetch URL is part of the evidence.
  They are not repository secrets; secret scanners can allowlist `data/captures/`.

## Rights in what this repository holds

Three different things live here, under three different rights.

- **This project's code** (`crawler/`, `site/`, `tests/`) is MIT-licensed — see
  `LICENSE`. **The provenance metadata** the ledger produces — hashes, capture
  timestamps, proof references, the records in `ledger.json` — is released CC0.
- **Archived documents** under `data/captures/` are *not* covered by either. They
  remain the copyright of whoever published them, and are retained byte-exact because
  an evidence archive of what a provider disclosed is worthless if the bytes can be
  edited after the fact. Most are Article 53(1)(d) training-data summaries: documents
  the EU AI Act requires their providers to make public.
- **A third party's own research** is archived but deliberately not served. The
  AI Accountability Lab's scored evaluations (`aial-eval` captures) are their work,
  and their repository carries no licence granting redistribution. The ledger keeps
  them because a grade becomes unrecoverable once revised, and publishes only what it
  holds — size, canonical text hash, timestamp proof — with a link to
  [AIAL](https://aial.ie/research/gpai-training-transparency/) for the assessment
  itself. Their content is not served on the site and not carried in the diff ledger.
  This is the project's own choice, not a response to an objection.

If you published something archived here and want it handled differently, write to
contact@gpailedger.com. Removal of a capture goes through `crawler/prune_capture.py`
so that the removal is itself recorded rather than silently rewriting the record.

