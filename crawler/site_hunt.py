"""Tier-2 relocation hunt: bounded same-domain crawl with fingerprint matching.

For targets whose recorded URL has failed on consecutive sweeps, crawl the provider's
own site (bounded BFS) hunting for the document at a new location. A candidate only
ever becomes a registry target through content proof, and only when that proof is
unambiguous:

  byte-identical to an archived version of this target       -> CONFIRMED relocation
  char-similarity >= CONFIRM_SIM to this target's own text,
  no other candidate >= CONFIRM_SIM, and closer to this
  target's text than to any sibling model's summary of the
  same provider by SIBLING_MARGIN                             -> CONFIRMED relocation
  CANDIDATE_SIM <= similarity, but not provably THE document  -> candidate, human review
  otherwise                                                   -> not a match

Confirmed relocations are written to crawler/relocations.json, which build_registry
merges as a provider-live target. Summaries of sibling models from one provider follow
the same template and sit at 0.99+ similarity to each other, so similarity alone can
never identify THE document — hence the sibling margin and the uniqueness rule.

Confinement: the hunt requests only URLs on the provider's own site. A redirect is
followed one hop at a time and only while it stays on-site (probe_redirect,
_resolve_on_site); the crawl, the sitemap read and the candidate fetch never leave
the domain. Third-party document hosts (Drive, Hugging Face, CDNs) and the AIAL
archive are never crawled: a source whose only locations are there gets
"no provider site to hunt".

Bounds: at most MAX_CANDIDATES candidates are scored per source (PDF links first),
streams are capped at STREAM_CAP identity characters per side (alignment is
quadratic) and scoring stops after HUNT_BUDGET_S seconds per source; what was
dropped is said in the report.

Status vocabulary (used in reports; never "removed"/"deleted" without further evidence):
  unreachable-at-recorded-location (N sweeps)   error streak, hunt not yet conclusive
  recovered-at-recorded-location                the recorded URL answered 200 this run
  relocated (fingerprint-confirmed)             confirmed match at a new URL
  candidate (unconfirmed)                       similar, but not provably the document
  no provider site to hunt                      nothing this hunt may crawl
  not-found-at-any-known-location               hunt exhausted this run

Also probes the dead URL itself for redirects and the domain's sitemap.xml.
Usage: python site_hunt.py [--max-pages N] [--only SUBSTRING]
"""
import argparse
import difflib
import json
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import capture as cap

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RELOCATIONS = Path(__file__).parent / "relocations.json"
REGISTRY = Path(__file__).parent / "sources.json"
ERROR_STREAK_THRESHOLD = 2
CONFIRM_SIM = 0.98
CANDIDATE_SIM = 0.85
SIBLING_MARGIN = 0.002   # sibling summaries of one provider sit at 0.993-0.998 to each other
MAX_CANDIDATES = 40
STREAM_CAP = 20000
HUNT_BUDGET_S = 600
PROVIDER_KINDS = ("provider-live", "provider-page", "cop-doc")
# hosts this hunt must never crawl, whatever the registry says
NEVER_CRAWL = ("aial.ie", "archive.org")
DOC_HINT = re.compile(r"\.pdf($|\?)|summary|training|transparen|ai-?act|article.?53|legal|policy", re.I)
SKIP_EXT = re.compile(r"\.(png|jpe?g|svg|gif|css|js|woff2?|ico|mp4|webm|zip)($|\?)", re.I)
REDIRECTS = (301, 302, 303, 307, 308)
# third-party document hosts that are not a provider's own crawlable site
THIRD_PARTY_HOSTS = ("drive.google.com", "huggingface.co", "storage.googleapis.com",
                     "cdn.sanity.io", "media.x.ai", "fbcdn.net", "cdn.", "us.aws.cdn.hf.co")


def char_stream(text: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", text.lower()))[:STREAM_CAP]


def similarity(a: str, b: str) -> float:
    """Character-stream similarity of two texts (first STREAM_CAP identity chars).
    Values below CANDIDATE_SIM are upper bounds: a candidate that cannot reach the
    review threshold is never aligned, since alignment is quadratic."""
    sa, sb = char_stream(a), char_stream(b)
    if not sa or not sb:
        return 0.0
    sm = difflib.SequenceMatcher(None, sa, sb, autojunk=False)
    if sm.real_quick_ratio() < CANDIDATE_SIM:
        return round(sm.real_quick_ratio(), 4)
    if sm.quick_ratio() < CANDIDATE_SIM:
        return round(sm.quick_ratio(), 4)
    return sm.ratio()


def error_streaks(events_path: Path):
    """Return {(source,target): {streak, url, kind}} for targets whose most recent
    outcomes are consecutive errors, EXCLUDING targets whose source has another
    target succeeding more recently than the errors (a superseded target on a
    healthy source needs no hunt — the successor is already being captured)."""
    last, source_success_ts = {}, {}
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        key = (e.get("source"), e.get("target"))
        if e.get("outcome") == "error" and e.get("absence") not in (None, "confirmed",
                                                                      "persistent"):
            # an absence claim that is unconfirmed (one vantage point, one day)
            # or contradicted (a fresh witness saw the document live) is not
            # evidence the document moved — no hunt; confirmed and persistent
            # (several dates) claims are worth a relocation search
            continue
        if e.get("outcome") == "error":
            entry = last.setdefault(key, {"streak": 0, "url": e.get("url"),
                                          "kind": e.get("kind"), "last_ts": ""})
            entry["streak"] += 1
            entry["url"] = e.get("url") or entry["url"]
            entry["last_ts"] = e.get("ts") or entry["last_ts"]
        elif e.get("outcome") in ("new", "unchanged", "unchanged-content",
                                  "recheck-recovered", "live-attested"):
            last.pop(key, None)  # streak broken for this target (or attested live)
            ts = e.get("ts") or ""
            # Only a live-DOCUMENT success suppresses a sibling's hunt. A provider-page
            # / watch-page success is a different document (an overview page) and must
            # NOT hide a dead document behind it.
            if e.get("kind", "") == "provider-live":
                source_success_ts[e.get("source")] = max(
                    source_success_ts.get(e.get("source"), ""), ts)
    return {k: v for k, v in last.items()
            if v["streak"] >= ERROR_STREAK_THRESHOLD
            and source_success_ts.get(k[0], "") < v["last_ts"]}


def _state() -> dict:
    p = DATA / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def stored_text(source_id: str, target_slug: str = None):
    """Fingerprint baseline: the dead target's own last extracted text, falling back
    to the newest text from any of the source's targets."""
    exact, fallback = None, None
    for key, entry in _state().items():
        if entry.get("retired") or not key.startswith(source_id + "::"):
            continue
        tslug = key.split("::", 1)[1]
        for v in entry.get("versions", []):
            p = DATA / v["dir"] / "extracted.txt"
            if p.exists():
                fallback = p
                if target_slug and tslug == target_slug:
                    exact = p
    best = exact or fallback
    return best.read_text(encoding="utf-8") if best else None


def stored_shas(source_id: str, target_slug: str = None) -> set:
    """SHA-256 of every archived version of the target (of the source when the
    target is unknown): a byte-identical candidate is the document, wherever it sits."""
    out = set()
    for key, entry in _state().items():
        if not key.startswith(source_id + "::"):
            continue
        if target_slug and key.split("::", 1)[1] != target_slug:
            continue
        out.update(v["sha256"] for v in entry.get("versions", []))
    return out


def _registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8")) if REGISTRY.exists() else {"sources": []}


def sibling_texts(source_id: str):
    """[(sibling id, newest stored text)] for the other models of the same provider:
    their summaries are near-duplicates of each other by template."""
    reg = _registry()
    provider = next((s.get("provider") for s in reg["sources"] if s["id"] == source_id), None)
    out = []
    if not provider:
        return out
    for s in reg["sources"]:
        if s["id"] == source_id or s.get("provider") != provider or s.get("retired"):
            continue
        text = stored_text(s["id"])
        if text:
            out.append((s["id"], text))
    return out


def _norm_host(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host  # not lstrip: that strips a char set


def same_site(url: str, domain: str) -> bool:
    """True only if url is https and its host is `domain` or a subdomain of it."""
    p = urlparse(url)
    if p.scheme != "https":
        return False
    host, dom = _norm_host(p.hostname or ""), _norm_host(domain)
    return bool(dom) and (host == dom or host.endswith("." + dom))


def never_crawl(host: str) -> bool:
    h = _norm_host(host)
    return any(h == n or h.endswith("." + n) for n in NEVER_CRAWL)


def third_party(host: str) -> bool:
    return any(h in (host or "") for h in THIRD_PARTY_HOSTS)


def _resolve_on_site(url: str, domain: str, max_hops: int = 5):
    """Follow redirects one hop at a time, only while they stay on the provider's
    site (an off-site Location is never requested). Returns the final on-site URL
    (the URL itself when it answers directly) or None. HEAD is used for the hops;
    a server that refuses HEAD (403/405) is left to the GET that follows."""
    cur = url
    for _ in range(max_hops):
        r = cap.guarded_request("HEAD", cur, timeout=30, allow_redirects=False)
        loc = r.headers.get("Location") or ""
        if r.status_code in REDIRECTS and loc:
            nxt = urljoin(cur, loc)
            if not same_site(nxt, domain):
                return None
            cur = nxt
            continue
        if r.status_code in (404, 410):
            return None
        return cur
    return None


def probe_redirect(url: str, domain: str, max_hops: int = 5):
    """Return the redirect target only if it stays on the provider's site. Hops
    are followed one at a time and only while they stay on-site, so an off-site
    Location is never requested (CRAWLING.md: the hunt never leaves the domain)."""
    cur = url
    try:
        for _ in range(max_hops):
            r = cap.guarded_request("HEAD", cur, timeout=30, allow_redirects=False)
            loc = r.headers.get("Location") or ""
            if r.status_code in REDIRECTS and loc:
                nxt = urljoin(cur, loc)
                if not same_site(nxt, domain):
                    return None
                cur = nxt
                continue
            if r.status_code == 200 and cur.rstrip("/") != url.rstrip("/"):
                return cur
            return None
    except Exception:  # noqa: BLE001
        pass
    return None


def sitemap_urls(domain: str):
    out = []
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        try:
            # a sitemap that redirects elsewhere is not this site's sitemap
            r = cap.guarded_request("GET", f"https://{domain}{path}", timeout=30,
                             allow_redirects=False)
            if r.status_code == 200:
                out += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)[:500]
        except Exception:  # noqa: BLE001
            continue
    return list(dict.fromkeys(u for u in out if DOC_HINT.search(u) and same_site(u, domain)))


def crawl_domain(domain: str, seeds, max_pages: int, max_requests: int = 300,
                 throttle: float = 1.0):
    """Bounded same-domain BFS. Yields (page_url, [candidate_doc_urls]).

    Every fetch attempt counts toward max_requests and is followed by a throttle
    sleep, so total outbound volume is hard-bounded regardless of link fan-out.
    Redirects are not followed by the client: an on-site Location is queued like
    any other link, an off-site one is dropped unrequested.
    """
    seen, queue = set(), deque()
    for s in seeds:
        queue.append((s, 0))
    requests_made = 0
    while queue and requests_made < min(max_requests, max_pages):
        url, depth = queue.popleft()
        if url in seen or depth > 3 or not same_site(url, domain):
            continue
        seen.add(url)
        requests_made += 1
        try:
            r = cap.guarded_request("GET", url, timeout=30, allow_redirects=False)
            time.sleep(throttle)
            if r.status_code in REDIRECTS:
                loc = urljoin(url, r.headers.get("Location") or "")
                if loc and same_site(loc, domain) and loc not in seen:
                    queue.append((loc, depth + 1))
                continue
            if r.status_code != 200 or "html" not in (r.headers.get("Content-Type") or ""):
                continue
        except Exception:  # noqa: BLE001
            time.sleep(throttle)
            continue
        soup = BeautifulSoup(r.content, "html.parser")
        links = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"]).split("#")[0]
            if not same_site(href, domain) or SKIP_EXT.search(href):
                continue
            links.add(href)
        candidates = [link for link in links if DOC_HINT.search(link)]
        yield url, candidates
        for link in links:
            if link not in seen:
                queue.append((link, depth + 1))


def fingerprint_check(candidate_url: str, baseline: str, domain: str):
    """Fetch a candidate — only if it resolves on the provider's site — and score
    it against the baseline text. Returns ((raw, meta, ext, text), similarity)."""
    try:
        final = _resolve_on_site(candidate_url, domain)
        if not final:
            return None, 0.0
        raw, meta = cap.fetch(final, retries=0, timeout=60)
    except Exception:  # noqa: BLE001
        return None, 0.0
    if raw is None or not same_site(meta.get("final_url") or final, domain):
        return None, 0.0
    ext = cap.guess_ext(meta["content_type"], meta["final_url"], raw)
    text, _ = cap.extract_text(raw, ext)
    if not text:
        return None, 0.0
    return (raw, meta, ext, text), similarity(baseline, text)


def provider_site(source_id: str, dead_url: str):
    """The domain this hunt may crawl for a source, or None: the dead URL's own
    host when it is the provider's site; otherwise the host of a provider-owned
    target (provider-live / provider-page / cop-doc) that is neither a
    third-party document host nor a site on the NEVER_CRAWL list."""
    domain = urlparse(dead_url).netloc
    if domain and not third_party(domain) and not never_crawl(domain):
        return domain
    for s in _registry()["sources"]:
        if s["id"] != source_id:
            continue
        for t in s.get("targets", []):
            d = urlparse(t["url"]).netloc
            if (t.get("kind") in PROVIDER_KINDS and d and not third_party(d)
                    and not never_crawl(d)):
                return d
    return None


def judge(scored, own_shas):
    """(confirmed, reason) for the best-scored candidate. scored: list of
    (similarity, url, sha256, best_sibling_similarity), best first."""
    if not scored:
        return False, ""
    sim, _url, sha, sib = scored[0]
    if sha in own_shas:
        return True, "byte-identical to an archived version of this target"
    if sim < CONFIRM_SIM:
        return False, f"similarity {sim:.4f}"
    strong = [s for s in scored if s[0] >= CONFIRM_SIM]
    if len(strong) > 1:
        return False, (f"ambiguous: {len(strong)} candidates score >= {CONFIRM_SIM} "
                       f"({', '.join(f'{s[0]:.4f}' for s in strong)})")
    if sib >= sim - SIBLING_MARGIN:
        return False, (f"not attributable: a sibling model's summary scores {sib:.4f} "
                       f"against this candidate vs {sim:.4f} for this model's own text")
    return True, f"similarity {sim:.4f}; best sibling {sib:.4f}; no other candidate >= {CONFIRM_SIM}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=120)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    streaks = error_streaks(DATA / "events.jsonl")
    if args.only:
        streaks = {k: v for k, v in streaks.items() if args.only.lower() in k[0].lower()}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Relocation hunt — {now}", ""]
    relocations = (json.loads(RELOCATIONS.read_text(encoding="utf-8"))
                   if RELOCATIONS.exists() else {})

    if not streaks:
        lines.append("No targets with an active error streak — nothing to hunt.")
    for (source_id, target), info in streaks.items():
        dead_url = info["url"]
        lines.append(f"## {source_id} — unreachable-at-recorded-location "
                     f"({info['streak']} sweeps)\n\nDead URL: `{dead_url}`\n")

        # rung 0: is the recorded location simply back? (the runner has seen
        # intermittent 404s that other networks never saw)
        try:
            raw0, meta0 = cap.fetch(dead_url, retries=0, timeout=60)
        except Exception:  # noqa: BLE001
            raw0, meta0 = None, None
        if raw0 is not None and meta0 and meta0.get("status_code") == 200:
            same = cap.sha256_hex(raw0) in stored_shas(source_id, target)
            lines.append(f"**recovered-at-recorded-location** — the recorded URL answered "
                         f"HTTP 200 this run"
                         + (", bytes identical to an archived version" if same else
                            ", bytes differ from the archived versions (the sweep will "
                            "capture them)")
                         + "; no relocation recorded\n")
            continue

        baseline = stored_text(source_id, target_slug=target)
        if not baseline:
            lines.append("No stored text to fingerprint against — manual review needed.\n")
            continue

        provider_domain = provider_site(source_id, dead_url)
        if not provider_domain:
            lines.append("**no provider site to hunt** — the recorded location is on a "
                         "third-party host and the source has no provider-owned site "
                         "this hunt may crawl; manual review needed\n")
            continue

        # rung 1: redirect at the dead URL (confined to the provider's site)
        redir = probe_redirect(dead_url, provider_domain)
        candidates = ([redir] if redir else []) + sitemap_urls(provider_domain)

        # rung 2: bounded same-site crawl
        # seed with the domain root plus the surfaces AIAL's discovery methodology
        # identifies as where summaries live (legal/compliance pages, docs)
        seeds = [f"https://{provider_domain}/"] + [
            f"https://{provider_domain}{p}" for p in
            ("/legal", "/legal/", "/compliance", "/trust", "/transparency",
             "/security-and-compliance", "/privacy")]
        for _page, page_candidates in crawl_domain(provider_domain, seeds, args.max_pages):
            candidates += page_candidates

        dead_norm = dead_url.split("#")[0].rstrip("/")
        cands = [c for c in dict.fromkeys(candidates)
                 if same_site(c, provider_domain) and c.split("#")[0].rstrip("/") != dead_norm]
        cands.sort(key=lambda u: 0 if re.search(r"\.pdf($|\?)", u, re.I) else 1)
        dropped = max(0, len(cands) - MAX_CANDIDATES)
        cands = cands[:MAX_CANDIDATES]

        own_shas = stored_shas(source_id, target)
        siblings = sibling_texts(source_id)
        scored, budget_hit = [], False
        t0 = time.monotonic()
        for cand in cands:
            if time.monotonic() - t0 > HUNT_BUDGET_S:
                budget_hit = True
                break
            res, sim = fingerprint_check(cand, baseline, provider_domain)
            if res is None:
                continue
            raw, _meta, _ext, text = res
            sib = (max((similarity(st, text) for _sid, st in siblings), default=0.0)
                   if sim >= CONFIRM_SIM else 0.0)
            scored.append((sim, cand, cap.sha256_hex(raw), sib))
        scored.sort(key=lambda s: -s[0])
        confirmed, why = judge(scored, own_shas)
        bounds = ((f" {dropped} further candidate(s) were not scored (cap {MAX_CANDIDATES});"
                   if dropped else "")
                  + (f" scoring stopped at the {HUNT_BUDGET_S} s budget;" if budget_hit else ""))

        if confirmed:
            best_sim, best_url = scored[0][0], scored[0][1]
            relocations.setdefault(source_id, [])
            if not any(r["url"] == best_url for r in relocations[source_id]):
                relocations[source_id].append({
                    "kind": "provider-live", "url": best_url,
                    "note": f"auto-relocation, fingerprint-confirmed {now} "
                            f"({why}; replaces {dead_url})",
                })
            lines.append(f"**relocated (fingerprint-confirmed):** `{best_url}` "
                         f"similarity {best_sim:.4f} — {why}; added to relocations.json\n")
        elif scored and scored[0][0] >= CANDIDATE_SIM:
            lines.append(f"**candidate (unconfirmed):** `{scored[0][1]}` similarity "
                         f"{scored[0][0]:.4f} — {why}; human review needed\n")
        else:
            best = f"{scored[0][0]:.2f}" if scored else "none"
            lines.append(f"**not-found-at-any-known-location** (crawled ≤{args.max_pages} "
                         f"pages on {provider_domain}, sitemap probed, redirect probed; "
                         f"best similarity {best}{bounds})\n")
        if bounds and (confirmed or (scored and scored[0][0] >= CANDIDATE_SIM)):
            lines.append(f"Bounds:{bounds}\n")

    RELOCATIONS.write_text(json.dumps(relocations, indent=2, ensure_ascii=False),
                           encoding="utf-8", newline="\n")
    (ROOT / "reports" / "hunt-latest.md").write_text("\n".join(lines) + "\n",
                                                     encoding="utf-8", newline="\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
