# Meta re-issued all three Muse training-data summaries on 21 August 2026

**Detected:** 22 Aug 2026, 07:30 UTC, by the first scheduled autonomous sweep (the
day the archive went public). **Status:** observables only; no conclusion drawn
about Meta's reasons.

## What changed

All three editions in Meta's "EU AI Act Transparency Reports" series were replaced
at new content-addressed CDN paths on the same day, each with a minor version bump and
a new "Last Update" date:

| Edition | Before (archived) | After (captured 22 Aug) |
|---|---|---|
| Muse Glimmer | V1, Last Update August 10, 2026 — sha `5f29be94fba3…` | **V1.1, August 21, 2026** — sha `0247cea31889…` |
| Muse Image | V1, Last Update 7/6/2026 — sha `e0637269761b…` | **V1.1, 8/21/2026** — sha `3cd5ac6b3e93…` |
| Muse Spark | V3, Last Update August 4, 2026 — sha `fb8e4a1daaba…` | **V3.1, August 21, 2026** — sha `08e43427de4a…` |

Word-level diffs (sha-pinned, full opcode enumeration — no length filter) show the same
edits in every document:

- **An authorized-representative (Article 54 AI Act) postal address was added** to section 1.1:
  "Merrion Road Dublin 4 D04 X2K5 Ireland".
- **The support-contact URL (`https://ai.meta.com/help`) was replaced with "N/A".**
- The market-placement date was reformatted (e.g. "7/7/26" → "July 7 2026").
- Minor whitespace/layout changes in form fields.

No data-source, data-processing, or scope fields changed in any of the three
documents (text similarity 0.92–0.98, all deltas enumerated above or whitespace).

## Evidence

Every version, with hashes, OpenTimestamps proofs and extracted text:
https://www.gpailedger.com/ledger/meta/muse-glimmer/ ·
https://www.gpailedger.com/ledger/meta/muse-image/ ·
https://www.gpailedger.com/ledger/meta/muse-spark/

## Note on capture identity

Meta content-addresses each upload, so every re-issue appears at a new CDN path.
From this event on, the extractor keys each edition on its hub label rather than its
path, so re-issues extend one version chain instead of opening a new one; the three
chains were merged accordingly (logged as `target-rekeyed` events).
