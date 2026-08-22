# Discovery & relocation: the four tiers

How the Ledger ensures a document that moves — or newly appears — still gets captured,
without ever claiming more than the evidence supports.

## Tier 0 — model-universe discovery (added 19 Aug 2026)

The registry must not depend on any single upstream tracker noticing a model. Three
independent feeds surface new models and new providers before anyone lists them:

1. **Provider catalog watches** (daily sweep, deterministic): the model-catalog pages
   of OpenAI, Anthropic, Mistral, Cohere, xAI, and Microsoft are registry watch
   targets — a new model on a tracked provider's own catalog surfaces as a change
   event. (Meta's hub series and Google's transparency bucket already work this way.)
2. **Provider-universe feeds** (daily sweep): the GPAI Code of Practice page
   (signatories self-declare GPAI-provider status — a new signatory is a new
   candidate provider) and osai-index.eu (open-source entrants).
3. **HF org releases** (`org_watch.py`, weekly in hunt.yml): keyless HF API query for
   new models from ~20 tracked organizations → `reports/org-watch-latest.md` as hunt
   candidates. First run (19 Aug) immediately surfaced DeepSeek-V4-Pro and the
   Qwen 3.8 family — providers absent from every tracker.

Candidates from Tier 0 become tracked sources only after a hunt verifies what they
are — the registry never grows on a feed's word alone. Scope honesty: "all possible
models" is unbounded; Tier 0 covers (a) every new model from tracked providers,
(b) every self-declared GPAI provider, (c) notable new entrants. Site language never
claims exhaustiveness.

## Status vocabulary (binding for all reports)

- `unreachable-at-recorded-location (N sweeps)` — the recorded URL errors; nothing
  else is implied.
- `relocated (fingerprint-confirmed)` — a new URL serves content proven to match
  (byte-identity or char-stream similarity ≥ 0.98).
- `not-found-at-any-known-location` — the hunt ladder is exhausted for this run.

"Removed", "deleted", or "went dark" never appear in generated output. A document
findable by no tier and no search engine fails Art. 53's "clearly visible and
accessible" publication requirement on its face — that finding, with the exhausted-hunt
record as evidence, is the deliverable; there is nothing left to fetch.

## Tier 1 — daily, inside the sweep (deterministic)

Redirect following (manifests record `final_url`), the AIAL-metadata refresh (their
URL corrections flow into the registry each run), catalog-driven extractors that
re-derive document URLs from provider listing pages every run (`meta_hub.py`,
`derived_targets.py`), and per-target error events in `data/events.jsonl`.

## Tier 2 — weekly, `site_hunt.py` (deterministic)

For each target with ≥ 2 consecutive error sweeps whose source has no healthier
target: probe the dead URL for redirects, probe the domain's sitemap.xml, then a
bounded same-domain BFS crawl (≤ 120 pages, depth ≤ 3, throttled) collecting
document-like links; every candidate is fingerprint-scored against the dead target's
own last stored text. Confirmed matches land in `crawler/relocations.json`, which
`build_registry.py` merges into the registry — discovery-to-capture with no code
edit. Runs weekly via `.github/workflows/hunt.yml` (Mondays 06:30 UTC).

## Tier 3 — weekly, AI search hunt (LLM agents; judgment the tiers above can't encode)

Covers what site crawls can't: documents published under different product names
(GPT Image 2 was filed as "ChatGPT Images 2.0"), filename-convention breaks
(MAI Cyber's `-Data-Card.pdf`), and models missing from all watched surfaces
(Muse Glimmer). Agents search per provider group over the current missing list and
dead targets; **the AI proposes, the hash disposes** — a candidate becomes a registry
entry only after fingerprint confirmation against stored text, or, for
never-before-seen documents, after template-structure verification.

Operation: run as an agent workflow in the operator's session, weekly cadence,
targeting only the delta (currently ~21 missing models). A further option: a
scheduled cloud agent that files candidates for the next session to verify.
Findings feed `EXTRA_TARGETS` / `relocations.json` after verification.

## What remains manual by design

Registering a **new** provider (not yet in AIAL metadata and not on any watched
surface) and accepting Tier-3 candidates below the auto-confirm threshold. Both are
verification decisions, not discovery labor.
