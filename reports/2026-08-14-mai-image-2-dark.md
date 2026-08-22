# Event: MAI Image 2 training-data summary went dark

**Detected:** day-4 sweep, 14 Aug 2026. First document to go dark **under the Ledger's watch.**

## Timeline

| When (UTC) | Fact |
|---|---|
| 21 Jul 2026 | AIAL archives the document (`MAI_Image_2_2026_07_21.pdf`, graded C+/D+) |
| 7 Aug 2026 | Existing Wayback snapshot of the URL (predates the Ledger) |
| 11 Aug 10:41:59 | Ledger capture: `sha256 0c29e9f3ad0f0062…`, OpenTimestamps-stamped, 200 OK |
| 12–13 Aug | Sweeps report unchanged (200 OK, same bytes) |
| 13 Aug ~17:00 → 14 Aug sweep | Document goes dark: `https://microsoft.ai/pdf/MAI-Image-2-Data-Summary.pdf` returns **HTTP 404** |

## Facts established on detection day

- **Targeted, not an outage:** every other Microsoft MAI document we track (MAI Image 2.5 `Data-Card.PDF`, MAI Code 1 Flash, MAI Cyber 1 Flash `Data-Card.pdf`) returned 200, unchanged, in the same sweep. The MAI Image 2 **Model Card** is also alive.
- **No discoverable relocation:** all plausible rename candidates 404 (`-Data-Card.pdf`, `-Data-Card.PDF`, `-Data-Summary.PDF`, `2.0` variants) — checked 14 Aug.
- **Microsoft's own Model Card still links the dead URL** (link annotation extracted from the live PDF on 14 Aug) — the provider's compliance link chain is broken.
- Context: MAI Cyber's summary is published under a `-Data-Card.pdf` filename that breaks Microsoft's own `-Data-Summary.pdf` pattern (AIAL evaluates that model; their eval metadata carries no archive filename for it). A botched rename toward that convention is a plausible innocent explanation for this outage; deliberate removal cannot be excluded. **No conclusion is drawn; the record is the record.**

## Compliance relevance

Art. 53(1)(d) + Explanatory Notice para (32): the summary "should be published on the
provider's official website in a clearly visible and accessible manner" — while the
document 404s and the model remains on the EU market, that publication state is
arguably not met. The Ledger holds the document (capture + OTS proof); Wayback holds
an independent snapshot; AIAL holds a third copy.

## Addendum (14 Aug, later): retirement hypothesis

Follow-up checks after the operator independently confirmed the 404:

- `microsoft.ai/models/mai-image-2/` now **redirects to `/models/mai-image-2-5/`** —
  an active consolidation of Image 2 into Image 2.5 is in progress. The summary
  removal fits that cleanup. Leading hypothesis: **model retirement, not suppression.**
- MAI Image 2.5's summary declares "Model dependencies: N/A", so Notice para (30)'s
  requirement to link the original model's summary does not chain 2.5 to the dead
  document.
- Still unexplained by a *clean* retirement: the Image 2 Model Card remains live and
  still links the dead summary — a half-finished cleanup.
- Open legal question (genuinely unsettled): whether
  the Art. 53(1)(d) publication duty survives a model's withdrawal from the market.
  The Act and Notice are silent; Recital 107's purpose (rightsholders enforcing
  rights about training that already occurred) argues for persistence. Whether MAI
  Image 2 is actually off the EU market (vs. page-consolidated but still served) is
  unverified from here.

Whatever the motive: the provider-hosted disclosure for a model that operated on the
EU market for months is gone, and the public record now exists only in third-party
archives. Retirement is the ordinary, innocent-looking mechanism by which disclosures
vanish — that is the thesis, not an alternative to it.

## Resolution (15 Aug 2026): restored, byte-identical — dark window ~13–15 Aug

The day-5 sweep (15 Aug, 21:31 UTC) found the summary URL serving HTTP 200 again with
SHA-256 `0c29e9f3ad0f006260da86742c482616d649ee9df3ad4537a180656fa0aaaa34` — **byte-identical
to our pre-dark capture of 11 Aug**. Restored, not republished: same document, same hash.
The model-page redirect to Image 2.5 remains in place, so the consolidation stands while
the compliance document came back — consistent with a cleanup that over-deleted and was
partially reverted.

The event log now documents the complete arc with provenance on both ends:
present (11–13 Aug, hashed + stamped; last 200: 13 Aug 16:58 UTC) → HTTP 404 (observed
14 Aug 19:57 UTC by the sweep, independently confirmed manually) → restored
byte-identical (15 Aug 21:31 UTC). This is the corpus's first fully documented
availability gap: a compliance document that was unreachable for up to ~2 days (404 observed on 14 Aug; bracketed by 200s on 13 and 15 Aug) and whose
continuity is provable only because third-party archives held it. No provider-hosted
version history would show this happened at all.

## Standing monitoring

The URL remains under daily watch. Post-restoration sweeps (15 and 17 Aug) report
200/unchanged against the pre-dark hash; any future change or disappearance mints a
new event on the same record.

*Correction (20 Aug 2026):* an earlier revision described this document as absent from AIAL's tracker and asserted a Microsoft rename; AIAL does evaluate MAI Cyber 1 Flash, and no rename is evidenced. The context paragraph above was restated as observables only.

*Addendum (22 Aug 2026):* GitHub-hosted runners observed the URL three times today — HTTP 404 at 07:24 UTC, 200 at 08:05 UTC, 404 at 08:50 UTC — while residential-IP checks returned 200 at ~07:45 UTC and 200 at ~09:05 UTC with the known bytes. The pattern is consistent with an intermittent edge-level inconsistency at the provider's CDN rather than a second removal; the daily event log continues to record each check, and any sustained absence will show there as consecutive errors.
