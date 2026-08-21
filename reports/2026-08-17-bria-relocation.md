# Bria: summary relocated Drive → web page; as-served content differs from January snapshot

**Status:** relocation confirmed 17 Aug 2026 (location found by the operator, manual check);
content finding **confirmed same day** by headless-browser verification (see below).

## Timeline

| Date | Event |
|---|---|
| 6 Jan 2026 | Document collection ("EU AI Act GPAI Code of Practice — Incorporating Bria Models", incl. the Art. 53(1)(d) summary for Bria 3.2) last updated per its own header; hosted as PDF on Google Drive |
| 12 Jan 2026 | AIAL archives the Drive PDF (`Bria_32_2026_01_12.pdf`) — write-once snapshot |
| 11–17 Aug 2026 | Drive URL returns HTTP 404 on all six sweeps in this span (no sweep ran 16 Aug; events logged). Wayback holds a 9 Jan 2026 snapshot of the Drive *viewer page* only; no capture of the PDF bytes exists |
| 17 Aug 2026 | The operator locates the successor: **https://bria.ai/eu-policy** — same document collection served as in-page web content. Captured + stamped (sha `f7cb2e6b3844…`) |

## Content comparison (page as-served vs AIAL January snapshot)

- Anchored character-stream similarity **0.88** — same document collection, but ~12% of
  content differs beyond rendering noise (PDF ligature artifacts and table-layout
  differences account for most of the visible diff, not the 12%).
- **Date anomaly:** the page carries "Updated Sept 1. 2025"; the January PDF's header
  said "Updated: January 6, 2026". The page may be built from an older revision, or the
  Sept date may belong to one sub-document only.
- **Passages present in the January PDF but absent from the entire served HTML**
  (checked as raw substrings, not just extracted text):
  - "no synthetic data" (January: "No synthetic data is used for training our models")
  - "data procurement and usage" (entire section heading)
  - "recurring revenue" and "one-time license" (January: compensation framework
    "providing a recurring revenue stream to data partners; Bria rejects one-time
    license deals for AI training data")
  - Note: other parts of the same compensation framework ARE present
    ("compensation", "measured contribution"), so this is passage-level, not
    section-level, absence.

## CONFIRMED (17 Aug, headless-browser verification): passages are gone, not lazy-loaded

Verified with headless Chromium (Playwright): full JS render (DOM grew 3,497 → 4,698
words), then every section button clicked explicitly ("Public Notice" ×2, "Public
Summary of Training Content", "Copyright Policy") — word count unchanged after
clicks, i.e. all content was already in the rendered DOM. Result, versus the January
PDF that Bria deleted:

- **Gone from the compliance content:** the "Data Procurement and Usage" section
  (including "No synthetic data is used for training our models"), the compensation
  framework's "recurring revenue stream to data partners" and "Bria rejects one-time
  license deals" claims, the "Letter to Customers", and the "January 6 2026" date —
  the page now carries **"Updated Sept 1. 2025"**, an *older* marker than the deleted
  document's.
- **Sibling-page sweep** (/copyrightability-report, /legal-lobby,
  /licensed-training-catalog, /privacy-policy, /terms-of-use, all JS-rendered): the
  compensation and data-procurement passages appear nowhere on Bria's legal surface.
  /licensed-training-catalog contains "No scraping. No synthetic data. No gray
  areas." — a marketing echo, not the compliance document's claim.

Bounded statement of the finding: *Bria's currently published EU-policy content no
longer contains specific representations its January 6, 2026 compliance document
made — notably the no-synthetic-data claim within the compliance document and the
data-partner compensation commitments — and the January document is no longer
available from the provider (Drive URL dead since at least 11 Aug; the byte-exact PDF survives only in AIAL's archive and our stamped capture of it). Whether this is a
rollback to a pre-January revision or an edit, the January representations are no
longer published.* Evidence chain: AIAL write-once snapshot (12 Jan) + our stamped
capture of it (11 Aug) + six sweeps of logged 404s (11–17 Aug) + today's rendered, stamped capture.

Whichever way it resolves, the January 6 PDF as a byte-exact artifact is now gone from
the provider side: AIAL's snapshot is the only known public copy of the byte-exact PDF (Wayback holds
only a Jan 2026 viewer-page snapshot), and our capture+stamp of it (11 Aug) plus
today's page capture bracket the transition.

## Sibling resolutions (same day, no content questions)

- **Apertus 1.5:** public copy found in the provider's `swiss-ai/apertus-legal` GitHub
  repo — **byte-identical** to AIAL's archive (`09275f31dffa…`). The HF-gated copy and
  public GitHub copy are the same document. Also captured: their Code of Practice PDF
  (new `cop-doc` document class). Gated-HF fact remains on record (401s, 11–17 Aug).
- **Cohere Command A+:** stable home is https://docs.cohere.com/docs/command-a-plus,
  which embeds a rotating pre-signed S3 link to the PDF. Live PDF fetched via the
  current signed URL: **byte-identical** to AIAL's archive (`22457734c44f…`). Ongoing
  watch = the docs page; the PDF is re-fetched by extracting the current signed URL.

*Method note (20 Aug 2026):* the similarity figure above lacked a stated method. Reproducible metric: SequenceMatcher over lowercased alphanumeric character streams of the two stored extracted texts gives 0.87.
