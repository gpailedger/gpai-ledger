# AIAL methodology — technical reference for this project

Compiled 19 Aug 2026 from AIAL's FAccT 2026 paper (Blankvoort, Pandit & Gahntz,
DOI 10.1145/3805689.3806755) and aial.ie/research/gpai-training-transparency/methodology.
This is the authority for how we describe, quote, or build on AIAL's grades. When their
methodology changes upstream, update this file first, then the copy that depends on it.

## Their framework, precisely

- **Two goals:** Transparency (extent of information) and Usefulness (utility /
  actionability for stakeholders). Each summary gets both scores; neither is "the" grade.
- **Six dimensions:** Clarity, Completeness, Consistency, Correctness → Transparency;
  Accessibility, Comprehension → Usefulness.
- **242 weighted metrics** across 8 assessment sections mapped to the template
  (Document; General Information; Public / Private / Scraped / User / Synthetic & Other
  Data; Data Processing). Element-ID scheme ties metrics to template fields
  (e.g. `1.1.a` → `F1.1.a.1` provider name, `F1.1.a.2` contact).
- **Scoring:** each metric 0 / 0.5 / 1 × an importance weight (1–25). Section and
  overall scores are sums over *applicable* metrics only, normalized to percentages.
- **Letter bands:** A+ ≥95, A ≥90, B+ ≥80, B ≥75, C+ ≥60, C ≥50, D+ ≥40, D ≥25, F <25.

## Semantics that are easy to get wrong (and that we must get right)

1. **Percentages have per-summary denominators.** N/A sections (conditional fields
   gated by a "No") are excluded from the maximum, so two summaries' percentages are
   normalized over different metric sets. Comparing percentages is their intended
   comparison method (European Data Portal style), but never present a percentage as
   a fraction of one fixed universal checklist.
2. **Optional fields count as mandatory** in their scoring — extra disclosure is
   rewarded. A provider skipping optional fields loses points relative to one who fills
   them.
3. **Correctness is internal-consistency only.** They assume stated facts are accurate
   "unless there are discrepancies … which invalidate this assumption." Their grades
   do NOT externally verify training-data claims. No downstream use of their grades should imply an AIAL
   grade certifies factual accuracy of the disclosed data.
4. **Grades ≠ legal findings.** Their own framing: low quality means a summary is
   "*likely*" to fall short of Art. 53(1)(d) — probabilistic, not a determination.
   Quote grades with axis + percentage + evaluation date + attribution.
5. **"Missing" = their four-step scoping assessment** (generality; FLOP threshold
   likely met; commercial entity/activity; placed on the EU market), with their own
   disclaimer that scoping clarity is a work in progress. It is not a legal finding of
   obligation. Our site/README language mirrors this. For models WE add to the missing
   watch (e.g. MAI-Voice-2, MAI-Transcribe-1.5), no scoping test has been run at all —
   say "no summary located", never "required".
6. **The Phi-4 caveat.** Microsoft never explicitly labeled `data_summary_card.md` an
   Art. 53(1)(d) summary. AIAL assessed it because its structure resembles the template
   and stakeholders would reasonably expect it to serve as one. Any claim shaped like
   "Microsoft filed a nonconformant summary for Phi-4" overstates; the accurate form is
   "the document AIAL located and graded (D/F) was never explicitly labeled a summary".

## Facts about their operations we rely on (verified independently by this project)

- Archives are write-once, dated PDFs (`Model_YYYY_MM_DD.pdf`), one snapshot per model,
  git-provenanced; no in-place modification observed in their history.
- **No published re-evaluation or re-archiving policy** for provider updates (methodology
  page is silent; observed behavior: score tweaks without re-snapshotting). This is the
  precise gap the Ledger's version chains fill.
- Discovery method: search engines + GenAI search + manual triad (model repo/README,
  provider legal/compliance pages, technical reports). Our Tier-2/Tier-3 hunts encode
  the same triad; `site_hunt.py` seeds include the legal/compliance paths.
- Funding (per the paper): AIAL's study was funded by Mozilla.org; AIAL is funded under
  the AI Collaborative (Omidyar Group), Bestseller Foundation, AI Security Institute, and
  MacArthur Foundation; ADAPT under Research Ireland grant 13/RC/2106_P2.
