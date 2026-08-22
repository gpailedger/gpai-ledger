"""Tier-2 relocation hunt: bounded same-domain crawl with fingerprint matching.

For targets whose recorded URL has failed on consecutive sweeps, crawl the provider's
own site (bounded BFS) hunting for the document at a new location. A candidate only
ever becomes a registry target through content proof:

  byte-identical or char-similarity >= 0.98  -> CONFIRMED relocation: written to
      crawler/relocations.json, which build_registry merges as a provider-live target
  0.85 <= similarity < 0.98                  -> candidate, reported for human review
  otherwise                                  -> not a match

Status vocabulary (used in reports; never "removed"/"deleted" without further evidence):
  unreachable-at-recorded-location (N sweeps)   error streak, hunt not yet conclusive
  relocated (fingerprint-confirmed)             confirmed match at a new URL
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

import requests
from bs4 import BeautifulSoup

import capture as cap

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RELOCATIONS = Path(__file__).parent / "relocations.json"
ERROR_STREAK_THRESHOLD = 2
DOC_HINT = re.compile(r"\.pdf($|\?)|summary|training|transparen|ai-?act|article.?53|legal|policy", re.I)
SKIP_EXT = re.compile(r"\.(png|jpe?g|svg|gif|css|js|woff2?|ico|mp4|webm|zip)($|\?)", re.I)
# third-party document hosts that are not a provider's own crawlable site
THIRD_PARTY_HOSTS = ("drive.google.com", "huggingface.co", "storage.googleapis.com",
                     "cdn.sanity.io", "media.x.ai", "fbcdn.net", "cdn.", "us.aws.cdn.hf.co")


def char_stream(text: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", text.lower()))


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, char_stream(a), char_stream(b),
                                   autojunk=False).ratio()


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
        key = (e.get("source"), e.get("target"))
        if e.get("outcome") == "error" and e.get("absence") not in (None, "confirmed"):
            # an absence claim that is unconfirmed (one vantage point, no
            # corroboration) or contradicted (a fresh witness saw the document
            # live) is not evidence the document moved — no hunt
            continue
        if e.get("outcome") == "error":
            entry = last.setdefault(key, {"streak": 0, "url": e.get("url"),
                                          "kind": e.get("kind"), "last_ts": ""})
            entry["streak"] += 1
            entry["url"] = e.get("url") or entry["url"]
            entry["last_ts"] = e.get("ts") or entry["last_ts"]
        elif e.get("outcome") in ("new", "unchanged", "unchanged-content",
                                  "recheck-recovered"):
            last.pop(key, None)  # streak broken for this target
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


def stored_text(source_id: str, target_slug: str = None):
    """Fingerprint baseline: the dead target's own last extracted text, falling back
    to the newest text from any of the source's targets."""
    state = json.loads((DATA / "state.json").read_text(encoding="utf-8"))
    exact, fallback = None, None
    for key, entry in state.items():
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


def probe_redirect(url: str, domain: str, max_hops: int = 5):
    """Return the redirect target only if it stays on the provider's site. Hops
    are followed one at a time and only while they stay on-site, so an off-site
    Location is never requested (CRAWLING.md: the hunt never leaves the domain)."""
    cur = url
    try:
        for _ in range(max_hops):
            r = requests.head(cur, headers=cap.HEADERS, timeout=30, allow_redirects=False)
            loc = r.headers.get("Location") or ""
            if r.status_code in (301, 302, 303, 307, 308) and loc:
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
            r = requests.get(f"https://{domain}{path}", headers=cap.HEADERS, timeout=30)
            if r.status_code == 200:
                out += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)[:500]
        except Exception:  # noqa: BLE001
            continue
    return [u for u in out if DOC_HINT.search(u) and same_site(u, domain)]


def crawl_domain(domain: str, seeds, max_pages: int, max_requests: int = 300,
                 throttle: float = 1.0):
    """Bounded same-domain BFS. Yields (page_url, [candidate_doc_urls]).

    Every fetch attempt counts toward max_requests and is followed by a throttle
    sleep, so total outbound volume is hard-bounded regardless of link fan-out.
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
            r = requests.get(url, headers=cap.HEADERS, timeout=30)
            time.sleep(throttle)
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
        candidates = [l for l in links if DOC_HINT.search(l)]
        yield url, candidates
        for l in links:
            if l not in seen:
                queue.append((l, depth + 1))


def fingerprint_check(candidate_url: str, baseline: str):
    """Fetch a candidate and score it against the baseline text."""
    try:
        raw, meta = cap.fetch(candidate_url, retries=0, timeout=60)
    except Exception:  # noqa: BLE001
        return None, 0.0
    ext = cap.guess_ext(meta["content_type"], meta["final_url"], raw)
    text, _ = cap.extract_text(raw, ext)
    if not text:
        return None, 0.0
    return (raw, meta, ext, text), similarity(baseline, text)


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
    found_any = False

    if not streaks:
        lines.append("No targets with an active error streak — nothing to hunt.")
    for (source_id, target), info in streaks.items():
        dead_url = info["url"]
        domain = urlparse(dead_url).netloc
        baseline = stored_text(source_id, target_slug=target)
        lines.append(f"## {source_id} — unreachable-at-recorded-location "
                     f"({info['streak']} sweeps)\n\nDead URL: `{dead_url}`\n")
        if not baseline:
            lines.append("No stored text to fingerprint against — manual review needed.\n")
            continue

        # crawl the provider's OWN domain: third-party document hosts (Drive, HF CDNs,
        # Google/Sanity/x.ai/fbcdn buckets) aren't the provider's site to walk
        provider_domain = domain
        if any(h in domain for h in THIRD_PARTY_HOSTS):
            reg = json.loads((Path(__file__).parent / "sources.json").read_text(encoding="utf-8"))
            for s in reg["sources"]:
                if s["id"] == source_id:
                    for t in s.get("targets", []):
                        d = urlparse(t["url"]).netloc
                        if d and not any(h in d for h in THIRD_PARTY_HOSTS):
                            provider_domain = d
                            break

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
        for page, page_candidates in crawl_domain(provider_domain, seeds, args.max_pages):
            candidates += page_candidates

        best_sim, best_url = 0.0, None
        # only ever fetch candidates that stay on the provider's site
        for cand in dict.fromkeys(c for c in candidates if same_site(c, provider_domain)):
            _, sim = fingerprint_check(cand, baseline)
            if sim > best_sim:
                best_sim, best_url = sim, cand
            if sim >= 0.98:
                break

        if best_url and best_sim >= 0.98:
            found_any = True
            relocations.setdefault(source_id, [])
            if not any(r["url"] == best_url for r in relocations[source_id]):
                relocations[source_id].append({
                    "kind": "provider-live", "url": best_url,
                    "note": f"auto-relocation, fingerprint-confirmed {now} "
                            f"(similarity {best_sim:.4f}, replaces {dead_url})",
                })
            lines.append(f"**relocated (fingerprint-confirmed):** `{best_url}` "
                         f"similarity {best_sim:.4f} — added to relocations.json\n")
        elif best_url and best_sim >= 0.85:
            lines.append(f"**candidate (unconfirmed):** `{best_url}` similarity "
                         f"{best_sim:.4f} — human review needed\n")
        else:
            lines.append(f"**not-found-at-any-known-location** (crawled ≤{args.max_pages} "
                         f"pages on {provider_domain}, sitemap probed, redirect probed; "
                         f"best similarity {best_sim:.2f})\n")

    RELOCATIONS.write_text(json.dumps(relocations, indent=2, ensure_ascii=False),
                           encoding="utf-8", newline="\n")
    (ROOT / "reports" / "hunt-latest.md").write_text("\n".join(lines) + "\n",
                                                     encoding="utf-8", newline="\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
