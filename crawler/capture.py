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
import socket
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from urllib.parse import urljoin, urlparse

USER_AGENT = ("GPAI-Ledger/0.1 (public-interest archive of EU AI Act Article 53(1)(d) "
              "training-data summaries; contact: contact@gpailedger.com)")
HEADERS = {"User-Agent": USER_AGENT}
# Where an observation was made from. Absence claims are only as strong as their
# vantage point: a datacenter runner can see a 404 an origin never shows to others.
VANTAGE = "github-runner" if os.environ.get("GITHUB_ACTIONS") else "operator"
# Provider pages run untrusted scripts inside the renderer: keep Chromium's OS
# sandbox on (Playwright's default is off). Ubuntu 24.04 runners must allow
# unprivileged user namespaces first (ledger.yml does); set
# GPAI_CHROMIUM_SANDBOX=0 only on a host that cannot provide a sandbox.
CHROMIUM_SANDBOX = os.environ.get("GPAI_CHROMIUM_SANDBOX", "1") != "0"
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


# Which documents a page links to IS its content when the page is a tracker: the
# AIAL summary list pointed Muse Spark at Google's Gemini PDF for weeks, and the
# correction changed only an href — invisible to text extraction, so the daily
# sweep recorded "unchanged-content" every day for eleven days. Hashing the
# document links separately makes that a version without touching the extracted
# text (which the drift ledger compares, and which must not churn).
DOC_LINK_EXTS = (".pdf", ".docx", ".doc", ".zip", ".md", ".txt", ".json")


def doc_links_sha(raw: bytes):
    """sha256 over the sorted set of document links in an HTML page, or None."""
    from bs4 import BeautifulSoup
    try:
        soup = BeautifulSoup(raw, "html.parser")
    except Exception:  # noqa: BLE001
        return None
    links = set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        path = href.split("?", 1)[0].split("#", 1)[0].lower()
        if path.endswith(DOC_LINK_EXTS):
            links.add(href)
    if not links:
        return None
    return hashlib.sha256("\n".join(sorted(links)).encode("utf-8")).hexdigest()


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
MAX_REDIRECTS = 10
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
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
  const phrases = ['use cookies', 'uses cookies', 'value your privacy',
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

    def __reduce__(self):
        # crosses the worker-process boundary of fetch_rendered intact
        return (PermanentFetchError, (str(self), self.status_code, self.headers))


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


# Hostnames are resolved and every address checked: a name can point at a
# loopback, LAN or link-local address (deliberately, or via DNS rebinding). The
# offline test suite switches resolution off; production never does.
RESOLVE_HOSTS = True


def _is_public_ip(ip) -> bool:
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return bool(ip.is_global)


def _assert_public_http(url: str) -> None:
    """Refuse non-http(s) schemes and hosts that are — or resolve to — private,
    loopback, link-local or otherwise non-public addresses. Registry URLs are
    refreshed daily from third-party metadata and mined URLs come out of rendered
    third-party DOMs: never let one point the crawler at file:, javascript:, or an
    internal address. Callers apply it to the request URL, every redirect hop and
    the final URL."""
    import ipaddress
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise RuntimeError(f"refusing non-http(s) URL scheme: {url[:120]}")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise RuntimeError(f"refusing URL without a host: {url[:120]}")
    if host == "localhost" or host.endswith(".localhost"):
        raise RuntimeError(f"refusing loopback host {host} in {url[:120]}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not _is_public_ip(ip):
            raise RuntimeError(f"refusing non-public address {host} in {url[:120]}")
        return
    if all(re.fullmatch(r"0x[0-9a-f]+|[0-9]+", label) for label in host.split(".")):
        # numeric spellings (2130706433, 0x7f000001, 0177.0.0.1, 127.1) are
        # addresses the ipaddress module rejects but resolvers accept
        raise RuntimeError(f"refusing numeric host spelling {host} in {url[:120]}")
    if not RESOLVE_HOSTS:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return  # unresolvable: the fetch itself fails with its normal error
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not _is_public_ip(addr):
            raise RuntimeError(f"refusing host {host} resolving to non-public "
                               f"address {addr} in {url[:120]}")


def _is_public_url(url: str) -> bool:
    try:
        _assert_public_http(url)
        return True
    except RuntimeError:
        return False


def _get_following_public_redirects(url: str, headers: dict, timeout: int):
    """GET with redirects followed ONE HOP AT A TIME: the next location is checked
    against the public-address guard BEFORE it is requested (requests' own
    follower would already have sent the request and read the intermediate body),
    an intermediate answer's body is never read, and the conditional headers are
    dropped when a hop leaves the original host."""
    cur, hdrs = url, dict(headers)
    for _ in range(MAX_REDIRECTS + 1):
        r = requests.get(cur, headers=dict(hdrs), timeout=timeout, allow_redirects=False,
                         stream=True)
        loc = r.headers.get("Location") if r.status_code in REDIRECT_STATUSES else None
        if not loc:
            return r
        nxt = urljoin(cur, loc)
        r.close()
        try:
            _assert_public_http(nxt)
        except RuntimeError as exc:
            raise PermanentFetchError(f"redirect into a non-public address: {exc}")
        if urlparse(nxt).netloc != urlparse(cur).netloc:
            hdrs.pop("If-None-Match", None)
            hdrs.pop("If-Modified-Since", None)
        cur = nxt
    raise PermanentFetchError(f"more than {MAX_REDIRECTS} redirects for {url}")


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
            # a public origin may redirect into a private address: every hop is
            # guarded before it is requested, and the final URL once more
            r = _get_following_public_redirects(url, headers, timeout)
            try:
                _assert_public_http(getattr(r, "url", None) or url)
            except RuntimeError as exc:
                r.close()
                raise PermanentFetchError(f"redirect into a non-public address: {exc}")
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


# Capture kinds whose CONTENT belongs to someone other than the provider this
# ledger holds to account. AIAL's scored evaluations are their own research
# output, published with no licence granting redistribution: the ledger archives
# them so a revised grade stays recoverable, and publishes what it holds (size,
# canonical text hash, timestamp proof) without republishing their words.
# Lives here because BOTH the site and the drift analyser must honour it.
# Remove a kind only once the rights holder has said yes.
RESTRICTED_KINDS = {
    "aial-eval": "third-party research, archived but not republished here",
}


def kind_of_capture_dir(d) -> str:
    """The target kind a capture directory belongs to, read from its path
    (captures/<source>/<kind>-<hash8>/<timestamp>)."""
    parts = [p for p in str(d or "").replace(chr(92), "/").split("/") if p]
    return parts[-2].rsplit("-", 1)[0] if len(parts) >= 2 else ""


def guess_ext(content_type: str, url: str, raw: bytes = None) -> str:
    if (content_type or "").lower() in EXT_BY_TYPE:
        return EXT_BY_TYPE[content_type.lower()]
    tail = url.split("?")[0].lower()
    for ext in (".pdf", ".zip", ".html", ".md", ".txt", ".docx", ".doc", ".json",
                ".yaml", ".yml"):
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


def _timeout_from_env(name: str, default: float, hi: float) -> float:
    raw = os.environ.get(name, "")
    try:
        v = float(raw) if raw else default
    except ValueError:
        v = default
    return max(1.0, min(v, hi))


# Parsing is the one untrusted-input step that is CPU-bound rather than
# network-bound: a malformed PDF can send a parser into an unbounded loop, and an
# unattended sweep that hangs loses the day's evidence to the job timeout. PDF
# parsing therefore runs in a disposable worker process with a hard deadline.
EXTRACT_TIMEOUT_S = _timeout_from_env("GPAI_EXTRACT_TIMEOUT", 120.0, 900.0)


def _pdf_text_bounded(data: bytes, timeout: float = None, fn=None) -> str:
    """extract_pdf_text in a worker process that is killed at the deadline. The
    worker's own exception (encrypted, damaged file) propagates unchanged; a
    stall raises RuntimeError, so the caller records 'extraction failed' and
    keeps the bytes — text is derived, the document is the evidence."""
    import multiprocessing as mp
    timeout = EXTRACT_TIMEOUT_S if timeout is None else timeout
    ctx = mp.get_context("spawn")
    with ctx.Pool(1) as pool:
        res = pool.apply_async(fn or extract_pdf_text, (data,))
        try:
            return res.get(timeout=timeout)
        except mp.TimeoutError:
            pool.terminate()
            raise RuntimeError(f"pdf text extraction exceeded {timeout:.0f}s "
                               f"(parser stalled on the input; bytes stored, text omitted)")


def _bounded_zip_members(zf):
    """Yield (info, bytes) for every member of an open ZipFile under the zip-bomb
    caps: member count, advertised per-member and total sizes, and a bounded read
    with a cumulative counter (a hostile archive can lie about its sizes). Sorted
    by filename: entry order is nondeterministic for on-demand-generated ZIPs and
    the member list doubles as a content key."""
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
        yield info, inner


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
            return _finish(normalize(_pdf_text_bounded(data)))
        if ext == ".html":
            return normalize(extract_html_text(data)), notes
        # .yaml/.yml are read as text but are deliberately NOT in the site's
        # DOC_SUFFIXES: an AIAL evaluation is evidence about a summary, never the
        # summary itself, and must never be counted as a document version
        if ext in (".md", ".txt", ".json", ".yaml", ".yml"):
            return normalize(data.decode("utf-8", errors="replace")), notes
        if ext == ".zip":
            # Zip-bomb guards live in _bounded_zip_members (shared with the
            # Art. 53 scope filter so the two can never drift apart).
            parts = []
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info, inner in _bounded_zip_members(zf):
                    notes.append({"inner_file": info.filename, "inner_sha256": sha256_hex(inner)})
                    if info.filename.lower().endswith(".pdf"):
                        # One encrypted/damaged member must never abort the archive:
                        # its bytes are hashed above; only its text is omitted.
                        try:
                            parts.append(f"===== {info.filename} =====\n"
                                         + _pdf_text_bounded(inner))
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


# A page can keep the renderer's main thread busy forever after the load event
# (Playwright's content()/count()/evaluate() have no timeout), so the whole
# browser session runs in a disposable worker process with a hard deadline —
# the browser is launched inside it and dies with it. 15 rendered targets at
# the default deadline still fit inside the sweep budget.
RENDER_TIMEOUT_S = _timeout_from_env("GPAI_RENDER_TIMEOUT", 300.0, 1800.0)


def fetch_rendered(url: str, timeout_ms: int = 60000, max_expand_clicks: int = 30,
                   deadline: float = None, fn=None):
    """fetch_rendered's body (_fetch_rendered_impl) in a worker process that is
    killed at the deadline; a stall raises RuntimeError so the caller records a
    plain fetch error and the sweep moves on. Returns (dom_bytes, meta)."""
    import multiprocessing as mp
    deadline = RENDER_TIMEOUT_S if deadline is None else deadline
    ctx = mp.get_context("spawn")
    with ctx.Pool(1) as pool:
        res = pool.apply_async(fn or _fetch_rendered_impl, (url, timeout_ms, max_expand_clicks))
        try:
            return res.get(timeout=deadline)
        except mp.TimeoutError:
            pool.terminate()
            raise RuntimeError(f"rendered fetch exceeded {deadline:.0f}s (the page kept the "
                               f"renderer busy; nothing stored for {url})")


def _fetch_rendered_impl(url: str, timeout_ms: int = 60000, max_expand_clicks: int = 30):
    """Fetch a JS-heavy page with headless Chromium and return the rendered DOM.

    Returns (dom_bytes, meta). The stored artifact is the serialized DOM after JS
    execution — a DERIVED rendering, not origin bytes; manifests mark it as such.
    After load, a generic disclosure pass clicks elements with aria-expanded="false"
    (accordion/collapsible content) so lazy-revealed text enters the DOM. Clicks are
    same-page only (buttons), capped, and errors are non-fatal.
    """
    from playwright.sync_api import sync_playwright

    _assert_public_http(url)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, chromium_sandbox=CHROMIUM_SANDBOX)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            # every request the page makes — navigations, sub-resources, frames —
            # must pass the public-address guard: a provider page must not be
            # able to make the runner fetch a private address on its behalf
            page.route("**/*", lambda route, request: (
                route.continue_() if _is_public_url(request.url) else route.abort()))
            resp = page.goto(url, wait_until="load", timeout=timeout_ms)
            try:
                _assert_public_http(page.url)
            except RuntimeError as exc:
                raise PermanentFetchError(f"navigation ended at a non-public address: {exc}")
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
                          "Reject optional", "Reject all", "Only essential", "Decline"):
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
                            and (f.url or "").startswith("http")
                            and _is_public_url(f.url)]
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
                if not _is_public_url(fr.url or ""):
                    continue
                try:
                    fhtml = fr.content()
                    ftext, _ = extract_text(fhtml.encode("utf-8"), ".html")
                    if ftext and len(ftext.split()) >= 50:
                        frame_sections.append(
                            f"\n<!-- gpai-ledger:frame {fr.url} -->\n" + fhtml)
                        frame_urls.append(fr.url)
                except Exception:
                    continue

            # the page may have navigated after goto (a JS/meta redirect, a click):
            # re-validate where it ended before anything is serialized
            try:
                _assert_public_http(page.url)
            except RuntimeError as exc:
                raise PermanentFetchError(f"page navigated to a non-public address: {exc}")
            dom = (page.content() + "".join(frame_sections)).encode("utf-8")
            if len(dom) > MAX_FETCH_BYTES:
                raise PermanentFetchError(f"rendered DOM {len(dom)} bytes exceeds cap for {url}")
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
        # same zip-bomb caps as extract_text: this filter runs FIRST on a fetched
        # bundle, so it must never inflate an unbounded member into memory
        for info, data in _bounded_zip_members(zin):
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


# Save Page Now does not always crawl anew: it can answer with an existing
# capture. One made shortly before our fetch still witnesses the same content;
# one made days or months earlier witnesses the page's earlier state and is no
# corroboration of this capture at all (the corpus held snapshots up to 222
# days older than the version they were attached to). Anything older than this
# slack is recorded but marked `fresh: false`, and retry_wayback keeps trying
# for a real one.
WAYBACK_FRESH_SLACK_S = 3600


def snapshot_parts(snapshot: str):
    """(14-digit capture timestamp, archived URL) of a Wayback snapshot URL."""
    m = re.match(r"https://web\.archive\.org/web/(\d{14})(?:id_|if_|im_)?/(.+)$",
                 snapshot or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def _same_url(a: str, b: str) -> bool:
    """Whether two URLs address the same resource for our purposes: percent
    encoding and a trailing slash must not read as a different address."""
    from urllib.parse import unquote

    def norm(u):
        return unquote(str(u or "")).rstrip("/")
    return norm(a) == norm(b)


def snapshot_is_fresh(snapshot: str, requested_at: str):
    """True when the snapshot was captured no earlier than WAYBACK_FRESH_SLACK_S
    before the save was requested — i.e. it witnesses what we just fetched.
    None when either timestamp is unreadable."""
    ts, _ = snapshot_parts(snapshot)
    if not ts or not requested_at:
        return None
    try:
        snap_dt = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        asked_dt = datetime.strptime(requested_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    return (asked_dt - snap_dt).total_seconds() <= WAYBACK_FRESH_SLACK_S


# Save Page Now requires an account for its API ("You need to be logged in to
# use Save Page Now"), and the anonymous /save/<url> path is rate-limited per
# IP — a GitHub runner's datacenter address is throttled to the point of dropped
# connections, which is why CI archived nothing while the same URLs saved fine
# from a residential network. With credentials the SPN2 API is used instead:
# POST a job, poll until it names a capture. Without them nothing changes.
SPN2_ENDPOINT = "https://web.archive.org/save"
SPN2_POLL_S = 5
SPN2_POLL_DEADLINE_S = 180
# If the Archive already holds a capture younger than this it may return that
# one instead of crawling again. Kept well inside WAYBACK_FRESH_SLACK_S so a
# reused capture still witnesses what we just fetched.
SPN2_REUSE_WITHIN = "30m"


def ia_auth():
    """The Authorization header value for Save Page Now, or None when no
    archive.org credentials are configured. The keys are read from the
    environment and never logged."""
    access = os.environ.get("GPAI_IA_ACCESS_KEY", "").strip()
    secret = os.environ.get("GPAI_IA_SECRET_KEY", "").strip()
    return f"LOW {access}:{secret}" if access and secret else None


def _spn2_json(resp):
    try:
        return resp.json()
    except Exception:  # noqa: BLE001 — an HTML error page, not JSON
        return {}


def _wayback_save_spn2(url: str, auth: str, timeout: int) -> dict:
    """Authenticated Save Page Now: submit a capture job and poll for the
    capture it assigns. `answered` marks a verdict about the URL itself (as
    opposed to the Archive dropping us), so callers can tell the two apart."""
    requested_at = utc_now()
    headers = dict(HEADERS)
    headers.update({"Accept": "application/json", "Authorization": auth})
    try:
        r = requests.post(SPN2_ENDPOINT, headers=headers, timeout=timeout,
                          data={"url": url, "skip_first_archive": "1",
                                "if_not_archived_within": SPN2_REUSE_WITHIN})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc), "via": "spn2",
                "requested_at": requested_at, "at": utc_now()}
    body = _spn2_json(r)
    job_id = body.get("job_id")
    if not job_id:
        # no job: either a verdict about the URL, or the Archive refusing us
        answered = r.status_code == 200 and bool(body.get("message"))
        return {"ok": False, "status_code": r.status_code, "spn_status": r.status_code,
                "error": str(body.get("message") or f"HTTP {r.status_code}")[:300],
                "answered": answered, "via": "spn2",
                "requested_at": requested_at, "at": utc_now()}

    deadline = time.monotonic() + SPN2_POLL_DEADLINE_S
    status = {}
    while time.monotonic() < deadline:
        time.sleep(SPN2_POLL_S)
        try:
            s = requests.get(f"{SPN2_ENDPOINT}/status/{job_id}", headers=headers,
                             timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": repr(exc), "job_id": job_id, "via": "spn2",
                    "requested_at": requested_at, "at": utc_now()}
        status = _spn2_json(s)
        if status.get("status") in ("success", "error"):
            break
    if status.get("status") == "success" and status.get("timestamp"):
        original = status.get("original_url") or url
        snapshot = f"https://web.archive.org/web/{status['timestamp']}/{original}"
        return {"ok": True, "status_code": 200, "spn_status": 200,
                "snapshot": snapshot, "snapshot_ts": status["timestamp"],
                "fresh": snapshot_is_fresh(snapshot, requested_at),
                "same_url": _same_url(original, url),
                "job_id": job_id, "via": "spn2",
                "requested_at": requested_at, "at": utc_now()}
    if status.get("status") == "error":
        return {"ok": False, "status_code": 200, "spn_status": 200,
                "error": str(status.get("status_ext") or status.get("message")
                             or "spn2 error")[:300],
                "answered": True, "job_id": job_id, "via": "spn2",
                "requested_at": requested_at, "at": utc_now()}
    return {"ok": False, "error": f"spn2 job {job_id} unfinished after "
                                  f"{SPN2_POLL_DEADLINE_S}s", "job_id": job_id,
            "via": "spn2", "requested_at": requested_at, "at": utc_now()}


def wayback_save(url: str, timeout: int = 120):
    """Best-effort triggered Wayback save. Returns outcome dict, never raises.

    A recorded snapshot URL means Save Page Now accepted the request; durable
    presence in the public index is only established by a later CDX check.
    `fresh` says whether the capture SPN named is new enough to witness the
    document we just fetched (see WAYBACK_FRESH_SLACK_S); `same_url` says
    whether it archives the address we asked for or a redirect target.

    With archive.org credentials in the environment the authenticated SPN2 API
    is used instead (see _wayback_save_spn2), which is the only path that works
    from a datacenter address.
    """
    auth = ia_auth()
    if auth:
        return _wayback_save_spn2(url, auth, timeout)
    requested_at = utc_now()
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
        snap_ts, snap_url = snapshot_parts(snapshot)
        out = {"ok": snapshot is not None, "status_code": r.status_code,
               "spn_status": spn_status, "snapshot": snapshot,
               "requested_at": requested_at, "at": utc_now()}
        if snapshot:
            out["fresh"] = snapshot_is_fresh(snapshot, requested_at)
            out["snapshot_ts"] = snap_ts
            # SPN follows redirects: the capture may archive the redirect target
            # (a CDN or signed URL), which is evidence about that address, not
            # about the one we track
            out["same_url"] = _same_url(snap_url, url) if snap_url else None
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc), "requested_at": requested_at,
                "at": utc_now()}


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

    def known_text_shas(self, source_id: str, tslug: str) -> dict:
        """text_sha256 -> capture dir for every retained version of a target,
        read from the manifests: a rendered page that flips between two chrome
        states (a consent banner stripped on one run and not the next) must
        not mint a version on every flip."""
        out = {}
        for v in self.state.get(self.key(source_id, tslug), {}).get("versions", []):
            mp = self.root / v["dir"] / "manifest.json"
            try:
                t = json.loads(mp.read_text(encoding="utf-8")).get("text_sha256")
            except (OSError, ValueError):
                continue
            if t:
                out[t] = v["dir"]
        return out

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
        **({"doc_links_sha256": doc_links_sha(raw)} if ext == ".html" else {}),
        "extraction_notes": list(notes) + list(extra_notes),
        "http": meta,
        "prior_sha256": store.last_sha(source_id, tslug),
    }
    if ext == ".zip" and text:
        # for bundles text_sha256 is the inner-member hash key, so the served
        # extracted.txt carries its own verifiable hash (verify_corpus C5)
        manifest["extracted_text_sha256"] = canonical_text_sha(text)
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
