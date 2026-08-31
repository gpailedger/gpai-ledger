"""Tier-1 discovery: probe the models the ledger marks MISSING against the URL
patterns their own provider already uses for models that ARE published.

Providers are consistent with themselves. If OpenAI serves GPT-5.6 Luna at
cdn.openai.com/pdf/gpt-5-6-luna-eu-ai-act-public-summary-of-training-content.pdf,
the same shape is worth trying for GPT-5.6 Sol. This script learns each provider's
patterns from the targets already in sources.json, substitutes a missing model's
slug, and fetches the result through the project's own guarded fetch — so a hit is
a document in hand, never an inference.

A hit is promoted to crawler/discovered.json (merged by build_registry.py exactly
like site_hunt.py's relocations.json, which flips the source to "published" and
puts it in the next sweep) ONLY if it clears every one of these:

  1. HTTP 200 from the provider's own host, through _assert_public_http.
  2. A document format we can read, and text we could actually extract.
  3. The text carries Article 53(1)(d) template markers — it is a training-content
     summary, not a system card or a marketing page.
  4. The text NAMES THE MODEL. A provider's summaries for sibling models are often
     95-99% identical, so "it looks like a summary" is not enough to attribute it.
  5. Its canonical text is not already held for a DIFFERENT model. Serving one
     model's summary at another model's URL is a real provider behaviour, and
     without this check it would silently mint a false publication claim.

Anything that fetches but fails 3-5 is reported as a candidate for a human and is
NOT promoted. Nothing here ever removes or rewrites an existing target.

Run weekly (hunt.yml): python crawler/probe_missing.py [--max-per-model N] [--budget S]
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from urllib.parse import urlsplit, urlunsplit

import capture as cap

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SOURCES = Path(__file__).parent / "sources.json"
DISCOVERED = Path(__file__).parent / "discovered.json"
REPORT = ROOT / "reports" / "probe-missing-latest.md"

MAX_PER_MODEL = 6          # candidate URLs tried per missing model
BUDGET_S = 900             # wall clock for the whole pass
THROTTLE_S = 2.0           # polite pause between fetches to one origin
DOC_EXTS = (".pdf", ".zip", ".md", ".txt", ".docx")

# Phrases from the Commission's template and its common renderings. A document
# must carry at least two: one alone appears in plenty of policy pages.
TEMPLATE_MARKERS = (
    "public summary of training content",
    "summary of the content used for training",
    "article 53",
    "general-purpose ai",
    "modalities",
    "size of dataset per modality",
    "data collection",
    "training data",
)
MIN_MARKERS = 2
# Two weak markers are met by an ordinary model card ("general-purpose ai" +
# "training data"), so a promotion also needs a phrase that belongs to the
# Commission's template and to little else.
DISTINCTIVE_MARKERS = (
    "public summary of training content",
    "summary of the content used for training",
    "size of dataset per modality",
    "article 53",
)


def budget_from_env() -> float:
    raw = os.environ.get("GPAI_PROBE_BUDGET", "")
    try:
        v = float(raw) if raw.strip() else float(BUDGET_S)
    except ValueError:
        print(f"WARNING: GPAI_PROBE_BUDGET={raw!r} is not a number; using {BUDGET_S}",
              flush=True)
        v = float(BUDGET_S)
    return max(60.0, min(v, 3600.0))


def norm_tokens(s: str) -> list:
    """The identity-bearing tokens of a model name: 'GPT-5.6 Sol' -> [gpt,5,6,sol]."""
    return [t for t in re.split(r"[^0-9a-z]+", (s or "").lower()) if t]


def slug_variants(model: str) -> list:
    """The spellings a provider might use in a URL for this model."""
    t = norm_tokens(model)
    if not t:
        return []
    joined = "".join(t)
    return list(dict.fromkeys([
        "-".join(t), "_".join(t), joined, ".".join(t),
        "-".join(t).replace("-", "", 1) if len(t) > 1 else joined,
    ]))


def learn_patterns(sources) -> dict:
    """provider -> [(url, slug_variant_found_in_it)] for published document targets.
    These are the shapes that demonstrably work for that provider today."""
    out = {}
    for s in sources:
        if s.get("status") != "published" or s.get("retired"):
            continue
        for t in s.get("targets", []):
            url = t.get("url") or ""
            if t.get("kind") not in ("provider-live", "provider-page"):
                continue
            if "aial.ie" in url or "web.archive.org" in url:
                continue          # a third party's copy is not the provider's pattern
            for v in slug_variants(s.get("model", "")):
                if len(v) >= 4 and v in url.lower():
                    out.setdefault(s["provider"], []).append((url, v))
                    break
    return out


def candidates_for(source, patterns) -> list:
    """Candidate URLs for a missing model, by substituting its slug into the
    patterns its own provider already uses. The substitution is confined to the
    path and query: a slug that also appears in the hostname must never be
    rewritten, or the probe would fetch whatever third party happens to have
    registered the resulting name. Deduplicated, capped by the caller."""
    urls = []
    for url, known_slug in patterns.get(source.get("provider"), []):
        split = urlsplit(url)
        tail = urlunsplit(("", "", split.path, split.query, split.fragment))
        prefix = url[:len(url) - len(tail)] if tail and url.endswith(tail) else url
        low = tail.lower()
        for v in slug_variants(source.get("model", "")):
            if not v or v == known_slug:
                continue
            i = low.find(known_slug)
            if i < 0:
                continue
            cand = prefix + tail[:i] + v + tail[i + len(known_slug):]
            if cand == url or cand in urls:
                continue
            if urlsplit(cand).netloc.lower() != split.netloc.lower():
                continue      # belt and braces: never leave the pattern's host
            urls.append(cand)
    return urls


def looks_like_summary(text: str) -> int:
    """How many template phrases the document carries. Extraction leaves line
    breaks inside phrases ("public summary of\ntraining content"), so the text is
    whitespace-normalised first — without it most real provider PDFs score zero."""
    low = " ".join((text or "").split()).lower()
    return sum(1 for m in TEMPLATE_MARKERS if m in low)


def has_distinctive_marker(text: str) -> bool:
    low = " ".join((text or "").split()).lower()
    return any(m in low for m in DISTINCTIVE_MARKERS)


# A summary names its model near the top. Requiring the name in the identifying
# region stops an incidental mention deep in a comparison table from attributing
# a whole document to a model it merely cites.
NAME_REGION_CHARS = 3000


def names_the_model(text: str, model: str, siblings=()) -> bool:
    """Whether the document names THIS model, as opposed to a sibling whose
    summary is nearly identical.

    The model's tokens must appear adjacently. When the name ends in a number,
    the match must not be followed by a further number: a document headed
    "MAI-Image-2.5" must never be accepted for "MAI-Image-2", which is exactly
    how a 99%-similar sibling summary gets misattributed. That rule can also
    reject a genuine match followed by an unrelated figure — a false negative
    here costs a line in the report for a human to read, while a false positive
    would put a false publication claim on a public ledger."""
    toks = norm_tokens(model)
    if not toks:
        return False
    head = " " + re.sub(r"[^0-9a-z]+", " ", (text or "")[:NAME_REGION_CHARS].lower()) + " "
    if not all(f" {t} " in head for t in toks):
        return False
    loose = r"[^0-9a-z]{0,3}".join(re.escape(t) for t in toks)
    if toks[-1].isdigit():
        loose += r"(?![^0-9a-z]{0,3}\d)"
    # Longest name wins. A sibling whose name extends this one ("FLUX.2 Klein"
    # for "FLUX.2", "Apertus 1.5" for "Apertus") shares every token, so a match
    # that continues into the sibling's extra words names the SIBLING, and
    # promoting on it would file that document under this model.
    extras = []
    for sib in siblings:
        st = norm_tokens(sib)
        if len(st) > len(toks) and st[:len(toks)] == toks:
            extras.append(r"[^0-9a-z]{0,3}".join(re.escape(t) for t in st[len(toks):]))
    for m in re.finditer(loose, head):
        rest = head[m.end():]
        if any(re.match(r"[^0-9a-z]{0,3}" + e, rest) for e in extras):
            continue          # this occurrence names a longer-named sibling
        return True
    return False


def text_shas_by_model(sources) -> dict:
    """canonical text sha256 -> source id, over every capture already stored.
    Used to refuse a document that is already held for a different model."""
    out = {}
    state_p = DATA / "state.json"
    if not state_p.exists():
        return out
    state = json.loads(state_p.read_text(encoding="utf-8"))
    for key, entry in state.items():
        sid = key.split("::", 1)[0]
        for v in entry.get("versions", []):
            mp = DATA / v["dir"] / "manifest.json"
            if not mp.exists():
                continue
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            # a bundle capture records the served text under a different key
            for k in ("text_sha256", "extracted_text_sha256"):
                if m.get(k):
                    out.setdefault(m[k], sid)
    return out


def probe(url: str):
    """(raw, meta, text) or (None, reason, None). Never raises."""
    try:
        raw, meta = cap.fetch(url, retries=0, timeout=45)
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__ + ": " + str(exc)[:120], None
    if raw is None or meta.get("status_code") != 200:
        return None, f"HTTP {meta.get('status_code')}", None
    ext = cap.guess_ext(meta.get("content_type"), url, raw)
    text, _notes = cap.extract_text(raw, ext)
    return raw, meta, text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--max-per-model", type=int, default=MAX_PER_MODEL)
    ap.add_argument("--budget", type=float, default=budget_from_env())
    ap.add_argument("--only", default="", help="probe a single source id")
    args = ap.parse_args(argv)

    registry = json.loads(SOURCES.read_text(encoding="utf-8"))
    sources = registry["sources"]
    patterns = learn_patterns(sources)
    known_text = text_shas_by_model(sources)
    missing = [s for s in sources
               if s.get("status") == "missing" and not s.get("retired")
               and (not args.only or s["id"] == args.only)]

    print(f"patterns learned for {len(patterns)} provider(s); "
          f"{len(missing)} missing model(s) to probe", flush=True)

    promoted, candidates, tried = {}, [], 0
    started = time.monotonic()
    for s in missing:
        if time.monotonic() - started > args.budget:
            print(f"budget of {args.budget:.0f}s spent; the rest wait for next week",
                  flush=True)
            break
        seen = {t.get("url") for t in s.get("targets", [])}
        for url in candidates_for(s, patterns)[:args.max_per_model]:
            if url in seen or time.monotonic() - started > args.budget:
                continue
            tried += 1
            raw, meta, text = probe(url)
            time.sleep(THROTTLE_S)
            if raw is None:
                continue
            markers = looks_like_summary(text)
            distinctive = has_distinctive_marker(text)
            siblings = [o.get("model", "") for o in sources
                        if o.get("provider") == s.get("provider")
                        and o.get("id") != s["id"]]
            named = names_the_model(text, s.get("model", ""), siblings)
            final_host = urlsplit(str(meta.get("final_url") or url)).netloc.lower()
            same_host = final_host == urlsplit(url).netloc.lower()
            tsha = cap.canonical_text_sha(text) if text else None
            clash = known_text.get(tsha) if tsha else None
            entry = {"id": s["id"], "model": s["model"], "url": url,
                     "markers": markers, "names_model": named,
                     "clash": clash if clash and clash != s["id"] else None}
            # one place decides why a document was not promoted; the report and
            # the operator's queue must never give different reasons
            entry["why"] = (
                f"its text is already held for {entry['clash']}" if entry["clash"] else
                "it does not name this model" if not named else
                f"it redirected off {urlsplit(url).netloc} to {final_host}"
                if not same_host else
                "it carries no phrase distinctive to the Art. 53(1)(d) template"
                if not distinctive else
                f"only {markers} Art. 53(1)(d) template marker(s)")
            if (markers >= MIN_MARKERS and distinctive and named and same_host
                    and not entry["clash"]):
                promoted.setdefault(s["id"], []).append(
                    {"kind": "provider-live", "url": url,
                     "note": f"found by probe_missing {cap.utc_now()[:10]}: the "
                             f"provider's own URL pattern; document fetched, carries "
                             f"the Art. 53(1)(d) template and names {s['model']}"})
                print(f"  PROMOTED {s['id']} -> {url}", flush=True)
            else:
                candidates.append(entry)
                print(f"  candidate {s['id']} -> {url} ({entry['why']})", flush=True)

    if promoted:
        existing = (json.loads(DISCOVERED.read_text(encoding="utf-8"))
                    if DISCOVERED.exists() else {})
        for sid, items in promoted.items():
            have = {i["url"] for i in existing.get(sid, [])}
            existing.setdefault(sid, []).extend(i for i in items if i["url"] not in have)
        cap.atomic_write_text(DISCOVERED, json.dumps(existing, indent=2,
                                                     ensure_ascii=False))

    if candidates:
        # A document we fetched but would not attribute on our own. It is
        # recorded rather than acted on: since the approval queue was retired the
        # ledger follows AIAL's list, and an attribution this project would not
        # make by machine is not one it should make by guessing.
        import decisions as dec
        queued = dec.add_candidates([{
            "source_id": c["id"], "model": c["model"], "url": c["url"],
            "provider": next((s.get("provider") for s in sources
                              if s["id"] == c["id"]), None),
            "classification": "fetched by the Tier-1 probe",
            "why": c["why"],
        } for c in candidates])
        dec.save(dec.PENDING, queued)

    lines = [f"# Probe of missing summaries — {cap.utc_now()[:10]}", "",
             f"Probed {len(missing)} model(s) marked missing against "
             f"{sum(len(v) for v in patterns.values())} provider URL pattern(s); "
             f"{tried} URL(s) fetched.", ""]
    lines.append(f"**Promoted to the registry: {sum(len(v) for v in promoted.values())}**"
                 + (" — each fetched, carrying the Art. 53(1)(d) template and naming "
                    "its model." if promoted else "."))
    for sid, items in sorted(promoted.items()):
        for i in items:
            lines.append(f"- `{sid}` → {i['url']}")
    lines += ["", "## Fetched but NOT promoted (a human should judge these)", ""]
    if candidates:
        lines.append("| Source | URL | Template markers | Names the model | Already held for |")
        lines.append("|---|---|---|---|---|")
        for c in candidates:
            lines.append(f"| {c['id']} | {c['url'][:80]} | {c['markers']} | "
                         f"{'yes' if c['names_model'] else 'no'} | {c['clash'] or '—'} |")
    else:
        lines.append("None — every fetched document either qualified or the URL did "
                     "not resolve.")
    lines += ["", "Nothing here is ever removed or rewritten; promotion only ever "
                  "ADDS a target, and the next sweep captures it with a hash, a "
                  "timestamp proof and a Wayback witness before the site says "
                  "anything about it.", ""]
    cap.atomic_write_text(REPORT, "\n".join(lines))
    print(f"\npromoted {sum(len(v) for v in promoted.values())}, "
          f"{len(candidates)} candidate(s) for review; report at {REPORT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
