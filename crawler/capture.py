"""Capture engine + provenance rig for the GPAI Ledger.

Every capture stores: raw bytes as fetched (or a rendered DOM for render targets,
marked in the manifest), SHA-256, normalized extracted text, and a manifest with HTTP
metadata. New versions are detected by content hash. Provenance per new version: a
triggered Wayback Machine save and an OpenTimestamps proof (.ots).

OpenTimestamps stamping uses the opentimestamps python library directly rather than
the `ots` CLI — no CLI dependency, portable across dev and CI. Stamping never needs
the library's bitcoin-RPC path.
"""
import hashlib
import io
import json
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

USER_AGENT = ("GPAI-Ledger/0.1 (public-interest archive of EU AI Act Article 53(1)(d) "
              "training-data summaries; contact: contact@gpailedger.com)")
HEADERS = {"User-Agent": USER_AGENT}
# Where an observation was made from. Absence claims are only as strong as their
# vantage point: a datacenter runner can see a 404 an origin never shows to others.
VANTAGE = "github-runner" if os.environ.get("GITHUB_ACTIONS") else "operator"
OTS_CALENDARS = [
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
    "https://ots.btc.catallaxy.com",
]

EXT_BY_TYPE = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "text/html": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
}


def atomic_write_text(path, text: str) -> None:
    """Crash-safe write: a kill mid-write must never leave a truncated corpus file
    (a truncated state.json would be committed by the always-run CI step and brick
    every later sweep). Write to a sibling tmp file, then atomically replace."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def atomic_write_bytes(path, data: bytes) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_text_sha(text: str) -> str:
    """Whitespace-collapsed text hash: the change-detection key for extracted text
    (invisible-character churn in dynamic pages must not mint versions)."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def target_slug(kind: str, url: str) -> str:
    return f"{kind}-{hashlib.sha256(url.encode()).hexdigest()[:8]}"


# Every fetched byte is attacker-controllable; cap the body so a hostile origin
# can't OOM an unattended runner. 60 MB clears the largest real document (the ~9.5 MB
# Anthropic bundle) with wide margin.
MAX_FETCH_BYTES = 60 * 1024 * 1024
# 4xx (except 429) are permanent — retrying wastes the sweep and hammers origins.
NO_RETRY_STATUS = {400, 401, 403, 404, 405, 410, 451}
# Zip decompression-bomb caps.
MAX_ZIP_MEMBERS = 2000
MAX_ZIP_MEMBER_BYTES = 100 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 200 * 1024 * 1024

# Removes cookie/consent chrome from a rendered page so the serialized DOM is stable.
# Matches on id/class/attribute naming conventions of consent platforms, and only
# removes a node whose own text also reads like a consent notice — so a compliance
# document that merely discusses cookies is never touched.
CONSENT_STRIP_JS = """() => {
  // Named containers of known consent platforms (CookieYes, OneTrust, Cookiebot,
  // Usercentrics, Didomi, Quantcast, Osano, Complianz, Sourcepoint, iubenda, Klaro,
  // Termly). These names are unambiguous: only the consent widget uses them.
  const platform = [
    '[id*="cky" i]', '[class*="cky-" i]', '#onetrust-banner-sdk', '[id*="onetrust" i]',
    '[class*="ot-sdk" i]', '#CybotCookiebotDialog', '.cc-window',
    '[id*="usercentrics" i]', '[class*="uc-banner" i]', '[id*="didomi" i]',
    '[class*="didomi" i]', '[class*="qc-cmp" i]', '[class*="osano" i]',
    '[id*="cmplz" i]', '[class*="cmplz" i]', '[id*="sp_message" i]',
    '[class*="iubenda-cs" i]', '[class*="klaro" i]', '[id*="termly" i]'
  ];
  // Generically-named candidates are removed ONLY if they are also a floating overlay
  // (position: fixed/sticky) — real page content is never a fixed overlay. This keeps
  // inline prose that merely mentions cookies (e.g. a privacy section) intact.
  const generic = [
    '[id*="cookie" i]', '[class*="cookie" i]', '[id*="consent" i]',
    '[class*="consent" i]', '[id*="gdpr" i]', '[class*="gdpr" i]',
    '[aria-label*="cookie" i]', '[data-testid*="cookie" i]', '[class*="cmp-" i]'
  ];
  const phrases = ['we use cookies', 'uses cookies', 'value your privacy',
                   'consent preferences', 'customize consent', 'cookie settings',
                   'manage cookies', 'necessary cookies', 'essential cookies',
                   'third-party cookies', 'consent to the use'];
  const hasPhrase = (n) => {
    const t = (n.textContent || '').toLowerCase();
    return t.length < 25000 && phrases.some(p => t.includes(p));
  };
  const isOverlay = (n) => {
    for (let p = n; p && p !== document.body; p = p.parentElement) {
      const pos = getComputedStyle(p).position;
      if (pos === 'fixed' || pos === 'sticky') return true;
    }
    return false;
  };
  let removed = 0;
  const kill = (list, requireOverlay) => {
    for (const s of list) {
      let nodes;
      try { nodes = document.querySelectorAll(s); } catch (e) { continue; }
      for (const n of nodes) {
        if (!n.isConnected || n === document.body || n === document.documentElement) continue;
        if (!hasPhrase(n)) continue;
        if (requireOverlay && !isOverlay(n)) continue;
        n.remove();
        removed++;
      }
    }
  };
  kill(platform, false);
  kill(generic, true);
  return removed;
}"""


class PermanentFetchError(RuntimeError):
    """Definitive fetch failure (permanent 4xx, oversized body): retrying is
    useless and hammers origins, so the retry loop re-raises immediately.
    status_code and headers (a diagnostic subset) let callers classify and
    record what the origin actually answered."""

    def __init__(self, message: str, status_code: int = None, headers: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


DIAG_HEADERS = ("Server", "Via", "X-Cache", "X-Azure-Ref", "X-Served-By", "CF-Ray",
                "X-Request-Id", "Date")


def diag_headers(r) -> dict:
    return {h: r.headers.get(h) for h in DIAG_HEADERS if r.headers.get(h)}


def _read_capped(r, limit: int = MAX_FETCH_BYTES) -> bytes:
    chunks, total = [], 0
    for chunk in r.iter_content(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise PermanentFetchError(f"response exceeds {limit} bytes — refusing to buffer")
        chunks.append(chunk)
    return b"".join(chunks)


def _assert_public_http(url: str) -> None:
    """Refuse non-http(s) schemes and private/loopback literal-IP hosts. Mined URLs
    come out of rendered third-party DOMs — never let one point the crawler at
    file:, javascript:, or an internal address."""
    import ipaddress
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise RuntimeError(f"refusing non-http(s) URL scheme: {url[:120]}")
    host = parts.hostname or ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # hostname, not an IP literal
    if not ip.is_global:
        raise RuntimeError(f"refusing non-public address {host} in {url[:120]}")


def fetch(url: str, retries: int = 2, timeout: int = 90, validators: dict = None):
    """Fetch a URL; returns (bytes, meta dict). Raises the last error on failure.

    Body is streamed and capped at MAX_FETCH_BYTES. Permanent 4xx raise
    PermanentFetchError (never retried); 429/503 honor Retry-After when present.
    validators: optional {"etag", "last_modified"} from the prior capture's
    manifest, sent as If-None-Match / If-Modified-Since. A 304 answer returns
    (None, meta) with meta["status_code"] == 304 and no body downloaded — the
    caller records an origin-asserted "unchanged", not a hash-verified one.
    """
    _assert_public_http(url)
    headers = dict(HEADERS)
    if validators:
        if validators.get("etag"):
            headers["If-None-Match"] = validators["etag"]
        if validators.get("last_modified"):
            headers["If-Modified-Since"] = validators["last_modified"]
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout,
                             allow_redirects=True, stream=True)
            declared = r.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_FETCH_BYTES:
                r.close()
                raise PermanentFetchError(f"Content-Length {declared} exceeds cap for {url}")
            meta = {
                "url": url,
                "final_url": r.url,
                "status_code": r.status_code,
                "content_type": (r.headers.get("Content-Type") or "").split(";")[0].strip(),
                "etag": r.headers.get("ETag"),
                "last_modified": r.headers.get("Last-Modified"),
                "content_length": declared,
                "fetched_at": utc_now(),
            }
            if r.status_code == 304 and validators:
                r.close()
                return None, meta
            if r.status_code == 200:
                body = _read_capped(r)
                return body, meta
            r.close()
            if r.status_code in NO_RETRY_STATUS:
                raise PermanentFetchError(f"HTTP {r.status_code} for {url}",
                                          status_code=r.status_code,
                                          headers=diag_headers(r))
            last = RuntimeError(f"HTTP {r.status_code} for {url}")
            retry_after = r.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 120))
                continue
        except PermanentFetchError:
            raise
        except Exception as exc:  # noqa: BLE001 — record and retry
            last = exc
        time.sleep(3 * (attempt + 1))
    raise last


def guess_ext(content_type: str, url: str, raw: bytes = None) -> str:
    if (content_type or "").lower() in EXT_BY_TYPE:
        return EXT_BY_TYPE[content_type.lower()]
    tail = url.split("?")[0].lower()
    for ext in (".pdf", ".zip", ".html", ".md", ".txt", ".docx", ".doc", ".json"):
        if tail.endswith(ext):
            return ext
    # magic-byte sniffing: extensionless download URLs served as octet-stream
    if raw:
        if raw[:5] == b"%PDF-":
            return ".pdf"
        if raw[:4] == b"PK\x03\x04":
            return ".zip"
        if raw[:200].lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
            return ".html"
    return ".bin"


def extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extract_html_text(data: bytes) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n")


LIGATURES = str.maketrans({
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
    "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
})


def normalize(text: str) -> str:
    text = text.translate(LIGATURES)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def extract_text(data: bytes, ext: str):
    """Returns (text or None, notes list). ZIPs: concatenates text of inner PDFs."""
    notes = []

    def _finish(text: str):
        n = text.count("�")
        if n:
            notes.append(f"{n} character(s) (typographic ligatures such as the 'ffi' "
                         f"in 'Office') could not be decoded from the source file's "
                         f"embedded font and are shown as �; this affects only this text "
                         f"rendering — the stored file is exact")
        return text, notes

    try:
        if ext == ".pdf":
            return _finish(normalize(extract_pdf_text(data)))
        if ext == ".html":
            return normalize(extract_html_text(data)), notes
        if ext in (".md", ".txt", ".json"):
            return normalize(data.decode("utf-8", errors="replace")), notes
        if ext == ".zip":
            # Zip-bomb guards: a hostile bundle can advertise a small compressed size
            # yet expand to gigabytes. Cap member count and total + per-member
            # decompressed bytes; read each member through a bounded read.
            # sort by filename: archive entry order is nondeterministic for
            # on-demand-generated ZIPs, and text must be a stable content key.
            parts = []
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                infos = sorted(zf.infolist(), key=lambda i: i.filename)
                if len(infos) > MAX_ZIP_MEMBERS:
                    raise RuntimeError(f"zip has {len(infos)} members (> {MAX_ZIP_MEMBERS})")
                if sum(i.file_size for i in infos) > MAX_ZIP_TOTAL_BYTES:
                    raise RuntimeError("zip advertised uncompressed size exceeds cap")
                total = 0
                for info in infos:
                    if info.file_size > MAX_ZIP_MEMBER_BYTES:
                        raise RuntimeError(f"zip member {info.filename} exceeds cap")
                    with zf.open(info) as fh:
                        inner = fh.read(MAX_ZIP_MEMBER_BYTES + 1)
                    if len(inner) > MAX_ZIP_MEMBER_BYTES:
                        raise RuntimeError(f"zip member {info.filename} over-reads cap")
                    total += len(inner)
                    if total > MAX_ZIP_TOTAL_BYTES:
                        raise RuntimeError("zip cumulative decompressed size exceeds cap")
                    notes.append({"inner_file": info.filename, "inner_sha256": sha256_hex(inner)})
                    if info.filename.lower().endswith(".pdf"):
                        # One encrypted/damaged member must never abort the archive:
                        # its bytes are hashed above; only its text is omitted.
                        try:
                            parts.append(f"===== {info.filename} =====\n"
                                         + extract_pdf_text(inner))
                        except Exception as exc:  # noqa: BLE001 — member-scoped
                            notes.append(f"text extraction failed for zip member "
                                         f"{info.filename}: {type(exc).__name__} "
                                         f"(bytes stored and hashed; text omitted)")
            if parts:
                return normalize("\n".join(parts)), notes
            notes.append("zip contained no extractable PDFs; no text extracted")
            return None, notes
        notes.append(f"no extractor for {ext}")
        return None, notes
    except Exception as exc:  # noqa: BLE001
        notes.append(f"extraction failed: {exc!r}")
        return None, notes


def fetch_rendered(url: str, timeout_ms: int = 60000, max_expand_clicks: int = 30):
    """Fetch a JS-heavy page with headless Chromium and return the rendered DOM.

    Returns (dom_bytes, meta). The stored artifact is the serialized DOM after JS
    execution — a DERIVED rendering, not origin bytes; manifests mark it as such.
    After load, a generic disclosure pass clicks elements with aria-expanded="false"
    (accordion/collapsible content) so lazy-revealed text enters the DOM. Clicks are
    same-page only (buttons), capped, and errors are non-fatal.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            resp = page.goto(url, wait_until="load", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass  # busy pages never go idle; capture what we have

            # Cookie/consent banners are page chrome, not disclosure content, and they
            # appear nondeterministically between renders — left in, they make the text
            # key unstable and mint noise versions. Dismiss where a decline control
            # exists, then remove any remaining consent container from the DOM (clicking
            # often only hides it via CSS, leaving the text in the serialization).
            consent_dismissed = None
            for label in ("Decline optional cookies", "Only allow essential cookies",
                          "Reject all", "Only essential", "Decline"):
                try:
                    btn = page.get_by_role("button", name=label, exact=False)
                    if btn.count():
                        btn.first.click(timeout=2500)
                        consent_dismissed = label
                        page.wait_for_timeout(800)
                        break
                except Exception:
                    continue
            clicks = 0
            try:
                for el in page.query_selector_all(
                        'button[aria-expanded="false"], [role="button"][aria-expanded="false"]'):
                    if clicks >= max_expand_clicks:
                        break
                    try:
                        el.click(timeout=2000)
                        clicks += 1
                    except Exception:
                        continue
                if clicks:
                    page.wait_for_timeout(1500)
            except Exception:
                pass

            # Serialize substantive child frames too (HF Spaces, embedded doc viewers):
            # cross-origin frames are reachable via the browser. Skip payment/analytics
            # noise by requiring real text content; give websocket apps time to paint.
            frame_sections, frame_urls = [], []
            child_frames = [f for f in page.frames if f != page.main_frame
                            and (f.url or "").startswith("http")]
            if child_frames:
                page.wait_for_timeout(4000)

            # Strip consent chrome LAST, immediately before serialization: some consent
            # platforms inject their DOM after networkidle, so an earlier pass would
            # find nothing while the nodes still land in the captured artifact.
            consent_removed = 0
            for ctx in [page.main_frame] + child_frames:
                try:
                    consent_removed += ctx.evaluate(CONSENT_STRIP_JS)
                except Exception:
                    continue

            for fr in child_frames:
                try:
                    fhtml = fr.content()
                    ftext, _ = extract_text(fhtml.encode("utf-8"), ".html")
                    if ftext and len(ftext.split()) >= 50:
                        frame_sections.append(
                            f"\n<!-- gpai-ledger:frame {fr.url} -->\n" + fhtml)
                        frame_urls.append(fr.url)
                except Exception:
                    continue

            dom = (page.content() + "".join(frame_sections)).encode("utf-8")
            meta = {
                "url": url,
                "final_url": page.url,
                "status_code": resp.status if resp else None,
                "content_type": "text/html",
                "etag": None,
                "last_modified": (resp.headers.get("last-modified") if resp else None),
                "content_length": str(len(dom)),
                "fetched_at": utc_now(),
                "rendered": True,
                "renderer": "playwright-chromium-headless",
                "expand_clicks": clicks,
                "consent_dismissed": consent_dismissed,
                "consent_nodes_removed": consent_removed,
                "frames_captured": frame_urls,
            }
            status = resp.status if resp else None
            if isinstance(status, int) and status >= 400:
                # a rendered error page is not the document — route it into the
                # same absence handling as a plain-fetch 4xx (run_capture)
                hdrs = {k: v for k, v in (resp.headers or {}).items()
                        if k.lower() in ("server", "via", "x-cache", "date",
                                         "x-azure-ref", "cf-ray", "x-served-by")}
                raise PermanentFetchError(f"HTTP {status} for {url} (rendered)",
                                          status_code=status, headers=hdrs)
            return dom, meta
        finally:
            browser.close()


ZIP_ART53_MEMBER = re.compile(r"training.?data|data.?summary", re.I)
ZIP_NOT_ART53_MEMBER = re.compile(r"AB.?2013", re.I)


def filter_zip_art53(raw: bytes):
    """Repack a provider bundle to its Art. 53 members only (deterministic: sorted
    names, fixed timestamps). Bundles can contain documents marked confidential /
    no-redistribution; this archive's scope is Art. 53 — other members are recorded
    by name and SHA-256 (existence proof) but never stored or served.
    Returns (filtered_zip_bytes, excluded_member_entries)."""
    out = io.BytesIO()
    excluded = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zin, \
         zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in sorted(zin.infolist(), key=lambda i: i.filename):
            data = zin.read(info)
            if ZIP_ART53_MEMBER.search(info.filename) and \
                    not ZIP_NOT_ART53_MEMBER.search(info.filename):
                zi = zipfile.ZipInfo(info.filename, date_time=(2026, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(zi, data)
            else:
                excluded.append({"inner_file": info.filename,
                                 "inner_sha256": sha256_hex(data)})
    return out.getvalue(), excluded


def zip_content_key(notes: list) -> str:
    """Format-agnostic content key for regenerated ZIP bundles: SHA-256 over the
    sorted (inner_file, inner_sha256) pairs recorded by extract_text. Byte-exact per
    member and immune to text-extraction failures — the correct dedupe key for
    on-demand-generated archives whose outer bytes differ every download."""
    pairs = sorted((n["inner_file"], n["inner_sha256"]) for n in notes
                   if isinstance(n, dict) and "inner_sha256" in n)
    return sha256_hex(json.dumps(pairs, ensure_ascii=False).encode("utf-8"))


def wayback_save(url: str, timeout: int = 120):
    """Best-effort triggered Wayback save. Returns outcome dict, never raises.

    A recorded snapshot URL means Save Page Now accepted the request; durable
    presence in the public index is only established by a later CDX check.
    """
    try:
        r = requests.get("https://web.archive.org/save/" + url,
                         headers=HEADERS, timeout=timeout, allow_redirects=True)
        loc = r.headers.get("Content-Location") or ""
        snapshot = None
        if loc.startswith("/web/"):
            snapshot = "https://web.archive.org" + loc
        else:
            # the FIRST redirect hop names the capture SPN assigned; r.url after
            # redirects may be an older nearest capture, so it is not used
            for hop in getattr(r, "history", []) or []:
                hop_loc = hop.headers.get("Location") or ""
                if re.match(r"^https://web\.archive\.org/web/\d{14}/", hop_loc):
                    snapshot = hop_loc
                    break
        # SPN's own answer is the redirect hop that named the capture (else the
        # final response). status_code stays the FINAL response: after a redirect
        # that is the replay of the capture, so it reads 404 for a correctly
        # archived error page — never mistake it for SPN's verdict.
        spn_status = r.status_code
        for hop in getattr(r, "history", []) or []:
            if snapshot and (hop.headers.get("Location") or "") == snapshot:
                spn_status = hop.status_code
                break
        # "accepted" = SPN assigned a capture timestamp (it does so even when the
        # origin answered 4xx: the error page is archived); durable presence in
        # the index is established later by retry_wayback.py --verify
        return {"ok": snapshot is not None, "status_code": r.status_code,
                "spn_status": spn_status, "snapshot": snapshot, "at": utc_now()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc), "at": utc_now()}


WITNESS_FRESH_SLACK_S = 120
WITNESS_ATTEMPTS = 4
WITNESS_POLL_S = 15


def wayback_witness(url: str) -> dict:
    """Ask an independent network (the Internet Archive's crawler — a second
    datacenter vantage, not a residential one) what a URL serves RIGHT NOW.

    Triggers Save Page Now, then reads the assigned capture back through its
    replay. A replay is a genuine capture only if it carries Memento-Datetime,
    and it describes the present only if that Memento-Datetime is not older
    than the request (Wayback redirects exact-timestamp URLs to the NEAREST
    capture, and SPN can hand back a deduplicated capture up to ~30 min old).
    Result: saw = "live" (fresh replay 200) | "absent" (fresh replay 404/410) |
    "inconclusive" (everything else — never treated as absence). Every field an
    auditor needs is recorded: SPN status, replayed URL, Memento-Datetime,
    replay status, redirect chain, origin server header, and a reason.
    """
    from datetime import timedelta
    from email.utils import parsedate_to_datetime
    t0 = datetime.now(timezone.utc)
    save = wayback_save(url)
    spn_status = save.get("spn_status", save.get("status_code"))
    out = {"witness": "wayback", "saw": "inconclusive", "status": None,
           "snapshot": save.get("snapshot"), "spn_status": spn_status,
           "requested_at": t0.strftime("%Y-%m-%dT%H:%M:%SZ"), "memento_datetime": None,
           "final_url": None, "redirects": [], "origin_server": None, "reason": None,
           "at": utc_now()}
    snap = save.get("snapshot")
    if not snap and 429 in (spn_status, save.get("status_code")):
        # SPN itself refused us. A 429 from the replay of an ACCEPTED capture is
        # left to the poll loop below and does not halt witnessing for the run.
        out["reason"] = "rate-limited"
        return out
    if not snap:
        out["reason"] = "spn-no-capture" + (f" (SPN {save.get('status_code')})"
                                            if save.get("status_code") else "")
        return out
    for attempt in range(WITNESS_ATTEMPTS):
        try:
            r = requests.get(snap, headers=HEADERS, timeout=60,
                             allow_redirects=True, stream=True)
            status, memento = r.status_code, r.headers.get("Memento-Datetime")
            out["final_url"] = getattr(r, "url", None)
            out["redirects"] = [(h.status_code, h.headers.get("Location"))
                                for h in (getattr(r, "history", []) or [])]
            out["origin_server"] = r.headers.get("X-Archive-Orig-Server")
            getattr(r, "close", lambda: None)()
        except Exception as exc:  # noqa: BLE001
            out["reason"] = f"replay-error: {exc!r}"[:200]
            if attempt < WITNESS_ATTEMPTS - 1:
                time.sleep(WITNESS_POLL_S)
            continue
        if memento:
            out["memento_datetime"] = memento
            try:
                md = parsedate_to_datetime(memento)
                if md.tzinfo is None:      # "-0000" parses naive; Wayback means UTC
                    md = md.replace(tzinfo=timezone.utc)
                fresh = md >= t0 - timedelta(seconds=WITNESS_FRESH_SLACK_S)
            except Exception:  # noqa: BLE001 — a header that cannot be evaluated
                out["reason"] = "memento-unparsable"   # re-polling cannot fix it
                return out
            if fresh:
                out["status"] = status
                if status == 200:
                    out["saw"], out["reason"] = "live", None
                elif status in (404, 410):
                    out["saw"], out["reason"] = "absent", None
                else:
                    out["reason"] = f"replay-status-{status}"
                return out
            out["reason"] = "stale-snapshot"      # an older capture was served
        else:
            out["reason"] = "not-replayable"      # capture not (yet) available
        if attempt < WITNESS_ATTEMPTS - 1:
            time.sleep(WITNESS_POLL_S)
    return out


def ots_stamp(digest: bytes):
    """Stamp a SHA-256 digest against OpenTimestamps calendars.

    Returns (ots_bytes or None, outcome dict). Fresh proofs carry pending calendar
    attestations; the calendars anchor them in bitcoin hours later. upgrade_ots.py
    upgrades stored proofs once the anchor exists.
    """
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import (BytesDeserializationContext,
                                               BytesSerializationContext)
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

    ts = Timestamp(digest)
    attested, errors = [], []
    for cal in OTS_CALENDARS:
        try:
            r = requests.post(cal + "/digest", data=digest, timeout=30,
                              headers={"User-Agent": USER_AGENT,
                                       "Accept": "application/vnd.opentimestamps.v1"})
            r.raise_for_status()
            ts.merge(Timestamp.deserialize(BytesDeserializationContext(r.content), digest))
            attested.append(cal)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{cal}: {exc!r}")
    if not attested:
        return None, {"ok": False, "calendars": [], "errors": errors, "at": utc_now()}
    ctx = BytesSerializationContext()
    DetachedTimestampFile(OpSHA256(), ts).serialize(ctx)
    return ctx.getbytes(), {"ok": True, "calendars": attested, "errors": errors, "at": utc_now()}


class Store:
    """data/captures/<source-slug>/<target-slug>/<utc-ts>/ + state.json + events.jsonl"""

    def __init__(self, data_root: Path):
        self.root = Path(data_root)
        self.captures = self.root / "captures"
        self.state_path = self.root / "state.json"
        self.events_path = self.root / "events.jsonl"
        self.captures.mkdir(parents=True, exist_ok=True)
        self.state = (json.loads(self.state_path.read_text(encoding="utf-8"))
                      if self.state_path.exists() else {})

    def key(self, source_id: str, tslug: str) -> str:
        return f"{source_id}::{tslug}"

    def last_sha(self, source_id: str, tslug: str):
        return self.state.get(self.key(source_id, tslug), {}).get("last_sha256")

    def last_text_sha(self, source_id: str, tslug: str):
        return self.state.get(self.key(source_id, tslug), {}).get("last_text_sha256")

    def record_version(self, source_id: str, tslug: str, sha: str, cap_dir: str,
                       text_sha: str = None, managed: str = None) -> None:
        """managed: name of the extractor that owns this target (e.g. "meta_hub",
        "derived_targets") when its fetch URL is minted per-run and therefore not in
        the registry's target list — consumers (the site) must treat such entries as
        active document targets, not superseded ones."""
        entry = self.state.setdefault(self.key(source_id, tslug),
                                      {"versions": []})
        entry["last_sha256"] = sha
        # Always track the text hash of the current version, including None: a
        # document version with no extractable text must not inherit the previous
        # version's text hash (it would corrupt canonical-text dedupe).
        entry["last_text_sha256"] = text_sha
        if managed:
            entry["managed"] = managed
        entry["last_capture"] = cap_dir
        entry["versions"].append({"sha256": sha, "dir": cap_dir})

    def event(self, **kw) -> None:
        kw.setdefault("ts", utc_now())
        kw.setdefault("vantage", VANTAGE)
        with self.events_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(kw, ensure_ascii=False) + "\n")

    def save_state(self) -> None:
        atomic_write_text(self.state_path, json.dumps(self.state, indent=2))

    def capture_dir(self, source_id: str, tslug: str) -> Path:
        # Never reuse a directory: two versions minted within the same UTC second
        # (possible for fast targets) must not overwrite each other's evidence.
        base = self.captures / source_id.replace("/", "__") / tslug
        slug = ts_slug()
        d = base / slug
        n = 1
        while d.exists():
            d = base / f"{slug}-{n}"
            n += 1
        d.mkdir(parents=True, exist_ok=False)
        return d


def store_new_version(store: Store, *, source_id: str, provider: str, model: str,
                      kind: str, tslug: str, event_url: str, raw: bytes, meta: dict,
                      ext: str, text, notes: list, text_sha,
                      wayback_url: str = None, do_ots: bool = True,
                      extra_notes=(), managed: str = None,
                      event_extra: dict = None):
    """The single sanctioned write path for a NEW capture version.

    Callers own fetching, extension choice, text extraction, and dedupe (those
    genuinely differ per source family); this owns the storage ritual so it can
    never drift between callers again: raw bytes -> extracted.txt -> wayback
    (only when wayback_url given) -> OTS (unless do_ots=False) -> manifest
    (atomic) -> state -> "new" event. prior_sha256 is read BEFORE record_version.
    Returns (rel_capture_dir, manifest).
    """
    cap_dir = store.capture_dir(source_id, tslug)
    (cap_dir / f"raw{ext}").write_bytes(raw)
    if text:
        (cap_dir / "extracted.txt").write_text(text, encoding="utf-8", newline="\n")
    sha = sha256_hex(raw)
    manifest = {
        "source_id": source_id, "provider": provider, "model": model,
        "target_kind": kind, "sha256": sha, "size_bytes": len(raw),
        "stored_as": f"raw{ext}", "text_sha256": text_sha,
        "extraction_notes": list(notes) + list(extra_notes),
        "http": meta,
        "prior_sha256": store.last_sha(source_id, tslug),
    }
    if wayback_url is not None:
        manifest["wayback"] = wayback_save(wayback_url)
    if do_ots:
        ots_bytes, ots_meta = ots_stamp(bytes.fromhex(sha))
        manifest["ots"] = ots_meta
        if ots_bytes:
            (cap_dir / f"raw{ext}.ots").write_bytes(ots_bytes)
    atomic_write_text(cap_dir / "manifest.json",
                      json.dumps(manifest, indent=2, ensure_ascii=False))
    rel = cap_dir.relative_to(store.root).as_posix()
    store.record_version(source_id, tslug, sha, rel, text_sha=text_sha,
                         managed=managed)
    store.save_state()
    store.event(source=source_id, target=tslug, url=event_url, kind=kind,
                outcome="new", sha256=sha, dir=rel, **(event_extra or {}))
    return rel, manifest
