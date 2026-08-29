# Crawling & politeness policy

The GPAI Ledger is a low-volume, public-interest archive of regulatory disclosures.
It fetches with an identified User-Agent that carries a contact address
(`GPAI-Ledger/0.1 (…; contact: …)`), so any operator can see who we are and reach us.

## Two tiers, two policies

**1. Registry targets (the daily sweep).** Each is a single-URL GET of a specific
compliance document or its listing page, fetched at most once per day, throttled
(≥1.5 s between requests, ≥6 s between Wayback saves). These are documents providers
are legally required to publish "in a clearly visible and accessible manner"
(Art. 53(1)(d)); we retrieve them as any reader would. We do not gate these on
robots.txt, but we:
- send conditional GETs (`If-None-Match`/`If-Modified-Since` from the prior capture)
  so unchanged documents cost the origin a 304, not a re-download (one hash-verified
  full fetch per URL per week);
- honor HTTP 429 / `Retry-After` and back off;
- do not retry permanent 4xx;
- cap every response body (60 MB), reject decompression bombs, and bound the time
  spent parsing any document;
- honor any provider opt-out request (email the contact address) — a provider that
  objects is switched to structured-facts treatment: the registry entry gets a
  `restricted` flag, after which the site publishes hashes, sizes, and provenance
  metadata but not the document bytes or extracted text (see docs/runbooks.md).

**2. Relocation hunt (`site_hunt.py`, weekly).** When a document's recorded URL has
failed on consecutive sweeps, a bounded breadth-first crawl of the *provider's own
domain* looks for it at a new location. This is broader, so it is more conservative:
- hard caps: ≤120 pages and ≤300 total requests per domain per run, one request at a
  time with a ≥1 s delay after every fetch;
- confined to the provider's own site (exact host or subdomain, HTTPS only) — it never
  follows a link or redirect off-domain, and never crawls third-party document hosts
  (Drive, HuggingFace/Google/Sanity CDNs);
- fetches a candidate only to fingerprint it against the document we already hold; a
  match is registered, everything else is discarded.

**3. The evaluation record (`harvest_eval_history.py`, daily).** The AI
Accountability Lab grades these summaries and revises the grades in place, so a
once-a-day look at their files loses every intermediate state, and a renamed or
deleted evaluation disappears entirely. Their repository history is public, so the
full record is read from the **GitHub API** rather than from their website: a
routine run costs a few dozen API calls to github.com and, once a state is held, it
is never fetched again. Their published earlier-version pages are discovered by
reading pages the sweep has already captured, so discovery costs aial.ie nothing
and only a page we do not yet hold is ever requested. Their own site carries 66
conditional-GET targets in the daily sweep (63 evaluation pages, 3 framework
pages), which cost a 304 unless something changed.

Everything captured from AIAL is a third party's own research: it is archived,
hashed and timestamped, and it is **not republished** — see the *Rights* section of
the README. Their permission, not a code change, is what would alter that.

If a provider asks us to reduce or stop either activity, we comply. The contact
address in the User-Agent is monitored for exactly this.

## What we never do

We also do not redistribute another organisation's research because we happened to
crawl it. Third-party evaluations are archived so a revised score stays recoverable,
and published only as facts about the file — size, hash, timestamp proof — unless
the rights holder agrees otherwise.

No authentication bypass, no paywalled or login-gated content (a gated summary is
recorded as gated — itself a compliance-relevant fact — not circumvented), no
high-frequency polling, no distributed crawling. The entire sweep is a few hundred
requests per day from a single scheduled job.
