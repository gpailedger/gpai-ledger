"""Static site generator for the GPAI Ledger.

Permalink contract (stable forever, versioned from day 1):
  /                                         index of all tracked sources
  /ledger/<provider>/<model>/               model page: capture history by class
  /ledger/<provider>/<model>/v/<capture>/   per-version permalink (provenance,
                                            stored file + .ots proof, text)
  /status/ /changes/ /methodology/ /about/  reference pages
  /what-is-a-training-data-summary/ /deadlines/ /dataset/ /corrections/ /privacy/

Reader-lens rules this generator enforces (see reports and site/lint.py):
- capture classes are explicit: document versions / watch-surface captures /
  captures of superseded target URLs — never one flat "versions" list
- provenance must be demonstrable from the page that asserts it: the stored bytes
  and the OpenTimestamps proof are served from the content-addressed /blob/ store
  (filename = SHA-256) and linked from each version page
- extractor-managed targets (rotating URLs; state entries carry "managed") are
  active document targets, not superseded ones
- operational annotations stay in manifests; only reader-safe notes render

SEO layer (research-derived, see reports/2026-08-20 research):
- absolute self-referencing canonicals + sitemap.xml with real lastmod when
  GPAI_SITE_URL is set; robots.txt disallows only /blob/
- watch-surface version permalinks are noindex (thin); byte-identical duplicate
  captures canonicalize to the first capture (never noindex+canonical together)
- JSON-LD: WebSite+Organization on the index, Dataset on /dataset/,
  BreadcrumbList on model/version pages; no FAQPage/SearchAction (retired)
- self-hosted Source Serif 4 (GDPR: no third-party font/asset requests)
"""
import html
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DIST = Path(__file__).resolve().parent / "dist"
STATIC = Path(__file__).resolve().parent / "static"

sys.path.insert(0, str(ROOT / "crawler"))
import capture as cap  # noqa: E402 — target_slug for active-target detection

DOC_SUFFIXES = (".pdf", ".zip", ".md", ".txt", ".docx", ".doc", ".xml")
APP_SHELL_TEXT_BYTES = 600  # extracted text below this on plain HTML = JS app shell
EXTRACT_DISPLAY_LIMIT = 200_000
# the public contact address; overridden via env once the custom-domain mailbox
# exists (GPAI_CONTACT_EMAIL=contact@gpailedger.com)
CONTACT_EMAIL = os.environ.get("GPAI_CONTACT_EMAIL", "contact@gpailedger.com")
# absolute site origin for canonical URLs / sitemap / social cards, no trailing
# slash (e.g. https://gpailedger.com); empty = no absolute-URL features
SITE_URL = os.environ.get("GPAI_SITE_URL", "").rstrip("/")
# when set, a CNAME file for GitHub Pages custom-domain routing is emitted
CUSTOM_DOMAIN = os.environ.get("GPAI_CUSTOM_DOMAIN", "")
# path prefix for internal links: "/" on the custom domain, "/<repo>/" on a
# GitHub project page
PREFIX = os.environ.get("GPAI_SITE_ROOT", "/")
if not PREFIX.endswith("/"):
    PREFIX += "/"
# a mis-set prefix would bake garbage into every URL on the site (Git Bash on
# Windows, for instance, rewrites a bare "/" into a filesystem path) — refuse
# to build rather than publish broken links
if not re.match(r"^/[A-Za-z0-9._~/-]*$", PREFIX) or "//" in PREFIX:
    raise SystemExit(f"GPAI_SITE_ROOT is not a valid URL path prefix: {PREFIX!r}")
REPO_URL = os.environ.get("GPAI_REPO_URL", "")

KIND_LABELS = {
    "provider-live": "provider site",
    "provider-page": "provider page",
    "aial-archive": "AIAL archived copy",
    "cop-doc": "Code of Practice doc (Art. 53(1)(a)–(c))",
    "regulatory": "official document",
    "watch-page": "watched page",
    "aial-eval": "AIAL evaluation",
    "aial-eval-history": "AIAL evaluation (earlier state)",
    "aial-eval-page": "AIAL evaluation page",
    "aial-method": "AIAL scoring framework",
}

# A third party's assessment of a summary, in any of the forms they publish it:
# the scored file, its earlier states, and the page that renders them. Evidence
# ABOUT a document, never the document — so never a document version anywhere.
THIRD_PARTY_EVAL_KINDS = ("aial-eval", "aial-eval-history", "aial-eval-page")
# the framework those assessments are made with; belongs to AIAL's own source
THIRD_PARTY_METHOD_KINDS = ("aial-method",)
# kinds that ARE the provider's own document: an absence on one of these is a
# statement about the provider, and the page's tense has to follow it
PROVIDER_DOC_KINDS = ("provider-live", "provider-page")

# Bumped BY HAND when a person actually re-reads the legal explainer against the
# current text of the Regulation. Deriving it from the build date asserted a
# review on every deploy that nobody had performed.
LEGAL_TEXT_REVIEWED = "2026-08-22"

STATUS_LABELS = {"published": "Published", "missing": "Missing",
                 "regulatory": "Regulatory", "watch": "Watch"}

# Notes carrying internal/operational language never render on public pages;
# everything else in extraction_notes (strings only) is shown.
INTERNAL_NOTE_MARKERS = ("text_sha256 migrated", "repaired 2026", "stable key =",
                         "stable CDN path")

ZIP_ART53_FILE = re.compile(r"training.?data|data.?summary", re.I)
ZIP_NOT_ART53 = re.compile(r"AB.?2013", re.I)   # California disclosure, not EU Art. 53


def zip_file_label(name: str) -> str:
    if ZIP_NOT_ART53.search(name):
        return "California AB 2013 disclosure (not Art. 53)"
    if ZIP_ART53_FILE.search(name):
        return "Art. 53 summary"
    return ""


# ---------------------------------------------------------------------------
# Design system: GOV.UK-derived type scale, WCAG-AA-verified ink/steel tokens,
# self-hosted Source Serif 4 for headings only, system serif body, system sans
# data UI. Dark mode = token redefinition under prefers-color-scheme. No
# gradients, no shadows, no icon boxes, no third-party requests.
# ---------------------------------------------------------------------------
CSS = """
@font-face{font-family:'Source Serif 4';src:url('{PREFIX}static/source-serif-4-latin.woff2') format('woff2');
 font-weight:100 900;font-style:normal;font-display:swap;}
:root{--bg:#ffffff;--surface:#f5f7f9;--text:#1a1d21;--muted:#56606b;--border:#d8dde3;
 --accent:#0f5c8c;--accent-strong:#0b4a73;--band:#1a1d21;--band-text:#ffffff;
 --ok-t:#0f6b3f;--ok-b:#e3f4ea;--bad-t:#a12622;--bad-b:#fbe9e9;
 --reg-t:#0b4a73;--reg-b:#e4eef5;--watch-t:#8a5a00;--watch-b:#fdf3d9;
 --sans:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
 --serif:Georgia,'Times New Roman',serif;
 --mono:ui-monospace,'Cascadia Code','SF Mono',Menlo,Consolas,'Liberation Mono',monospace;}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--surface:#1c2126;--text:#e6e9ec;
 --muted:#9aa4ae;--border:#313941;--accent:#7db2e0;--accent-strong:#a3c9ea;
 --band:#101316;--band-text:#e6e9ec;--ok-t:#6fce9e;--ok-b:#173226;--bad-t:#f1948e;
 --bad-b:#3a1d1b;--reg-t:#9cc4e4;--reg-b:#182b3a;--watch-t:#e0b566;--watch-b:#332a14;}}
*{box-sizing:border-box}
html{font-size:16px}
body{margin:0;background:var(--bg);color:var(--text);font:1.1875rem/1.32 var(--serif);}
h1,h2,h3{font-family:'Source Serif 4',Georgia,serif;font-weight:700;line-height:1.11;
 letter-spacing:-0.01em;margin:1.6em 0 .5em}
h1{font-size:2.25rem;margin-top:.9em}h2{font-size:1.5rem}h3{font-size:1.1875rem}
@media (max-width:640px){h1{font-size:1.6875rem}h2{font-size:1.3125rem}}
p,ul,ol,dl{max-width:68ch}
a{color:var(--accent);text-underline-offset:2px}
a:hover{color:var(--accent-strong)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
code{font-family:var(--mono);font-size:.85em;overflow-wrap:anywhere}
pre{font-family:var(--mono);font-size:.8em;line-height:1.45;overflow-x:auto;
 background:var(--surface);border:1px solid var(--border);padding:1rem;max-width:100%}
.muted{color:var(--muted)}
.skip{position:absolute;left:-999px;top:0;background:var(--bg);color:var(--accent);
 padding:.5rem 1rem;z-index:9}
.skip:focus{left:0}
.band{background:var(--band);color:var(--band-text);font-family:var(--sans)}
.band-inner{max-width:1100px;margin:0 auto;padding:.9rem 1rem;display:flex;
 flex-wrap:wrap;align-items:baseline;gap:.4rem 1.5rem}
.wordmark{font-family:'Source Serif 4',Georgia,serif;font-weight:700;font-size:1.35rem;
 color:var(--band-text);text-decoration:none;letter-spacing:-0.01em}
.tagline{font-size:.85rem;color:#9aa4ae}
nav.site{max-width:1100px;margin:0 auto;padding:.45rem 1rem;font-family:var(--sans);
 font-size:.9375rem;display:flex;flex-wrap:wrap;gap:1.25rem;border-bottom:1px solid var(--border)}
nav.site a{text-decoration:none;color:var(--text)}
nav.site a:hover{color:var(--accent);text-decoration:underline}
main{max-width:1100px;margin:0 auto;padding:0 1rem 3rem}
.crumbs{font-family:var(--sans);font-size:.875rem;color:var(--muted);margin:1rem 0 0;
 max-width:none}
.crumbs a{color:var(--muted)}
.subtitle{font-family:var(--sans);font-size:.9375rem;color:var(--muted);margin-top:.25rem}
.statstrip{display:flex;flex-wrap:wrap;gap:0;margin:1.5rem 0 .5rem;padding:0;
 border-top:1px solid var(--border);border-bottom:1px solid var(--border);max-width:none}
.statstrip div{padding:.9rem 1.75rem .9rem 0;margin-right:1.75rem;
 border-right:1px solid var(--border)}
.statstrip div:last-child{border-right:0}
.statstrip dt{font-family:var(--sans);font-size:.8125rem;color:var(--muted);margin:0}
.statstrip dd{font-family:var(--sans);font-size:1.5rem;font-weight:600;margin:0;
 font-variant-numeric:tabular-nums}
.tablewrap{overflow-x:auto;max-width:100%}
table{border-collapse:collapse;width:100%;min-width:640px;font-family:var(--sans);
 font-size:.9375rem;line-height:1.35}
td{padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:top;
 text-align:left}
th{font-size:.875rem;font-weight:600;text-align:left;color:var(--muted);
 border-bottom:2px solid var(--text);padding:8px 12px;background:var(--bg);
 position:sticky;top:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
caption{position:absolute;left:-999px}
.tag{font-family:var(--sans);font-size:.8125rem;font-weight:600;padding:2px 8px;
 white-space:nowrap}
.tag-published{color:var(--ok-t);background:var(--ok-b)}
.tag-missing{color:var(--bad-t);background:var(--bad-b)}
.tag-regulatory{color:var(--reg-t);background:var(--reg-b)}
.tag-watch{color:var(--watch-t);background:var(--watch-b)}
footer.site{border-top:1px solid var(--border);margin-top:2rem;font-family:var(--sans);
 font-size:.875rem;color:var(--muted)}
footer.site .inner{max-width:1100px;margin:0 auto;padding:1.5rem 1rem 2.5rem;
 display:flex;flex-wrap:wrap;gap:2.5rem}
footer.site ul{list-style:none;margin:0;padding:0}
footer.site li{margin:.25rem 0}
footer.site a{color:var(--muted)}
@media print{.band,nav.site,footer.site,.skip{display:none}
 body{font-size:11pt;color:#000;background:#fff}
 a{color:#000;text-decoration:none}
 pre{border:1px solid #999;overflow:visible;white-space:pre-wrap}}
""".replace("{PREFIX}", "%%PREFIX%%")


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{meta}<style>{css}</style></head><body>
<a class="skip" href="#main">Skip to content</a>
<header class="band"><div class="band-inner">
<a class="wordmark" href="{p}">GPAI Ledger</a>
<span class="tagline">The public record of EU AI Act training-data summaries</span>
</div></header>
<nav class="site" aria-label="Site">
<a href="{p}">Models</a>
<a href="{p}status/">Status</a>
<a href="{p}changes/">Changes</a>
<a href="{p}methodology/">Methodology</a>
<a href="{p}about/">About</a>
</nav>
<main id="main">
{crumbs}{body}
</main>
<footer class="site"><div class="inner">
<ul>
<li><strong>GPAI Ledger</strong> — independent public-interest archive</li>
<li>Independently operated · <a href="mailto:{contact}">{contact}</a></li>
<li>Metadata, hashes and the event log: CC0. Archived documents remain their publishers' property.</li>
</ul>
<ul>
<li><a href="{p}what-is-a-training-data-summary/">What is a training-data summary?</a></li>
<li><a href="{p}deadlines/">Deadlines</a></li>
<li><a href="{p}dataset/">Dataset (JSON)</a></li>
<li><a href="{p}corrections/">Corrections</a></li>
<li><a href="{p}privacy/">Privacy</a></li>
{repo_li}</ul>
<ul>
<li>{stamp}</li>
<li><a href="{p}methodology/">How to verify a capture</a></li>
</ul>
</div></footer>
</body></html>"""

GENERATED = None  # stamped by main(); tests may pass a fixed value
BUILD_STAMP = ""  # human build line for the footer, set by main()


def esc(s):
    return html.escape(str(s or ""))


def url_attr(s):
    """Escape a URL for an href/src, neutralizing dangerous schemes (javascript:,
    data:) that html.escape leaves clickable. Provider-controlled URLs land here."""
    u = str(s or "").strip()
    if u.startswith("//"):
        return "#"  # protocol-relative: an off-site destination in disguise
    if u.startswith(("https://", "http://", "/", "#", "mailto:")):
        return html.escape(u)
    return "#"


def mask_tokens(u: str) -> str:
    """Mask token/signature query values in DISPLAYED urls (stored data unchanged)."""
    return re.sub(r"([?&](?:r|X-Amz-Signature|X-Amz-Credential|Signature|Policy|"
                  r"Key-Pair-Id|oh|oe|ccb|_nc_gid|_nc_ohc|_nc_cat|_nc_sid|_nc_ad|"
                  r"_nc_cid|_nc_oc|efg|msockid)=)[^&\s]+", r"\1…", u)


def short_url(u: str) -> str:
    """Middle-truncate, preserving the filename tail that distinguishes URLs."""
    u2 = mask_tokens(u.split("://", 1)[-1])
    if len(u2) <= 72:
        return u2
    return u2[:40] + "…" + u2[-30:]


SIGNED_URL = re.compile(r"[?&](?:r|X-Amz-Signature|Signature|Policy|oh|_nc_gid|msockid)=")


def target_cell(kind_label: str, url: str) -> str:
    """The Target row: signed/tokened URLs are shown masked and NOT hyperlinked —
    publishing a live click-through token is not required for provenance (the URL
    string itself is the evidence)."""
    if SIGNED_URL.search(url):
        return (f"{esc(kind_label)} — <code>{esc(mask_tokens(url))}</code> "
                f"<span class='muted'>(signed URL; token masked, not linked)</span>")
    return f"{esc(kind_label)} — <a href='{url_attr(url)}'>{esc(mask_tokens(url))}</a>"


def head_meta(desc: str = "", canonical_path: str = None, robots: str = None,
              jsonld: list = None, og_title: str = None) -> str:
    """The SEO/head block: description, canonical, robots, favicons, og tags,
    feed autodiscovery, JSON-LD. Absolute-URL features appear only when
    GPAI_SITE_URL is configured."""
    out = []
    if desc:
        out.append(f'<meta name="description" content="{esc(desc)}">')
    if robots:
        out.append(f'<meta name="robots" content="{esc(robots)}">')
    if SITE_URL and canonical_path is not None:
        out.append(f'<link rel="canonical" href="{esc(SITE_URL + canonical_path)}">')
    out.append(f'<link rel="icon" href="{PREFIX}static/favicon-48.png" sizes="48x48">')
    out.append(f'<link rel="icon" href="{PREFIX}static/favicon-96.png" sizes="96x96">')
    out.append(f'<link rel="apple-touch-icon" href="{PREFIX}static/apple-touch-icon.png">')
    out.append('<meta name="theme-color" content="#1a1d21">')
    out.append('<meta property="og:site_name" content="GPAI Ledger">')
    if og_title:
        out.append(f'<meta property="og:title" content="{esc(og_title)}">')
    if desc:
        out.append(f'<meta property="og:description" content="{esc(desc)}">')
    if SITE_URL:
        out.append(f'<meta property="og:image" content="{SITE_URL}{PREFIX}static/og-card.png">')
        if canonical_path is not None:
            out.append(f'<meta property="og:url" content="{esc(SITE_URL + canonical_path)}">')
        out.append('<meta name="twitter:card" content="summary_large_image">')
        out.append(f'<link rel="alternate" type="application/atom+xml" '
                   f'title="GPAI Ledger — changes" href="{SITE_URL}{PREFIX}changes/atom.xml">')
    for block in (jsonld or []):
        # JSON inside <script> must never be able to close the element: '<', '>'
        # and '&' become JSON \u escapes (still valid JSON, identical once parsed)
        payload = (json.dumps(block, ensure_ascii=False)
                   .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
        out.append('<script type="application/ld+json">' + payload + '</script>')
    return "\n".join(out) + "\n"


def crumbs_html(items) -> str:
    """items: [(label, path-or-None)]; the last item is the current page."""
    if not items:
        return ""
    parts = []
    for label, path in items:
        if path is not None:
            parts.append(f"<a href='{esc(path)}'>{esc(label)}</a>")
        else:
            parts.append(esc(label))
    return ("<p class='crumbs'>" + " › ".join(parts) + "</p>\n")


def breadcrumb_jsonld(items) -> dict:
    """Schema.org BreadcrumbList; last element carries no item URL (Google infers
    the current page)."""
    els = []
    for i, (label, path) in enumerate(items, 1):
        el = {"@type": "ListItem", "position": i, "name": label}
        if path is not None and SITE_URL:
            el["item"] = SITE_URL + path
        els.append(el)
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": els}


def write(path: Path, title: str, body: str, desc: str = "",
          canonical_path: str = None, robots: str = None, jsonld: list = None,
          crumbs: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    repo_li = (f'<li><a href="{esc(REPO_URL)}">Code &amp; data (GitHub)</a></li>'
               if REPO_URL else "")
    path.write_text(PAGE.format(
        title=esc(title), body=body, p=PREFIX,
        css=CSS.replace("%%PREFIX%%", PREFIX),
        meta=head_meta(desc, canonical_path, robots, jsonld, og_title=title),
        contact=esc(CONTACT_EMAIL), repo_li=repo_li,
        stamp=esc(BUILD_STAMP), crumbs=crumbs), encoding="utf-8")


# hashes of full bundles replaced by an Art. 53 scope repack (scope-repack
# events); filled by main() before any version page renders
REPACKED_SHAS = set()
# sha256 of a pruned capture -> its pruned-noise event (tool-made events carry
# the hash; curation-time events carry only the dir, whose hash is in the
# matching 'new' event)
PRUNED_EVENTS = {}
# (source id, target) -> the current confirmed absence of a provider copy:
# {"first": ts of the first confirmation in this streak, "last": ts, "by": [vantages],
# "url": the address that stopped answering}. Filled by last_checked_map().
GONE_TARGETS = {}
# sha256 -> (earliest capture slug, source id) across the whole corpus: a later
# capture of identical bytes points at the earliest attestation
SHA_FIRST = {}
# (source, target, from_dir, to_dir) -> analyze_drift's version-diffs ledger
# record: the common-extractor verdict on whether consecutive versions differ in
# text. Filled by main(); every "content changed" note and /changes/ entry for a
# pair that has a record comes from here, never from the stored text hashes.
VDIFFS = {}


def load_version_diffs() -> dict:
    p = ROOT / "reports" / "version-diffs.json"
    if not p.exists():
        return {}
    out = {}
    for rec in json.loads(p.read_text(encoding="utf-8")).values():
        if all(k in rec for k in ("source", "target", "from_dir", "to_dir", "verdict")):
            out[(rec["source"], rec["target"], rec["from_dir"], rec["to_dir"])] = rec
    return out


def last_checked_map():
    """source id -> latest event timestamp (proof-of-life for every page). Also
    collects REPACKED_SHAS from scope-repack events in the same pass."""
    out = {}
    p = DATA / "events.jsonl"
    if not p.exists():
        return out
    new_sha = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        src, ts = e.get("source"), e.get("ts", "")
        # a target the sweep deliberately skipped was not checked: a host being
        # down must never advance "Last checked" on a model page
        if src and ts and e.get("outcome") not in ("host-unreachable",
                                                   "host-unreachable-summary"):
            out[src] = max(out.get(src, ""), ts)
        if e.get("outcome") == "scope-repack" and e.get("prior_sha256"):
            REPACKED_SHAS.add(e["prior_sha256"])
        d = str(e.get("dir") or "").replace("\\", "/")
        if e.get("outcome") == "new" and d and e.get("sha256"):
            new_sha[d] = e["sha256"]
        if e.get("outcome") == "pruned-noise":
            sha = e.get("sha256") or new_sha.get(d)
            if sha:
                PRUNED_EVENTS[sha] = e
        _track_absence(e, src)
    return out


def _track_absence(e, src) -> None:
    """Maintain GONE_TARGETS over the event stream: a confirmed absence stands
    until the document is fetched again, seen live by a witness, or attested live
    from another network."""
    key = (src, e.get("target"))
    if not all(key):
        return
    outcome, absence = e.get("outcome"), e.get("absence")
    if (outcome in ("new", "unchanged", "unchanged-content", "recheck-recovered",
                    "live-attested") or absence == "contradicted"):
        GONE_TARGETS.pop(key, None)
    elif outcome == "error" and absence == "confirmed" and e.get("ts"):
        rec = GONE_TARGETS.setdefault(key, {"first": e["ts"], "by": [],
                                            "url": e.get("url"),
                                            "kind": e.get("kind")})
        rec["last"] = e["ts"]
        rec["url"] = rec["url"] or e.get("url")
        rec["kind"] = rec.get("kind") or e.get("kind")
        for v in e.get("confirmed_by") or []:
            if isinstance(v, str) and v not in rec["by"]:
                rec["by"].append(v)


# what a confirmed absence means depends on WHOSE copy stopped answering: the
# provider's own document, a third party's mirror of it, or a page we merely
# watch. Saying "the provider's copy" for all three would be a false statement
# about the provider.
GONE_WORDING = {
    "provider-live": "The provider's copy of this document no longer resolves.",
    "provider-page": "The provider page carrying this document no longer resolves.",
    "aial-archive": "The third-party archived copy of this document no longer "
                    "resolves (this is AIAL's mirror, not the provider's copy).",
    "cop-doc": "The archived Code of Practice document no longer resolves.",
    "regulatory": "The official document at this address no longer resolves.",
    "aial-eval": "The third-party evaluation of this summary is no longer "
                 "published at this address (this is AIAL's assessment, not the "
                 "provider's document).",
    "aial-eval-page": "AIAL's published evaluation page for this model no longer "
                      "resolves (this is their assessment, not the provider's "
                      "document).",
    "aial-eval-history": "This earlier state of the third-party evaluation is no "
                         "longer retrievable at its pinned address.",
    "aial-method": "This page of AIAL's scoring framework no longer resolves.",
}
GONE_WORDING_DEFAULT = "A page this project monitors no longer resolves."


def gone_corroboration(by) -> str:
    """How a confirmed absence was corroborated — naming only the vantages that
    actually did so. An unrecognised or empty set claims nothing."""
    s = {v for v in (by or []) if isinstance(v, str)}
    archive = "an independent Internet Archive capture" if "witness" in s else ""
    network = "a second, unrelated network" if "operator" in s else ""
    if archive and network:
        return f"corroborated by {archive} and from {network}"
    if archive:
        return f"corroborated by {archive}"
    if network:
        return f"checked from {network} as well as this project's daily runner"
    return "corroborated by a second, independent check"


def gone_notes(sid: str) -> list:
    """Banner(s) for a source whose tracked copy is confirmed no longer
    resolving — the ledger's own reason to exist, so it is stated on the page."""
    out = []
    for (s, _t), r in sorted(GONE_TARGETS.items()):
        if s != sid:
            continue
        headline = GONE_WORDING.get(r.get("kind") or "", GONE_WORDING_DEFAULT)
        day = str(r.get("first", ""))[:10].replace("-", "") + "T"
        out.append(
            # headline and corroboration are this file's own constants, not
            # third-party text: escaping them only mangles their apostrophes
            f"<p><strong>{headline}</strong> "
            f"Confirmed on {esc(human_date(day))} — {gone_corroboration(r.get('by'))}. "
            f"The archived version(s) below, "
            f"with their SHA-256 hashes and OpenTimestamps proofs, are unaffected"
            + (f"; the address that stopped answering is "
               f"<code>{esc(str(r.get('url')))}</code>" if r.get("url") else "")
            + ".</p>")
    return out


def reader_notes(manifest) -> list:
    return [n for n in manifest.get("extraction_notes", [])
            if isinstance(n, str) and not any(m in n for m in INTERNAL_NOTE_MARKERS)]


def zip_inner_files(manifest) -> list:
    return [n for n in manifest.get("extraction_notes", [])
            if isinstance(n, dict) and "inner_file" in n]


def prior_cell(prior_sha, corpus_shas, prior_ref=None, repacked_shas=frozenset(),
               pruned=None) -> str:
    """Prior-capture row: a prior sha whose capture is no longer in the corpus
    must say WHY, not silently reference a version that is nowhere on the site.
    Only two sanctioned paths remove a capture, each leaving an event: a scope
    repack that replaced a full bundle by its Art. 53 subset (repacked_shas =
    the replaced bundles' hashes from scope-repack events, as verify_corpus C4
    reads them) and a prune of content-identical byte churn.
    prior_ref: (cap_slug, iso_ts) of the prior version's page when it is
    published as a sibling permalink — the hash then links to it."""
    if not prior_sha:
        return "<code>— first capture of this target</code>"
    if prior_sha in corpus_shas:
        if prior_ref:
            slug, iso = prior_ref[0], prior_ref[1]
            return (f"<a href='../{esc(slug)}/'><code>{esc(prior_sha)}</code></a> "
                    f"<span class='muted'>(captured {esc(iso)})</span>")
        return f"<code>{esc(prior_sha)}</code>"
    if prior_sha in repacked_shas:
        return (f"<code>{esc(prior_sha)}</code> <span class='muted'>(the full bundle as "
                f"fetched, replaced by this Art. 53 scope repack: members outside "
                f"scope are recorded by name and hash but never served — the "
                f"replacement is a scope-repack event in the log)</span>")
    ev = (pruned if pruned is not None else PRUNED_EVENTS).get(prior_sha)
    if ev and ev.get("via") == "prune_capture":
        return (f"<code>{esc(prior_sha)}</code> <span class='muted'>(that capture was "
                f"pruned as content-identical noise — by the prune rule its content is "
                f"identical to a retained version of this target; its hash is in the "
                f"event log)</span>")
    if ev:
        when = (ev.get("ts") or "")[:10]
        return (f"<code>{esc(prior_sha)}</code> <span class='muted'>(that capture was "
                f"removed during pre-launch curation"
                + (f" on {esc(when)}" if when else "")
                + f"; the event log records the reason: “{esc(ev.get('reason') or 'none recorded')}”"
                f" — content identity with a retained version was not checked by the "
                f"prune tool, and the capture is not in this repository's history)</span>")
    return (f"<code>{esc(prior_sha)}</code> <span class='muted'>(that capture is no "
            f"longer in the corpus and no prune event names it — see the event log)</span>")


# A snapshot witnesses a capture only if it was taken at about the same time.
# One from months earlier shows the page before we stored it; one from days
# later shows the page after, and neither can vouch for the bytes we hold. The
# window is generous because a save is triggered right after a fetch.
WAYBACK_CONCURRENT_S = 3600


def wayback_witnesses(m):
    """Whether the recorded snapshot witnesses THIS capture — that is, whether
    it was taken within WAYBACK_CONCURRENT_S of our fetch, either side. False
    for a pre-existing capture the Wayback Machine returned instead of crawling,
    and equally for one obtained long afterwards (a backlog drain archives the
    URL as it is today, not the document we stored). None when it cannot be
    told."""
    wb = m.get("wayback") or {}
    if not wb.get("ok") or not wb.get("snapshot"):
        return None
    ts = re.search(r"/web/(\d{14})", wb["snapshot"])
    fetched = (m.get("http") or {}).get("fetched_at") or ""
    if not ts or not fetched:
        return None
    try:
        snap = datetime.strptime(ts.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        got = datetime.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return abs((snap - got).total_seconds()) <= WAYBACK_CONCURRENT_S


def wayback_cell(m):
    wb = m.get("wayback") or {}
    snap = wb.get("snapshot")
    if not snap:
        return "saved (snapshot pending)" if wb.get("ok") else "not saved"
    ts = re.search(r"/web/(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})", snap)
    label = (f"Wayback snapshot, {ts.group(1)}-{ts.group(2)}-{ts.group(3)} "
             f"{ts.group(4)}:{ts.group(5)} UTC" if ts else "Wayback snapshot")
    caption = ""
    fetched = (m.get("http", {}).get("fetched_at") or "")[:10].replace("-", "")
    snap_date = f"{ts.group(1)}{ts.group(2)}{ts.group(3)}" if ts else ""
    if m.get("http", {}).get("rendered"):
        caption = " <span class='muted'>(separate fetch of a rendered page — bytes differ from this capture)</span>"
    elif snap_date and fetched and snap_date < fetched:
        # Save Page Now sometimes answers with an ALREADY-EXISTING snapshot instead
        # of crawling anew; that snapshot witnesses the page's earlier state, not
        # this capture — say so rather than implying we triggered it
        caption = (" <span class='muted'>(pre-existing snapshot returned by the "
                   "Wayback Machine — witnesses the page before this capture)</span>")
    elif snap_date and fetched and snap_date > fetched:
        caption = " <span class='muted'>(save triggered after capture; separate fetch)</span>"
    if wb.get("same_url") is False:
        # SPN followed a redirect: the snapshot archives the address it landed
        # on (a CDN or signed URL), not the one this ledger tracks
        caption += (" <span class='muted'>(the Wayback Machine followed a redirect: "
                    "this snapshot archives the address it landed on, not the "
                    "tracked URL)</span>")
    return f"<a href='{url_attr(snap)}'>{esc(label)}</a>{caption}"


# Capture kinds whose CONTENT belongs to someone other than the provider this
# ledger holds to account. AIAL's scored evaluations are their own research
# output, published with no licence granting redistribution: the ledger archives
# them so a revised grade stays recoverable, and publishes what it holds (size,
# canonical text hash, timestamp proof) without republishing their words.
# Remove a kind from here only once the rights holder has said yes.
RESTRICTED_KINDS = cap.RESTRICTED_KINDS


def restriction_of(source, m):
    """Why this capture's bytes and text are withheld, or None to serve them.
    Per capture, not per source: a source can carry both the provider's document
    and another party's assessment of it, and only the latter is withheld."""
    if source.get("restricted"):
        return source["restricted"]
    by_kind = RESTRICTED_KINDS.get(m.get("target_kind"))
    if by_kind:
        return by_kind
    http = m.get("http") or {}
    for u in (http.get("url"), http.get("final_url")):
        if u in cap.RESTRICTED_URLS:
            return ("third-party research, not served on this site or in the "
                    "dataset")
    return None


def structured_facts(manifest, text) -> str:
    """Restricted-source treatment: publish verifiable facts about the document
    (hashes, size, length, inner files) without republishing its content.
    text: the capture's extracted text (newline-normalized), or None."""
    rows = [f"<tr><th>Document size</th><td>{manifest['size_bytes']:,} bytes</td></tr>"]
    if manifest.get("text_sha256"):
        rows.append(f"<tr><th>Canonical text SHA-256</th><td><code>"
                    f"{esc(manifest['text_sha256'])}</code></td></tr>")
    if text is not None:
        words = len(text.split())
        rows.append(f"<tr><th>Extracted text length</th><td>{words:,} words</td></tr>")
    inner = zip_inner_files(manifest)
    if inner:
        files = "".join(f"<li><code>{esc(f['inner_file'])}</code> — "
                        f"<code>{esc(f['inner_sha256'][:16])}…</code></li>"
                        for f in sorted(inner, key=lambda f: f["inner_file"]))
        rows.append(f"<tr><th>Contained files</th><td><ul>{files}</ul></td></tr>")
    # WHY the content is withheld differs, and saying the wrong one is a false
    # statement about a real organisation: a provider asked, whereas a third
    # party's research is withheld by this project's own choice, unasked.
    http = manifest.get("http") or {}
    third_party = (manifest.get("target_kind") in RESTRICTED_KINDS
                   or http.get("url") in cap.RESTRICTED_URLS
                   or http.get("final_url") in cap.RESTRICTED_URLS)
    why = ("This is another organisation's own research, and this project does not "
           "redistribute it: the file itself is not served here"
           if third_party else
           "At the provider's request the document itself is not served here")
    return (f"<h2>Structured facts</h2><p class='muted'>{why}; these facts still "
            "pin its identity "
            "and let any copy be authenticated against this record.</p>"
            f"<div class='tablewrap' role='region' aria-label='Structured facts' "
            f"tabindex='0'><table>{''.join(rows)}</table></div>")


def extract_display(manifest, text) -> str:
    """Render extracted text with truncation marker; for ZIP bundles show a file
    manifest and only the Art. 53-relevant files' text.
    text: the capture's extracted text (newline-normalized), or None."""
    stored = manifest["stored_as"]
    label = ("<p class='muted'>Machine-extracted text (layout may be lost; the "
             "authoritative content is the stored file above).</p>")
    if text is None:
        return "<p class='muted'>No text extracted for this format — download the stored file above.</p>"

    if stored == "raw.zip":
        inner = zip_inner_files(manifest)
        rows = "".join(
            f"<tr><td>{esc(f['inner_file'])}</td><td><code>{esc(f['inner_sha256'][:16])}…</code></td>"
            f"<td>{esc(zip_file_label(f['inner_file']))}</td></tr>"
            for f in sorted(inner, key=lambda f: (zip_file_label(f["inner_file"]) != "Art. 53 summary",
                                                  f["inner_file"])))
        parts = re.split(r"^===== (.+?) =====$", text, flags=re.M)
        shown, omitted = [], 0
        for i in range(1, len(parts), 2):
            if zip_file_label(parts[i]) == "Art. 53 summary":
                shown.append(f"===== {parts[i]} =====\n" + parts[i + 1].strip())
            else:
                omitted += 1
        body = "\n\n".join(shown)
        omit_line = (f"<p class='muted'>Text of {omitted} other document(s) omitted from "
                     f"display; all are contained, byte-exact, in the stored raw.zip "
                     f"above.</p>" if omitted else "")
        excluded = next((d["members_not_stored"] for d in
                         manifest.get("extraction_notes", [])
                         if isinstance(d, dict) and "members_not_stored" in d), None)
        if excluded:
            xrows = "".join(
                f"<tr><td>{esc(x['inner_file'])}</td>"
                f"<td><code>{esc(x['inner_sha256'][:16])}…</code></td></tr>"
                for x in sorted(excluded, key=lambda x: x["inner_file"]))
            omit_line += (
                f"<h3>Files recorded but not stored</h3><p class='muted'>These "
                f"bundle members are outside Art. 53 scope (some carry "
                f"confidentiality markings); they are recorded by name and "
                f"SHA-256 so their identity stays provable, but their bytes are "
                f"not archived or served.</p>"
                f"<div class='tablewrap' role='region' aria-label='Files recorded "
                f"but not stored' tabindex='0'><table><tr><th>File</th>"
                f"<th>SHA-256</th></tr>{xrows}</table></div>")
        return (f"<h2>Files in this bundle</h2><div class='tablewrap' role='region' "
                f"aria-label='Files in this bundle' tabindex='0'>"
                f"<table><tr><th>File</th><th>SHA-256</th>"
                f"<th></th></tr>{rows}</table></div>"
                f"<h2>Extracted text (Art. 53 summaries)</h2>{label}{omit_line}"
                f"<pre>{esc(body[:EXTRACT_DISPLAY_LIMIT])}</pre>")

    shown = text[:EXTRACT_DISPLAY_LIMIT]
    marker = ""
    if len(text) > EXTRACT_DISPLAY_LIMIT:
        marker = (f"<p class='muted'>[Extract truncated at {EXTRACT_DISPLAY_LIMIT:,} "
                  f"characters for page size — the stored file above is complete; the "
                  f"SHA-256 covers the full file.]</p>")
    if "\x00" in shown:
        # NUL is not a legal HTML character; the stored extracted.txt keeps the
        # extractor's output verbatim, the page simply cannot carry it
        shown = shown.replace("\x00", "")
        marker = ("<p class='muted'>[Control characters (NUL) produced by text "
                  "extraction are omitted from this display; the stored file and "
                  "its SHA-256 are unaffected.]</p>") + marker
    return f"<h2>Extracted text</h2>{label}<pre class='extract'>{esc(shown)}</pre>{marker}"


def blob_names(m):
    """Content-addressed serving names for a capture: (ext_part, blob_name,
    ots_blob). Captured HTML gets a .txt suffix so it can never execute under
    this site's origin (captured pages carry live third-party scripts)."""
    ext_part = m["stored_as"].split("raw")[-1]                 # ".pdf", ".html", …
    blob_ext = ext_part + (".txt" if ext_part == ".html" else "")
    return ext_part, m["sha256"] + blob_ext, m["sha256"] + ext_part + ".ots"


def own_ots_name(m, cap_slug: str) -> str:
    """Serving name of a capture's own OpenTimestamps proof: the content
    address plus the capture id, so two captures of identical bytes never
    share one proof file."""
    ext_part = m["stored_as"].split("raw")[-1]
    return f"{m['sha256']}{ext_part}.{cap_slug}.ots"


def human_date(ts_slug: str) -> str:
    """20260811T102809Z -> 11 Aug 2026"""
    m = re.match(r"(\d{4})(\d{2})(\d{2})T", ts_slug)
    if not m:
        return ts_slug
    months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
              "Oct", "Nov", "Dec"]
    return f"{int(m.group(3))} {months[int(m.group(2))]} {m.group(1)}"


def iso_date(ts_slug: str) -> str:
    m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})", ts_slug)
    if not m:
        return ""
    return (f"{m.group(1)}-{m.group(2)}-{m.group(3)}T"
            f"{m.group(4)}:{m.group(5)}:{m.group(6)}Z")


def render_version_page(source, m, cap_slug, corpus_shas, text,
                        raw_exists: bool, ots_exists: bool,
                        prior_ref=None) -> str:
    """One version page's body. Pure: all facts come from the manifest, the
    registry source entry, and the (already-read) extracted text."""
    restricted = restriction_of(source, m)
    ext_part, blob_name, _shared_ots = blob_names(m)
    # every capture serves ITS OWN proof (identical bytes are captured under
    # several targets and dates; the shared <sha>.<ext>.ots name keeps serving
    # the earliest one for existing links)
    ots_blob = own_ots_name(m, cap_slug)
    blob_ext = ext_part + (".txt" if ext_part == ".html" else "")
    serve_name = "../../../../../blob/" + blob_name
    serve_ots = "../../../../../blob/" + ots_blob
    first = SHA_FIRST.get(m["sha256"])
    earlier = first if first and first[0][:16] < cap_slug[:16] else None

    serve_note = (" <span class='muted'>(served with a .txt suffix so the "
                  "captured page cannot run scripts on this site; bytes are "
                  "identical — the SHA-256 verifies against this file)</span>"
                  if blob_ext.endswith(".txt") else "")
    # a page must not promise a witness the same page reports as absent
    has_snap = bool((m.get("wayback") or {}).get("ok")
                    and (m.get("wayback") or {}).get("snapshot"))
    if restricted:
        stored_cell = (f"{m['size_bytes']:,} bytes — <span class='muted'>"
                       f"not served ({esc(str(restricted))}); the hash"
                       + (", timestamp proof and Wayback witness below"
                          if has_snap else " and timestamp proof below")
                       + f" still establish what was published when</span>")
    else:
        stored_cell = (f"<a href='{esc(serve_name)}' download>{esc(blob_name)}</a> "
                       f"({m['size_bytes']:,} bytes){serve_note}"
                       if raw_exists else f"{esc(m['stored_as'])} ({m['size_bytes']:,} bytes)")
    restamped = (m.get("ots") or {}).get("restamped_at")
    # a scope-repacked bundle was assembled after the fetch: its proof dates the
    # stored file, not the fetch (the replaced full bundle's hash is in the
    # scope-repack event; older manifests may carry it as full_bundle_sha256)
    repacked = bool(m.get("full_bundle_sha256")) or (m.get("prior_sha256") in REPACKED_SHAS)
    later_stamp = restamped or ((m.get("ots") or {}).get("at") if repacked else None)
    if restamped:
        ots_caption = (f"stamp submitted {esc(restamped)}, after the fetch — the original "
                       f"submission failed; the proof shows the bytes existed no later "
                       f"than that date (anchored in bitcoin over time)")
    elif repacked:
        ots_caption = (f"stamp submitted {esc(later_stamp or '')}, after the fetch — the "
                       f"stored file was assembled from the fetched bundle on that date; "
                       f"the proof shows these bytes existed no later than then (anchored "
                       f"in bitcoin over time)")
    else:
        ots_caption = "calendar-attested; anchored in bitcoin over time"
    if earlier:
        ots_caption += (f"; identical bytes were first archived on "
                        f"{esc(human_date(earlier[0]))} — <a href='{PREFIX}ledger/"
                        f"{esc(earlier[1])}/v/{esc(earlier[0])}/'>that version</a> "
                        f"carries the earliest attestation")
    ots_cell = (f"<a href='{esc(serve_ots)}' download>{esc(ots_blob)}</a> "
                f"<span class='muted'>({ots_caption})</span>" if ots_exists else "not stamped")
    if restricted:
        verify_line = ("<p class='muted'>Verify: obtain the file from the "
                       "target address above"
                       + (", or from the Wayback snapshot below" if has_snap else "")
                       + ", then <code>sha256sum</code> it — it must equal the "
                       "hash above; the <code>.ots</code> proof dates that "
                       "hash.</p>")
    else:
        verify_line = (f"<p class='muted'>Verify: <code>sha256sum {esc(blob_name)}</code> "
                       f"must equal the hash above (the filename IS the expected "
                       f"hash); <code>ots verify {esc(ots_blob)} -f {esc(blob_name)}</code> "
                       f"(<a href='https://opentimestamps.org'>opentimestamps.org</a>) "
                       + ("proves the bytes existed no later than the stamp date"
                          if later_stamp else
                          "proves the bytes existed no later than the attestation "
                          "time — an upper bound on the capture time; the fetch time "
                          "above is the archive's own record")
                       + " (fresh proofs report 'pending' until bitcoin-anchored, "
                       "typically within a day). <code>ots verify</code> needs a local "
                       "Bitcoin Core node (a pruned one is fine); without one, "
                       "<code>ots info</code> on the proof prints the attesting block "
                       "height and merkle path to check on any block explorer.</p>")
    notes = reader_notes(m)
    notes_row = (f"<tr><th>Notes</th><td>{esc('; '.join(notes))}</td></tr>"
                 if notes else "")
    # scope-repacked bundle: the stored zip was assembled after the fetch, so the
    # OTS proof dates the REPACKED file, not the fetch — say so, and point at the
    # original bundle's hash
    repack_row = ""
    if repacked:
        repack_row = (f"<tr><th>Original bundle</th><td><code>"
                      f"{esc(m.get('full_bundle_sha256') or m.get('prior_sha256') or '')}</code> "
                      f"<span class='muted'>(hash of the full bundle as fetched; "
                      f"this stored file is the Art. 53 subset, assembled at the "
                      f"date the OTS proof attests)</span></td></tr>")
    kind_label = KIND_LABELS.get(m["target_kind"], m["target_kind"])
    # A state harvested from a third party's git history was fetched TODAY, from a
    # commit-pinned address. Without the upstream date a reader sees only today's
    # fetch and cannot tell when the grade actually stood; without the sentence
    # after it, they could read the upstream date as the date this archive saw it.
    upstream_row = ""
    if m.get("git_commit"):
        when = str(m.get("git_commit_date") or "")
        upstream_row = (
            f"<tr><th>Upstream commit</th><td>{esc(human_date(when[:10].replace('-', '') + 'T') if when else when)}"
            f" — <code>{esc(str(m['git_commit'])[:12])}</code>"
            f"<span class='muted'> (when this state began to stand in the upstream "
            f"repository; this archive fetched it at the time above, not then)"
            f"</span></td></tr>")
    cop_warn = ("<p class='muted'><strong>Note:</strong> this is a GPAI Code of "
                "Practice document (Art. 53(1)(a)–(c) — model documentation and "
                "copyright policy), <em>not</em> the Art. 53(1)(d) public "
                "training-data summary.</p>"
                if m["target_kind"] == "cop-doc" else "")
    # A capture describes itself. Almost always that agrees with the source it is
    # filed under, but a third party's evaluation of a model this ledger does not
    # track is FILED under their source while being ABOUT another model: taking
    # the heading from the source published "GPAI Training Transparency tracker"
    # as the model of an evaluation of Claude Fable 5.
    shown_model = m.get("model") or source["model"]
    shown_provider = m.get("provider") or source["provider"]
    filed_note = ("" if shown_model == source.get("model") else
                  f"<p class='muted'>Filed under "
                  f"{esc(source.get('provider') or '')} — "
                  f"{esc(source.get('model') or '')}, the source this project "
                  f"captured it from; the "
                  + ("assessment itself is" if m.get("target_kind") in THIRD_PARTY_EVAL_KINDS
                     else "document itself is the filing")
                  + f" of the model named above.</p>")
    return (f"<h1>{esc(shown_model)} — capture {esc(cap_slug)}</h1>"
            + filed_note + cop_warn +
            f"<div class='tablewrap' role='region' aria-label='Capture provenance' tabindex='0'><table>"
            f"<tr><th>Provider</th><td>{esc(shown_provider)}</td></tr>"
            f"<tr><th>Target</th><td>{target_cell(kind_label, m['http']['url'])}</td></tr>"
            f"<tr><th>Fetched (UTC)</th><td>{esc(m['http']['fetched_at'])}</td></tr>"
            f"{upstream_row}"
            f"<tr><th>Stored file</th><td>{stored_cell}</td></tr>"
            f"<tr><th>SHA-256</th><td><code>{esc(m['sha256'])}</code></td></tr>"
            f"<tr><th>OpenTimestamps proof</th><td>{ots_cell}</td></tr>"
            f"<tr><th>Wayback</th><td>{wayback_cell(m)}</td></tr>"
            f"<tr><th>Prior capture of this target</th><td>{prior_cell(m.get('prior_sha256') or (prior_ref[2] if prior_ref else None), corpus_shas, prior_ref, REPACKED_SHAS, PRUNED_EVENTS)}</td></tr>"
            f"{repack_row}"
            f"{notes_row}"
            f"</table></div>{verify_line}"
            + (structured_facts(m, text) if restricted
               else extract_display(m, text)))


def is_document(r, inpage_urls) -> bool:
    # a third party's scored evaluation is evidence ABOUT the summary, never the
    # summary: it must not reach the document count, the changes feed, or the
    # drift comparison that decides whether a provider edited its document
    if r.get("kind") in THIRD_PARTY_EVAL_KINDS + THIRD_PARTY_METHOD_KINDS:
        return False
    return (r["stored_as"].endswith(DOC_SUFFIXES) or r["url"] in inpage_urls
            or bool(r["managed"]))


def distinct_documents(rows_data, inpage_urls) -> int:
    """Number of distinct document versions: byte-identical copies (the
    provider's file and AIAL's archived copy) and text-identical re-captures
    of one document count once."""
    return len({r.get("text_sha") or r["sha"] for r in rows_data
                if is_document(r, inpage_urls) and not r["retired"]})


def status_checked(source, checked) -> str:
    """Status-table 'Last checked': the source's own latest event date; for a
    source whose targets are fetched but that has no event yet, the sweep
    date; for a model with no known location nothing is checked."""
    own = checked.get(source["id"])
    if own:
        return own[:10]
    if source.get("targets"):
        return max(checked.values(), default="")[:10] or "—"
    return "—"


def version_row_html(r, inpage_urls, sha_first) -> str:
    """One row of a model page's capture table."""
    notes = []
    if r["url"] in inpage_urls:
        notes.append("document published as in-page web content (rendered capture)")
    elif r["rendered"]:
        notes.append("rendered page (captured with a browser)")
    elif r["stored_as"] == "raw.html" and r["txt_size"] < APP_SHELL_TEXT_BYTES:
        notes.append("page as served without JavaScript — mostly empty shell; "
                     "later captures store the fully rendered page")
    if "/blob/" in r["url"]:
        notes.append("HuggingFace viewer page around the file, not the file itself")
    if "drive.google.com/file/" in r["url"]:
        notes.append("Google Drive viewer page around the file, not the file itself")
    if "fbcdn.net" in r["url"]:
        notes.append("Meta CDN-hosted PDF (signed URL)")
    if r.get("upstream_date"):
        when = str(r["upstream_date"])[:10].replace("-", "")
        notes.append(f"the state that stood upstream from "
                     f"{human_date(when + 'T')} (fetched here on "
                     f"{human_date(r['ts'])})")
    if sha_first[r["sha"]] != r["ts"]:
        notes.append(f"bytes identical to {sha_first[r['sha']]}")
    elif r.get("diff_verdict") == "changed":
        # the version-diffs ledger compared both captures on one extractor; a
        # page that is not the document (hub, catalog, viewer) changed its text,
        # not "content" in the sense of the summary
        what = "content" if is_document(r, inpage_urls) else "page text"
        against = ("the previous capture of this target" if not r.get("diff_compared_with")
                   else f"the newest earlier capture of this target made with the same "
                        f"capture method ({r['diff_compared_with'].rsplit('/', 1)[-1]}; the "
                        f"immediately previous capture used a different method)")
        if not (r.get("diff_words") or 0) and (r.get("diff_moved") or 0):
            notes.append(f"text re-ordered vs {against} "
                         f"({r.get('diff_moved')} word(s) moved, none changed)")
        else:
            notes.append(f"{what} changed vs {against} "
                         f"({r.get('diff_words')} word(s) differ in the extracted text)")
    elif r.get("diff_verdict") == "changed-unverified":
        notes.append(f"extracted text differs from the previous capture "
                     f"({r.get('diff_words')} word(s) by the stored extracts) — the two "
                     f"captures could not be re-extracted with one tool, so this is not "
                     f"verified as a content change")
    elif r.get("diff_verdict") == "identical-text":
        # The ledger sees two matching word streams. It does not see WHY the bytes
        # differ, and for a form-style PDF the extracted text does not carry
        # checkbox state at all — so a flipped answer, the most material edit a
        # provider can make, changes the SHA-256 and leaves this text identical.
        # Asserting "not a content change" claimed knowledge the data cannot give.
        notes.append("bytes differ from the previous capture; the extracted text "
                     "is identical, so no change is visible in the text layer "
                     "(the text layer does not carry checkbox or form-field state)")
    elif r.get("diff_verdict") == "method-changed":
        notes.append("captured with a different method than the previous capture "
                     "(rendering, frame or consent handling changed): the extracted "
                     "text differs, which is not evidence of a content change")
    elif r.get("diff_verdict") == "no-text":
        notes.append("bytes differ from the previous capture; no extracted text on "
                     "one side to compare")
    elif r["prior_text_sha"] is not None and r["text_sha"] is not None:
        if r["prior_text_sha"] != r["text_sha"]:
            notes.append("content changed vs the previous capture of this target")
    elif (r["prior_sha"] and r["prior_sha"] != r["sha"]
            and not r["stored_as"].endswith(".html")):
        notes.append("content changed vs the previous capture of this target")
    if r["retired"]:
        notes.append(str(r["retired"]))
    return (f"<tr><td><a href='v/{esc(r['ts'])}/'>{esc(r['ts'])}</a></td>"
            f"<td>{esc(KIND_LABELS.get(r['kind'], r['kind']))}</td>"
            f"<td class='muted'>{esc(short_url(r['url']))}</td>"
            f"<td><code>{esc(r['sha'][:16])}…</code></td><td class='num'>{r['size']:,}</td>"
            f"<td class='muted'>{esc('; '.join(notes)) if notes else ''}</td></tr>")


VHEAD = ("<tr><th>Capture (UTC)</th><th>Source of copy</th><th>URL</th>"
         "<th>SHA-256</th><th>Bytes</th><th>Notes</th></tr>")


def wrap_table(label: str, inner: str) -> str:
    return (f"<div class='tablewrap' role='region' aria-label='{esc(label)}' "
            f"tabindex='0'><table>{inner}</table></div>")


def render_version_sections(rows_data, inpage_urls) -> list:
    """The model page's capture tables, split by capture class. rows_data must
    already be sorted newest-first."""
    sha_first = {}
    for r in sorted(rows_data, key=lambda r: r["ts"]):
        sha_first.setdefault(r["sha"], r["ts"])
    current = [r for r in rows_data if r["active"] and not r["retired"]]
    # a third party's scored assessment is neither the document nor a page we
    # watch for the document to appear on: it is evidence about the document
    third_party = THIRD_PARTY_EVAL_KINDS + THIRD_PARTY_METHOD_KINDS
    eval_rows = [version_row_html(r, inpage_urls, sha_first)
                 for r in current if r["kind"] in THIRD_PARTY_EVAL_KINDS]
    method_rows = [version_row_html(r, inpage_urls, sha_first)
                   for r in current if r["kind"] in THIRD_PARTY_METHOD_KINDS]
    doc_rows = [version_row_html(r, inpage_urls, sha_first)
                for r in current
                if is_document(r, inpage_urls) and r["kind"] not in third_party]
    surf_rows = [version_row_html(r, inpage_urls, sha_first)
                 for r in current
                 if not is_document(r, inpage_urls) and r["kind"] not in third_party]
    old_rows = [version_row_html(r, inpage_urls, sha_first)
                for r in rows_data if not r["active"] or r["retired"]]

    vsections = []
    if doc_rows:
        multi = len({(r["kind"], r["url"]) for r in current
                     if is_document(r, inpage_urls)}) > 1
        multi_note = ("<p class='muted'>Rows may capture the same summary from "
                      "different sources or formats (the provider's site, AIAL's "
                      "archived copy); sizes and hashes are comparable only within "
                      "one URL.</p>" if multi else "")
        vsections.append("<h2>Document versions</h2>" + multi_note
                         + wrap_table("Document versions", VHEAD + "".join(doc_rows)))
    if surf_rows:
        caption = ("<p class='muted'>Pages monitored for change — new or updated "
                   "summaries typically appear here first.</p>"
                   if not doc_rows else
                   "<p class='muted'>Captures of the page(s) where the document is "
                   "published (portals, hubs, docs pages) — monitored for change "
                   "detection; not the document itself.</p>")
        vsections.append("<h2>Watch-surface captures</h2>" + caption
                         + wrap_table("Watch-surface captures", VHEAD + "".join(surf_rows)))
    if eval_rows:
        vsections.append(
            "<h2>Third-party evaluation</h2>"
            "<p class='muted'>The AI Accountability Lab scores published summaries "
            "against the Commission's template and publishes the result. These are "
            "captures of <strong>their assessment</strong>, archived here because "
            "they are revised over time and a score is otherwise unrecoverable once "
            "changed. It is AIAL's research judgement, not a legal determination, "
            "and not the provider's document. Their assessment is archived "
            "here but <strong>not served on this site or in the dataset</strong>: "
            "each capture below "
            "proves what was published and when, and points to AIAL for the "
            "content. Attribution: "
            "<a href='https://aial.ie/research/gpai-training-transparency/'>aial.ie"
            "</a>.</p>"
            + wrap_table("Third-party evaluation", VHEAD + "".join(eval_rows)))
    if method_rows:
        vsections.append(
            "<h2>The framework these evaluations use</h2>"
            "<p class='muted'>AIAL's scoring framework, weightings and grade "
            "boundaries — the pages that make a published score readable as a "
            "grade. Archived for the same reason as the evaluations, and on the "
            "same terms: held and hashed here, <strong>not served on this site "
            "or in the dataset</strong>. "
            "Attribution: <a href='https://aial.ie/research/gpai-training-"
            "transparency/methodology'>aial.ie</a>.</p>"
            + wrap_table("Scoring framework", VHEAD + "".join(method_rows)))
    if old_rows:
        vsections.append("<h2>Captures of superseded target URLs</h2>"
                         "<p class='muted'>Locations this ledger previously tracked "
                         "for this source (per-row notes give the reason each was "
                         "superseded) — kept as faithful records of what those URLs "
                         "served at capture time.</p>"
                         + wrap_table("Superseded target URLs", VHEAD + "".join(old_rows)))
    return vsections


def render_model_page(source, vsections, checked):
    """A model page's (body, title). checked: source id -> last event ts."""
    sid = source["id"]
    status = source["status"]
    aial = source.get("aial", {})
    retired = bool(source.get("retired"))
    # a missing-summary model with no candidate location: nothing is fetched for
    # it, so the page must not say it is checked
    no_location = status == "missing" and not source.get("targets") and sid not in checked
    if status == "missing" and retired:
        if aial:
            missing_note = ("While this model was listed in the AI Accountability Lab "
                            "(AIAL) registry, AIAL assessed it as covered by the EU "
                            "disclosure obligation and found no published summary — "
                            "their research assessment, not a legal ruling. The model "
                            "has since left that registry and is no longer checked here.")
        else:
            missing_note = ("No published summary was located by this project's "
                            "monitoring while the model was tracked; whether the model "
                            "is obligated under Art. 53 has not been assessed. It is no "
                            "longer checked here.")
    elif status == "missing":
        if aial:
            missing_note = ("The AI Accountability Lab (AIAL) assesses this model as "
                            "covered by the EU disclosure obligation but has found no "
                            "published summary — their research assessment, not a "
                            "legal ruling.")
        else:
            missing_note = ("No published summary located by this project's "
                            "monitoring; whether the model is obligated under "
                            "Art. 53 has not been assessed.")
    else:
        missing_note = ""

    anthropic_x = ""
    if sid.startswith("anthropic/claude"):
        if status == "published":
            anthropic_x = ("<p class='muted'>Anthropic publishes this model's summary "
                           "inside its trust center; the document is also captured in "
                           "the <a href='../trust-center-bundle/'>Anthropic "
                           "trust-center bundle</a>.</p>")
        else:
            anthropic_x = ("<p class='muted'>Anthropic publishes summaries for some "
                           "newer models in its <a href='../trust-center-bundle/'>"
                           "trust center (watched here)</a>; none has "
                           "been located for this model.</p>")

    last_ts = checked.get(sid) or (max(checked.values()) if checked else "")
    checked_line = ""
    if last_ts and retired:
        label = "Last checked" if sid in checked else "Last sweep"
        checked_line = (f"<p class='muted'>{label}: {esc(last_ts[:10])} "
                        f"(no longer checked — the model left the registry this "
                        f"project follows; see "
                        f"<a href='{PREFIX}methodology/'>Methodology</a>). "
                        f"Seen a summary we missed? See the "
                        f"<a href='{PREFIX}corrections/'>Corrections</a> page.</p>")
    elif no_location:
        checked_line = (f"<p class='muted'>No candidate location for a summary is "
                        f"known for this model yet, so there is no URL to check; the "
                        f"registry it is listed in is re-read on the daily schedule "
                        f"(see <a href='{PREFIX}methodology/'>Methodology</a>). "
                        f"Seen a summary we missed? See the "
                        f"<a href='{PREFIX}about/'>contact details</a>.</p>")
    elif last_ts:
        label = "Last checked" if sid in checked else "Last sweep"
        checked_line = (f"<p class='muted'>{label}: {esc(last_ts[:10])} "
                        f"(re-checked on the daily schedule — see "
                        f"<a href='{PREFIX}methodology/'>Methodology</a>). "
                        f"Seen a summary we missed? See the "
                        f"<a href='{PREFIX}about/'>contact details</a>.</p>")

    purpose = (f"<p>{esc(source['note'])}</p>" if source.get("note") else "")
    aial_line = ""
    if aial:
        aial_line = (f"<p class='muted'>AIAL metadata: summary dated "
                     f"{esc(aial.get('public_summary_date') or 'none found')}, model "
                     f"released {esc(aial.get('model_publication_date') or 'none found')}, "
                     f"<a href='{url_attr(aial.get('eval_page', ''))}'>eval page</a></p>")

    subtitle = ""
    intro = ""
    if status in ("published", "missing"):
        subtitle = ("<p class='subtitle'>Public summary of training content · "
                    "Article 53(1)(d), Regulation (EU) 2024/1689</p>")
        expl = (f"<a href='{PREFIX}what-is-a-training-data-summary/'>public "
                f"summary of training content</a>")
        if status == "published":
            # A confirmed absence on one of this source's own document targets
            # makes the present tense false: the Microsoft MAI-Image-2 page
            # asserted the provider "publishes a" summary three lines above its
            # own "no longer resolves" banner.
            gone_here = any(s == source["id"]
                            and (r.get("kind") or "") in PROVIDER_DOC_KINDS
                            for (s, _t), r in GONE_TARGETS.items())
            if gone_here:
                intro = (f"<p>{esc(source['provider'])} published a {expl} "
                         f"for {esc(source['model'])} under the EU AI Act. The "
                         f"copy this ledger tracked no longer resolves (see "
                         f"below); every version captured while it did is "
                         f"archived here with its hash and timestamp proof.</p>")
            else:
                intro = (f"<p>{esc(source['provider'])} publishes a {expl} "
                         f"for {esc(source['model'])} under the EU AI "
                         f"Act; every version this ledger has captured is archived below "
                         f"with its hash and timestamp proof.</p>")
        else:
            intro = (f"<p>No {expl} has been located "
                     f"for {esc(source['model'])}. "
                     + ("No location for one is known yet; this page will archive "
                        "every version once one is found.</p>" if no_location else
                        "This page tracks the locations where one would appear and "
                        "will archive every version once published.</p>"))

    badge = (f"<strong class='tag tag-{esc(status)}'>"
             f"{esc(STATUS_LABELS.get(status, status))}</strong>")
    model_body = (f"<h1>{esc(source['model'])}</h1>" + subtitle +
                  f"<p>Provider: {esc(source['provider'])} — status: {badge}"
                  + (f" <span class='muted'>({esc(missing_note)})</span>" if missing_note else "")
                  + "</p>" + intro + purpose + aial_line + anthropic_x
                  + ("".join(vsections) if vsections
                     else "<p class='muted'>No captures stored yet"
                          + (" — no summary has been located." if status == "missing" else ".")
                          + "</p>")
                  + checked_line)
    page_title = model_page_title(source)
    return model_body, page_title


# bespoke titles for the reference sources with real query volume; everything
# else follows the status-driven template
TITLE_OVERRIDES = {
    "eu-commission/explanatory-notice-and-template":
        "AI Act training-content template C(2025) 8311 — archived | GPAI Ledger",
    "aial/tracker":
        "AIAL's GPAI Training Transparency tracker, archived | GPAI Ledger",
    "anthropic/trust-center-bundle":
        "Anthropic trust-center bundle — Art. 53 summaries archived | GPAI Ledger",
}
DESC_OVERRIDES = {
    "eu-commission/explanatory-notice-and-template":
        "The European Commission's mandatory template for AI Act training-data "
        "summaries — C(2025) 8311 final and the Explanatory Notice, archived with "
        "hash and timestamp proof.",
    "aial/tracker":
        "The AI Accountability Lab's GPAI transparency tracker, archived here so "
        "the provenance chain includes its own upstream source.",
    "anthropic/trust-center-bundle":
        "Anthropic publishes training-data summaries inside its trust-center "
        "bundle; every Art. 53 document version is archived here with hashes and "
        "timestamp proofs.",
}


def model_page_title(source) -> str:
    sid = source["id"]
    if sid in TITLE_OVERRIDES:
        return TITLE_OVERRIDES[sid]
    status = source["status"]
    if status == "published":
        return (f"{source['model']} training data summary "
                f"({source['provider']}) | GPAI Ledger")
    if status == "missing":
        return (f"{source['model']} training data summary — none located "
                f"({source['provider']}) | GPAI Ledger")
    # avoid doubled parentheticals like "Model catalog (developer docs) (xAI)"
    # while keeping cross-provider uniqueness
    if source["provider"].lower() in source["model"].lower():
        return f"{source['model']} — GPAI Ledger"
    if "(" in source["model"]:
        return f"{source['model']} · {source['provider']} — GPAI Ledger"
    return f"{source['model']} ({source['provider']}) — GPAI Ledger"


def model_page_desc(source, rows_data, inpage_urls) -> str:
    if source["id"] in DESC_OVERRIDES:
        return DESC_OVERRIDES[source["id"]]
    if source["status"] == "published" and rows_data:
        first = min(r["ts"] for r in rows_data)
        return (f"{source['model']}'s EU AI Act training-data summary "
                f"({source['provider']}), archived since {human_date(first)}: "
                f"every version with capture date, SHA-256 hash and "
                f"OpenTimestamps proof.")
    if source["status"] == "missing" and source.get("retired"):
        return (f"No Article 53(1)(d) training-data summary was located for "
                f"{source['model']} ({source['provider']}) while it was tracked; "
                f"the model has left the registry this project follows and is no "
                f"longer checked.")
    if source["status"] == "missing" and not source.get("targets"):
        return (f"No Article 53(1)(d) training-data summary located for "
                f"{source['model']} ({source['provider']}); no location is known "
                f"yet — registry metadata re-read daily; archived with proof once found.")
    if source["status"] == "missing":
        return (f"No Article 53(1)(d) training-data summary located for "
                f"{source['model']} ({source['provider']}). Tracked daily; "
                f"archived with proof once published.")
    return (f"Captures of {source['model']} ({source['provider']}), archived by "
            f"the GPAI Ledger with SHA-256 hashes and OpenTimestamps proofs.")


def render_index_row(source, rows_data, inpage_urls) -> str:
    sid = source["id"]
    n_docs = distinct_documents(rows_data, inpage_urls)
    n_caps = len(rows_data)
    status = source["status"]
    badge = (f"<strong class='tag tag-{esc(status)}'>"
             f"{esc(STATUS_LABELS.get(status, status))}</strong>")
    return (f"<tr><td><a href='{PREFIX}ledger/{esc(sid)}/'>{esc(source['model'])}</a></td>"
            f"<td>{esc(source['provider'])}</td>"
            f"<td>{badge}</td>"
            f"<td class='num'>{n_docs}</td><td class='num'>{n_caps}</td></tr>")


def render_index(model_rows, other_rows, stats: dict) -> str:
    legend = ("<p class='muted'><strong>Status legend</strong> — "
              "<em class='published'>published</em>: a summary document is located and "
              "captured. <em class='missing'>missing</em>: no summary located — for "
              "AIAL-tracked models this reflects AIAL's research assessment that one is "
              "required (not a legal determination); for models added by this project's "
              "own monitoring, obligation has not been assessed. "
              "<em class='regulatory'>regulatory</em>: official Commission documents, "
              "archived for reference. <em class='watch'>watch</em>: pages we monitor "
              "because summaries or template changes may appear there first. "
              "<em>Documents</em> counts distinct document versions (byte- or text-identical copies, such as the provider's file and AIAL's archived copy, count once); <em>Captures</em> "
              "counts all stored snapshots including monitoring pages.</p>")
    THEAD = ("<tr><th>Tracked source</th><th>Provider</th><th>Status</th>"
             "<th>Documents</th><th>Captures</th></tr>")
    strip = ("<dl class='statstrip'>"
             + "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>"
                       for k, v in stats.items())
             + "</dl>")
    return (f"<h1>The public record of EU AI Act training-data summaries</h1>"
            f"<p>Article 53(1)(d) of the EU AI Act requires every provider of a "
            f"general-purpose AI model placed on the EU market — open-source models "
            f"included — to publish a <em>public summary of training content</em>. "
            f"Providers publish these documents on their own sites and "
            f"can change, move, or remove them at any time — no official registry "
            f"exists. The GPAI Ledger checks every known summary on a daily schedule and archives "
            f"every version with a SHA-256 hash, an OpenTimestamps proof, and — where "
            f"the URL permits — a Wayback snapshot, so what was published, and "
            f"when, stays provable. "
            f"<a href='{PREFIX}what-is-a-training-data-summary/'>What is a "
            f"training-data summary?</a></p>"
            + strip
            + legend
            + "<h2>Models</h2>"
            + wrap_table("Models", THEAD + "".join(r for _, r in sorted(model_rows)))
            + "<h2>Regulatory documents &amp; watched surfaces</h2>"
            + wrap_table("Regulatory documents and watched surfaces",
                         THEAD + "".join(r for _, r in sorted(other_rows))))


# ---------------------------------------------------------------------------
# Reference pages
# ---------------------------------------------------------------------------

def render_about() -> str:
    return f"""<h1>About the GPAI Ledger</h1>
<p>The GPAI Ledger is a public-interest archive of the training-data disclosures the
EU AI Act requires: Article 53(1)(d) obliges providers of general-purpose AI models placed
on the EU market to publish a summary of the content used to train each model,
according to the Commission's template. Providers
publish these documents on their own websites and may change, move, or remove them at
any time; no official registry exists. This ledger checks every known summary on a
daily schedule, keeps every content version, and records the evidence needed to
prove what was published when. Every check is logged in the append-only event log,
so any missed sweep is itself visible in the record.</p>
<h2>Who runs it</h2>
<p>The GPAI Ledger is an independent, self-funded project. It is
not affiliated with any AI provider, law firm, or regulator, and accepts no
funding from the companies it tracks. Model metadata is seeded from the
<a href="https://aial.ie/research/gpai-training-transparency/">AI Accountability Lab
(AIAL)</a> at Trinity College Dublin, with attribution, plus this project's own
monitoring and discoveries.</p>
<h2>Contact</h2>
<p>Corrections, disputes, provider objections, or tips about summaries we missed:
<a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>. Providers who object to
full-text archiving of a document are offered a structured-facts treatment — hashes,
sizes and provenance metadata without the document bytes; see the
<a href="{PREFIX}methodology/">methodology page</a>.</p>
<h2>What is automated, what is human</h2>
<p>Capture, hashing, timestamping, and page generation are fully automated and run
on a public, auditable pipeline. Which sources to track, how to label their status,
and every editorial note are human decisions. Errors are corrected openly — see the
<a href="{PREFIX}corrections/">corrections log</a>.</p>
<h2>Relationship to AIAL</h2>
<p>AIAL grades the <em>quality</em> of published summaries and maintains the tracker
whose metadata seeds this ledger's model list. AIAL's grades and scoping assessments
are theirs; this ledger's captures, version chains, and provenance are this
project's. Their pages are archived here so the provenance chain includes its own
upstream source.</p>
<h2>Licensing</h2>
<p>Capture manifests, hashes, and the append-only event log are released under CC0.
Archived documents remain the property of their publishers and are served for
public-interest transparency, research, and verification, with attribution.</p>"""


def render_methodology() -> str:
    repo_link_m = (f'<a href="{esc(REPO_URL)}">the project repository</a>'
                   if REPO_URL else "the project repository")
    return f"""<h1>Capture, hash, timestamp: how the evidence is made</h1>
<p>Every location where a <em>public summary of training content</em>
(Article 53(1)(d), Regulation (EU) 2024/1689) is published gets checked on a daily
schedule. This page explains exactly what is stored, how change is detected, and how
anyone can verify a capture without trusting this site.</p>
<h2>The capture pipeline</h2>
<ol>
<li><strong>Fetch.</strong> Each tracked URL is fetched on a daily schedule
(conditional GETs
spare origins a re-download when nothing changed). JavaScript-only pages are
rendered in a real browser and stored as the rendered page, marked as such. A 404 seen from the automated runner for a document already archived is not treated as an absence on its own: the target is re-checked after a pause and then cross-checked against an independent witness (a fresh Wayback Machine capture, accepted only if it is genuinely fresh); every check is logged with the vantage point it was made from. An absence is called confirmed only when a second vantage point saw it too — the witness, or the operator checking from another network; a 404 the runner alone keeps seeing on several dates is recorded as persistent, never as confirmed, because one datacenter address cannot tell a removed document from a refused connection.</li>
<li><strong>Dedupe.</strong> A new version is stored only when content actually
changes: byte hash for documents, a whitespace-insensitive text hash for rendered
pages, an inner-file hash set for provider bundles.</li>
<li><strong>Prove.</strong> Each stored version keeps the exact fetched bytes, the
extracted text, HTTP metadata, a SHA-256, an
<a href="https://opentimestamps.org">OpenTimestamps</a> proof (anchored in the
bitcoin blockchain, typically within a day), and — where the URL permits — a triggered
Internet Archive (Wayback) snapshot as an independent witness.</li>
<li><strong>Publish.</strong> Every version gets a permanent page; stored bytes are
served under their own hash from the content-addressed <code>/blob/</code> store.</li>
</ol>
<h2>Verify a capture yourself</h2>
<p>Files are served under their own hash (e.g. <code>&lt;sha256&gt;.pdf</code>), so
the filename is the expected checksum:</p>
<pre>sha256sum &lt;sha256&gt;.pdf          # must equal the filename / the version page's SHA-256
ots verify &lt;sha256&gt;.pdf.ots -f &lt;sha256&gt;.pdf   # proves the bytes existed no later than the attestation time (opentimestamps.org)</pre>
<p>OpenTimestamps proofs are attested by public calendar servers within seconds and
anchored in the bitcoin blockchain, typically within a day; anchored proofs verify against the
blockchain with no trust in this site required. <code>ots verify</code> checks the attestation
against a local Bitcoin Core node (a pruned node is enough); without one, <code>ots info
&lt;proof&gt;</code> prints the attesting block height and merkle path, which any block explorer
lets you check by hand. The crawler, verifier, and full
corpus (raw bytes, manifests, event log) are public in {repo_link_m}, so
the whole archive can be re-verified from source.</p>
<h2>Permalink stability</h2>
<p>Version URLs (<code>/ledger/&lt;provider&gt;/&lt;model&gt;/v/&lt;capture&gt;/</code>)
are stable and safe to cite; content-bearing versions are never removed. Model
slugs are never renamed. The one exception is narrow: a re-capture whose bytes
changed but whose content is identical to a neighboring version (banner churn,
re-rendering) may be pruned as noise — every prune is logged in the append-only
event log (tool-made prunes carry the pruned file's hash and reason; the
curation-time prunes of August 2026 carry the capture directory, whose hash is in
the matching capture event) — and the prune tool's rule guarantees the pruned
capture's content survives in a neighboring retained version.
Capture ids are minting timestamps and can trail the fetch
time by seconds; the manifest's <code>fetched_at</code> is authoritative.</p>
<h2>Work this project does not republish</h2>
<p>Two kinds of capture are held but not served. A provider who objects to
full-text archiving is switched to the treatment described next. Separately, a
third party's own research — the AI Accountability Lab's scored evaluations of
published summaries — is archived so that a grade stays recoverable after they
revise it, but their words are not served on this site, in the CC0 dataset or in
the change ledger: those pages carry the
hashes, size, and timestamp proof, and link to AIAL for the assessment itself.
The captures themselves remain in this project's public corpus on GitHub, so
that every hash published here can be reproduced from it — withholding applies to
what this site and its dataset serve, not to the evidence trail. No objection has
been made; the ledger simply does not republish another
organisation's work without their permission.</p>
<h2>Provider objections</h2>
<p>A provider who objects to full-text archiving of a document is switched to a
structured-facts treatment: the page keeps the document's hashes, size, text length,
and provenance chain — enough to authenticate any copy — without serving the bytes.
Write to <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>.</p>
<h2>What the labels mean</h2>
<p><em>provider site / provider page</em>: the provider's own published copy or the
page that links it. <em>AIAL archived copy</em>: the write-once snapshot archived by
the AI Accountability Lab. <em>watched page</em>: a portal, hub, or listing monitored
because documents appear or change there. <em>AIAL evaluation</em>: the AI
Accountability Lab's scored assessment of a published summary — their research
judgement, not a legal determination, and not the provider's document. <em>official document</em>: European
Commission material (the template summaries must follow). <em>Code of Practice
doc</em>: a document published under the Art. 56 Code of Practice, whose chapters cover
Art. 53(1)(a)–(c) and Art. 55 — related to, but distinct from, the Art. 53(1)(d)
summary.</p>
<h2>Complementary resources</h2>
<p><a href="https://aial.ie/research/gpai-training-transparency/">AIAL's
transparency tracker</a> grades the quality of published summaries.
<a href="https://eur-lex.europa.eu/eli/reg/2024/1689/oj">Regulation (EU) 2024/1689
on EUR-Lex</a> is the law itself. The
<a href="https://digital-strategy.ec.europa.eu/en/factpages/general-purpose-ai-obligations-under-ai-act">European
Commission's GPAI pages</a> carry the official template and guidance.</p>"""


def render_explainer() -> str:
    return f"""<h1>What is an Article 53(1)(d) training-data summary?</h1>
<p class='subtitle'>Public summary of training content · Article 53(1)(d),
Regulation (EU) 2024/1689</p>
<p>The EU AI Act requires every provider of a general-purpose AI (GPAI) model placed
on the EU market to <em>"draw up and make publicly available a sufficiently detailed
summary about the content used for training"</em> — Article 53(1)(d) of Regulation
(EU) 2024/1689. It is among the first legal instruments anywhere that force AI companies
to disclose, publicly, what their models were trained on — applicable since 2 August 2025,
ahead of California's AB 2013 (operative from 1 January 2026).</p>
<h2>What the summary must contain</h2>
<p>The European Commission issued the mandatory template on 24 July 2025 (C(2025)
5235 final) and formally adopted it as a Commission Communication on
5 December 2025 (C(2025) 8311 final —
<a href="{PREFIX}ledger/eu-commission/explanatory-notice-and-template/">every
version archived here</a>). A compliant summary identifies the model and provider,
states the modalities and overall size of the training data, then lists the main data
sources by category. The accompanying Explanatory Notice sets the level of detail; it
also directs a provider who further trains a model already on the market on additional
data requiring a change to the summary to update it — at six-month intervals, or sooner
if that data requires a materially significant change to the summary's content — and to
record the date of each update.</p>
<h2>The template's three sections</h2>
<ol>
<li><strong>General information</strong> — provider identification (including the
authorised representative for providers established outside the EU, Article 54),
model identification and dependencies, the date the model was placed on the EU
market, and the modalities, overall size and general characteristics of the
training data.</li>
<li><strong>List of data sources</strong> — per category: publicly available
datasets, private third-party datasets (licensed or otherwise), data crawled or
scraped by or on behalf of the provider (with crawler names, crawler behavior such
as respect for robots.txt, collection periods, and a summary of the top domain
names scraped), user data, synthetic data, and other sources.</li>
<li><strong>Relevant data processing aspects</strong> — the measures taken to
respect reservations of rights under the text-and-data-mining exception (Article
4(3) of Directive (EU) 2019/790) and to remove illegal content from the training
data.</li>
</ol>
<h2>Where to read the published summaries</h2>
<p>The <a href="{PREFIX}status/">publication-status table</a> lists every tracked
model; each links its archive of captured versions — for example
<a href="{PREFIX}ledger/meta/muse-spark/">Meta Muse Spark</a> (a summary that has
already changed between versions) or
<a href="{PREFIX}ledger/fastweb/fastwebmiia/">FastwebMIIA</a>.</p>
<h2>Who must publish, and when</h2>
<p>Every GPAI provider, EU-based or not, whose model is placed on the EU market —
including providers of free and open-source models, because the open-source
exemption in Article 53(2) covers only points (a) and (b) of Article 53(1), not the
summary. A downstream entity that modifies a model so substantially that it becomes
the provider must publish its own summary, limited to the data used for the
modification. The obligation has applied since <strong>2 August 2025</strong> to models
placed on the market from that date, with the summary due at the latest when the
model is placed on the market; models already on the market before then have until
<strong>2 August 2027</strong>. See the <a href="{PREFIX}deadlines/">deadlines
page</a> for the full timeline.</p>
<h2>What happens if a provider doesn't comply</h2>
<p>The Commission, acting through the AI Office, can request information (Article
91), request measures up to restricting or withdrawing the model (Article 93) and
impose fines on GPAI providers of up to <strong>€15&nbsp;million or 3% of total
worldwide annual turnover in the preceding financial year</strong>, whichever is
higher (Article 101). These supervision and enforcement powers apply from
2 August 2026.</p>
<h2>Why an archive is needed</h2>
<p>Providers publish these summaries on their own websites — behind changing URLs,
rotating tokens, consent walls, and silent edits. There is no official registry, no
version history, and no guarantee yesterday's summary still says what it said. The
GPAI Ledger <a href="{PREFIX}">archives every version of every summary it can
find</a>, daily, with cryptographic proof of what was published when — so
researchers, journalists, rights holders and compliance teams can cite the record
rather than a link that may die.</p>
<h2>Read the sources</h2>
<p><a href="https://eur-lex.europa.eu/eli/reg/2024/1689/oj">Regulation (EU)
2024/1689 (EUR-Lex)</a> · <a
href="https://digital-strategy.ec.europa.eu/en/library/explanatory-notice-and-template-public-summary-training-content-general-purpose-ai-models">Commission
Explanatory Notice &amp; template (public summary of training content)</a> · <a
href="https://aial.ie/research/gpai-training-transparency/">AIAL's quality
grades</a></p>
<p class='muted'>Last reviewed: {esc(LEGAL_TEXT_REVIEWED)}. This page summarizes
the law for orientation; it is not legal advice.</p>"""


def render_deadlines() -> str:
    return """<h1>When training-data summaries are due</h1>
<p class='subtitle'>Article 53(1)(d), Regulation (EU) 2024/1689 — the timeline</p>
<h2>2 August 2025 — the obligation starts</h2>
<p>Providers placing a general-purpose AI model on the EU market from this date must
publish the training-content summary, at the latest when the model is placed on the
market; the open-source exemption in Article 53(2) does not cover this summary. The
Commission's mandatory template was issued on 24 July 2025 (C(2025) 5235 final) and
formally adopted as a Commission Communication on 5 December 2025 (C(2025)
8311 final).</p>
<h2>2 August 2026 — enforcement powers apply</h2>
<p>The Commission's supervision and enforcement powers over GPAI providers, exercised
through the AI Office, apply (Articles 88–94). Fines for non-compliance with GPAI
obligations, imposed by the Commission, can reach €15 million or 3% of total
worldwide annual turnover in the preceding financial year, whichever is higher
(Article 101).</p>
<h2>2 August 2027 — legacy-model deadline</h2>
<p>Providers of models placed on the market <em>before</em> 2 August 2025 must have
brought them into compliance — including the public training-content summary — by
this date (Article 111(3)). The Explanatory Notice allows such a provider that cannot,
despite best efforts, provide parts of the required information to state and justify
the gaps in its summary.</p>
<h2>Six-month intervals — updates after further training</h2>
<p>The Regulation sets no fixed update cadence. The Commission's Explanatory Notice
directs a provider that further trains a model already on the market on additional
data requiring a change to the summary to update it at six-month intervals, or sooner
if that data requires a materially significant change to the summary's content —
whichever comes first — recording the date of each update. The GPAI Ledger's daily
capture makes those updates — and any silent edits — visible as a version chain.</p>
<h2>Unchanged by the Digital Omnibus</h2>
<p>The Digital Omnibus on AI (Regulation (EU) 2026/1744, in force since 27 July 2026)
postponed several high-risk and transparency deadlines but left the obligations of
general-purpose AI model providers in Articles 51–55 — and every date on this page —
unchanged.</p>
<p><a href="https://eur-lex.europa.eu/eli/reg/2024/1689/oj">Read the Regulation on
EUR-Lex</a> · <a href="https://eur-lex.europa.eu/eli/reg/2026/1744/oj">Regulation (EU)
2026/1744 (Digital Omnibus on AI)</a>.</p>"""


def render_corrections(entries: list) -> str:
    repo_suffix = (f' (<a href="{esc(REPO_URL)}">repository</a>)' if REPO_URL else "")
    rows = "".join(f"<li><strong>{esc(d)}</strong> — {body}</li>"
                   for d, body in entries)
    return f"""<h1>Corrections</h1>
<p>An evidence archive earns trust by correcting itself in the open. Errors in
captures, labels, or analysis are corrected with a dated note — the original record
is never silently rewritten, and the append-only event log keeps every operation
inspectable.</p>
<p>To report an error: <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>.
The full analyses these corrections refer to live in the project repository's
<code>reports/</code> directory{repo_suffix}.</p>
<h2>Correction log</h2>
<ul>{rows}</ul>"""


def render_privacy() -> str:
    return """<h1>Privacy</h1>
<p>This site sets no cookies, runs no analytics, embeds no third-party resources,
and loads no external fonts or scripts. Every asset — including the heading font —
is served from this site's own origin.</p>
<p>Hosting is provided by GitHub Pages. GitHub logs and stores each visitor's IP
address for security purposes (see
<a href="https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages#data-collection">GitHub
Pages: data collection</a>) and processes request data under
<a href="https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement">GitHub's
privacy statement</a>. This project itself collects nothing.</p>
<p>Email sent to the contact address is used solely to handle the correspondence
(corrections, disputes, objections, tips) and is not shared.</p>"""


def render_status_page(status_rows) -> str:
    head = ("<tr><th>Provider</th><th>Model</th><th>Status</th>"
            "<th>Document versions</th><th>Last content change</th>"
            "<th>Last checked</th></tr>")
    rows = "".join(status_rows)
    return (f"<h1>Who has published a training-data summary — and who hasn't</h1>"
            f"<p class='subtitle'>Public summaries of training content · "
            f"Article 53(1)(d), Regulation (EU) 2024/1689</p>"
            f"<p>Status of every general-purpose AI model this ledger tracks, as of "
            f"the last sweep. "
            f"<em>Published</em> means a summary document is located and archived; "
            f"<em>missing</em> reflects the AI Accountability Lab's research "
            f"assessment (or this project's monitoring) that none has been found — "
            f"not a legal determination. Facts only: capture dates, version counts, "
            f"and change history, each linked to its evidence.</p>"
            + wrap_table("Publication status", head + rows)
            + "<p class='muted'>A model's obligation status can depend on facts "
              "outside public view (market placement date, exemptions); this table "
              "records what is observable — what is published where, and when it "
              "changed. A dash under <em>Last checked</em> means no candidate "
              "location is known for that model, so nothing is fetched for it; its "
              "registry entry is re-read daily. <em>Provider copy no longer "
              "resolves</em> means the address that served the summary stopped "
              "answering, confirmed from more than one network — the model page "
              "carries the date, and every version already archived stays "
              "available with its proof.</p>")


def render_changes_page(changes) -> str:
    if changes:
        def _entry(c):
            before = (f"<a href='{esc(c['prior_link'])}'>capture of "
                      f"{esc(c['prior_date'])}</a>" if c.get("prior_link")
                      else "the previous capture")
            delta = (f" ({int(c['word_delta'])} word(s) differ in the extracted text)"
                     if c.get("word_delta") is not None else "")
            return (f"<li><strong><time datetime='{esc(c['iso'])}'>{esc(c['date'])}</time></strong> — "
                    f"{esc(c['model'])} ({esc(c['provider'])}): content changed{delta} "
                    f"between {before} and the "
                    f"<a href='{esc(c['link'])}'>capture of {esc(c['date'])}</a>. "
                    f"<span class='muted'>Dates are capture dates; the provider's "
                    f"edit occurred at or before the later capture.</span></li>")
        body_list = "<ul>" + "".join(_entry(c) for c in changes) + "</ul>"
    else:
        body_list = ("<p class='muted'>No content changes detected yet since the "
                     "archive began (11 August 2026). New entries appear here the "
                     "day a change is captured.</p>")
    feed = ("Subscribe: <a href='atom.xml'>Atom feed</a>." if SITE_URL else "")
    return (f"<h1>What changed, and when</h1>"
            f"<p>Every detected content change to a tracked summary — same "
            f"location, new content — newest first, each linking the before and "
            f"after captures so the difference can be verified byte-for-byte. "
            f"Newly discovered summaries appear on the "
            f"<a href='{PREFIX}status/'>status page</a>; differences between a "
            f"provider's live copy and third-party archived copies are noted on "
            f"the model pages. {feed}</p>"
            + body_list)


def render_dataset_page(n_versions: int, first_date: str) -> str:
    return (f"<h1>The corpus as a dataset</h1>"
            f"<p>The complete capture index is published as machine-readable JSON: "
            f"one record per stored version with provider, model, capture time, "
            f"source URL, SHA-256, size, and provenance status.</p>"
            f"<p><a href='{PREFIX}ledger.json'><strong>ledger.json</strong></a> — "
            f"{n_versions:,} records since {esc(first_date)}, regenerated on every "
            f"build. Metadata is CC0; the full corpus (raw bytes, manifests, "
            f"OpenTimestamps proofs, event log) lives in "
            + (f"<a href='{esc(REPO_URL)}'>the public repository</a>. " if REPO_URL
               else "the public repository. ")
            + f"</p>"
            f"<h2>Fields</h2>"
            f"<p class='muted'>Top-level keys: <code>generated_utc</code>, "
            f"<code>license</code>, <code>source</code> (site origin), "
            f"<code>repository</code>, <code>records</code>.</p>"
            f"<ul>"
            f"<li><code>source_id</code>, <code>provider</code>, <code>model</code>"
            f" — what the capture belongs to</li>"
            f"<li><code>kind</code> — one of "
            + ", ".join(f"<code>{esc(k)}</code>" for k in sorted(KIND_LABELS))
            + " (labels as on the site: "
            + "; ".join(f"{esc(KIND_LABELS[k])}" for k in sorted(KIND_LABELS))
            + "). "
            + ("The AI Accountability Lab's own research about a summary — "
               + ", ".join(f"<code>{esc(k)}</code>"
                           for k in sorted(cap.RESTRICTED_KINDS))
               + " — is archived here and deliberately not served: those records "
                 "carry hashes and provenance, and their <code>blob_url</code> is "
                 "null. <code>aial-archive</code> is <em>not</em> withheld: it is "
                 "the provider's own document, mirrored by the Lab, and is served "
                 "like any other provider document. A capture of the Lab's tracker "
                 "pages is withheld by address rather than by kind.")
            + "</li>"
            f"<li><code>captured_utc</code> — record-write timestamp (UTC, ISO "
            f"8601); it can trail the HTTP fetch by seconds (for fanned-out "
            f"captures, up to ~90s) — the version page's <em>Fetched</em> field "
            f"is authoritative</li>"
            f"<li><code>url</code> — the fetched location; expiring signature "
            f"tokens are masked with <code>…</code>, so masked URLs are records, "
            f"not resolvable links</li>"
            f"<li><code>sha256</code>, <code>size_bytes</code>, "
            f"<code>text_sha256</code> — content identity; <code>text_sha256</code> "
            f"is <code>null</code> when no text could be extracted</li>"
            f"<li><code>ots</code>, <code>wayback</code> — provenance status</li>"
            f"<li><code>permalink</code> — the version page serving the stored "
            f"bytes and proof (site-relative; prepend <code>source</code>)</li>"
            f"<li><code>blob_url</code>, <code>ots_url</code> — the stored bytes and "
            f"this capture's own OpenTimestamps proof (site-relative; "
            f"<code>null</code> when not served); <code>wayback_snapshot</code> — the "
            f"Wayback Machine capture recorded for it, or <code>null</code>; "
            f"<code>wayback_witnesses_capture</code> — <code>true</code> only when "
            f"the snapshot was taken within an hour of this capture; "
            f"<code>false</code> when it is an older capture the Wayback Machine "
            f"returned instead of crawling, or one saved long afterwards, neither "
            f"of which vouches for these bytes; "
            f"<code>wayback_snapshot_same_url</code> — <code>false</code> when the "
            f"snapshot archives a redirect target rather than the tracked address</li>"
            f"</ul>"
            f"<h2>Scope of the changes feed</h2>"
            f"<p>The <a href='{PREFIX}changes/'>changes feed</a> covers content "
            f"changes to already-tracked summaries (same target, new content). "
            f"Newly published summaries appear on the "
            f"<a href='{PREFIX}status/'>status page</a> when discovered.</p>"
            f"<h2>Cite this archive</h2>"
            f"<p>Cite version permalinks directly — they are stable. Suggested "
            f"form: <em>GPAI Ledger, “&lt;Model&gt; training-data summary, "
            f"version of &lt;date&gt;”, &lt;permalink URL&gt;, accessed "
            f"&lt;date&gt;.</em></p>"
            f"<p>For legal or licensing matters requiring verifiable copies of "
            f"specific captures, write to <a href='mailto:{CONTACT_EMAIL}'>"
            f"{CONTACT_EMAIL}</a>.</p>")


def render_404(site_root: str) -> str:
    return (f"<h1>Page not found</h1><p>This capture or model page does not exist (or "
            f"the URL is mistyped). The <a href='{esc(site_root)}'>"
            f"index</a> lists every tracked source, and the "
            f"<a href='{esc(site_root)}status/'>status page</a> shows every model. "
            f"If a cited permalink 404s, please report it: it should not happen.</p>")


CORRECTION_LOG = [
    ("18 Aug 2026", "An earlier revision of the Muse Spark V2→V3 analysis called the "
     "delta 'no substantive content change'. A diff filter had dropped single-word "
     "changes, hiding a training-data collection-date extension (June → July 2026). "
     "The report was corrected with a visible note and the filter removed."),
    ("19 Aug 2026", "An 11 Aug capture stored under meta/muse-spark actually "
     "contained a Google document, selected by a 17 Aug diff via path order. The "
     "mislabeled entry was retired, the correctly-labeled capture lives under "
     "google/gemini-3-pro, and diff tooling now selects captures by hash, never "
     "by path order."),
    ("20 Aug 2026", "The 14 Aug MAI Image 2 report asserted a naming anomaly was "
     "'why AIAL never found' the document — a claim about AIAL's internal process "
     "this project cannot know. Reworded to state only the observable facts."),
    ("20 Aug 2026", "The same report described MAI Cyber 1 Flash's summary as "
     "absent from AIAL's tracker and asserted Microsoft had 'renamed' it — AIAL "
     "does evaluate that model, and no rename is evidenced. Restated as "
     "observables: the summary is published under a '-Data-Card' filename that "
     "breaks the provider's own '-Data-Summary' pattern."),
]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def main(generated: str = None) -> int:
    global GENERATED, BUILD_STAMP, VDIFFS
    GENERATED = generated or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    registry = json.loads((ROOT / "crawler" / "sources.json").read_text(encoding="utf-8"))
    drift_p = ROOT / "reports" / "drift-latest.json"
    drift_notes, near_notes, history_notes = {}, {}, {}
    if drift_p.exists():
        for rrec in json.loads(drift_p.read_text(encoding="utf-8")):
            # The live-vs-archive banners describe a comparison between the
            # provider's live copy and AIAL's archived copy. A record whose basis
            # is the capture's own history is NOT that comparison, and its
            # self-history note is rendered separately below; narrating it here
            # asserted a comparison the ledger never performed. Records written
            # before `basis` existed carry the in-page branch's own wording, so
            # fall back to that rather than trusting the verdict alone.
            note = str(rrec.get("note") or "")
            live_vs_archive = (rrec.get("basis") == "live-vs-archive"
                               if rrec.get("basis")
                               else "in-page document" not in note)
            if live_vs_archive:
                if rrec.get("verdict") == "DRIFT-CANDIDATE" and "similarity" in rrec:
                    drift_notes[rrec["id"]] = rrec["similarity"]
                elif rrec.get("verdict") == "near-identical" and "similarity" in rrec:
                    near_notes[rrec["id"]] = rrec
            # the self-history note stands either way: it describes the comparison
            # that WAS performed, and for an in-page publication it is the only
            # difference the ledger can speak to
            sh = rrec.get("self_history") or {}
            if sh.get("verdict") == "changed" and sh.get("word_delta"):
                history_notes[rrec["id"]] = sh
    VDIFFS = load_version_diffs()
    state = (json.loads((DATA / "state.json").read_text(encoding="utf-8"))
             if (DATA / "state.json").exists() else {})
    # every sha still held in the corpus — used to annotate prior-capture
    # references whose capture was pruned (content survives in a neighbour)
    corpus_shas = {v["sha256"] for e in state.values() for v in e.get("versions", [])}
    SHA_FIRST.clear()
    for key, e in state.items():
        for v in e.get("versions", []):
            slug = v["dir"].replace("\\", "/").rsplit("/", 1)[-1]
            cur = SHA_FIRST.get(v["sha256"])
            if cur is None or slug < cur[0]:
                SHA_FIRST[v["sha256"]] = (slug, key.split("::", 1)[0])
    checked = last_checked_map()
    registry_ids = {s["id"] for s in registry["sources"]}

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)
    if STATIC.exists():
        shutil.copytree(STATIC, DIST / "static")

    # the footer stamp renders on every page, so it must exist BEFORE the loop
    global BUILD_STAMP
    _n_ver = sum(len(e.get("versions", [])) for e in state.values())
    _n_models = sum(1 for s in registry["sources"]
                    if s["status"] in ("published", "missing") and not s.get("retired"))
    BUILD_STAMP = (f"Build: {GENERATED} · {_n_ver} archived versions · "
                   f"{_n_models} models tracked")

    skipped = []          # (source id, version dir) whose manifest is missing
    ots_blob_written = {}  # blob name -> earliest cap_slug whose proof is served
    model_rows, other_rows, status_rows = [], [], []
    export_rows, change_entries, sitemap = [], [], []
    n_versions_total = 0
    first_capture_overall = "99999999"

    for source in registry["sources"]:
        sid = source["id"]
        versions_by_target = {k.split("::", 1)[1]: v for k, v in state.items()
                              if k.startswith(sid + "::")}
        model_dir = DIST / "ledger" / sid
        model_path = f"{PREFIX}ledger/{sid}/"

        active_slugs = {cap.target_slug(t["kind"], t["url"])
                        for t in source.get("targets", [])}
        inpage_urls = {t["url"] for t in source.get("targets", []) if t.get("inpage")}

        # pass 1: read every version's manifest, mint page dirs, copy blobs
        entries = []          # (m, cap_slug, vdir, tslug, raw_exists, ots_exists, text, raw_txt_len)
        rows_data = []
        for tslug, tstate in sorted(versions_by_target.items()):
            prior_sha = None
            prior_text_sha = None
            prior_slug = None
            prior_dir = None
            for ver in tstate.get("versions", []):
                cap_dir = DATA / ver["dir"]
                manifest_path = cap_dir / "manifest.json"
                if not manifest_path.exists():
                    # a state-listed version we cannot render: NEVER silently
                    # unpublish it — collect and fail the build at the end
                    skipped.append((sid, ver["dir"]))
                    continue
                m = json.loads(manifest_path.read_text(encoding="utf-8"))
                restricted = restriction_of(source, m)
                cap_slug = cap_dir.name
                vdir = model_dir / "v" / cap_slug
                if vdir.exists():
                    # two targets of one source captured in the same UTC second:
                    # suffix deterministically instead of overwriting
                    cap_slug = f"{cap_slug}-{tslug[-6:]}"
                    vdir = model_dir / "v" / cap_slug
                vdir.mkdir(parents=True, exist_ok=True)

                raw_src = cap_dir / m["stored_as"]
                ots_src = cap_dir / (m["stored_as"] + ".ots")
                _, blob_name, ots_blob = blob_names(m)
                blob_dir = DIST / "blob"
                blob_dir.mkdir(parents=True, exist_ok=True)
                # restricted captures (a provider objection, or a third party's
                # own work): structured facts, hashes, and the OTS proof are
                # published; the bytes and extracted text are NOT copied into the
                # site, so nothing this project may not redistribute is served
                if raw_src.exists() and not restricted \
                        and not (blob_dir / blob_name).exists():
                    shutil.copy2(raw_src, blob_dir / blob_name)
                # same-digest captures can exist under several targets/dates;
                # serve the EARLIEST capture's proof so no page implies a later
                # attestation than its own
                if ots_src.exists():
                    # the capture's own proof, under a name no other capture uses
                    shutil.copy2(ots_src, blob_dir / own_ots_name(m, cap_slug))
                    prev = ots_blob_written.get(ots_blob)
                    if prev is None or cap_slug < prev:
                        shutil.copy2(ots_src, blob_dir / ots_blob)
                        ots_blob_written[ots_blob] = cap_slug

                # read the extracted text ONCE: byte length feeds the app-shell
                # note; the decoded, newline-normalized text feeds the page body
                # (read_text's universal-newline behavior, reproduced explicitly)
                text_path = cap_dir / "extracted.txt"
                raw_txt = text_path.read_bytes() if text_path.exists() else None
                extracted_text = (raw_txt.decode("utf-8")
                                  .replace("\r\n", "\n").replace("\r", "\n")
                                  if raw_txt is not None else None)

                entries.append((m, cap_slug, vdir, extracted_text,
                                raw_src.exists(), ots_src.exists(),
                                (prior_slug, iso_date(prior_slug), prior_sha)
                                if prior_slug else None))
                pair = (VDIFFS.get((sid, tslug, prior_dir, ver["dir"]))
                        if prior_dir else None)
                row = {
                    "ts": cap_slug, "kind": m["target_kind"],
                    # a state harvested from upstream history was all fetched on one
                    # day, so the capture timestamp cannot order it for a reader
                    "upstream_date": m.get("git_commit_date"),
                    "url": m["http"]["url"], "stored_as": m["stored_as"],
                    "rendered": bool(m.get("http", {}).get("rendered")),
                    "sha": m["sha256"], "size": m["size_bytes"],
                    "retired": tstate.get("retired"),
                    "managed": tstate.get("managed"),
                    # A harvested capture has no registry target by design: its
                    # address is a pinned commit or a page discovered from one we
                    # already hold. Without this it falls into "Captures of
                    # superseded target URLs", which tells a reader the ledger
                    # once tracked that address and stopped — untrue of every one.
                    "active": (tslug in active_slugs
                               or bool(tstate.get("managed"))
                               or bool(m.get("harvested_from"))),
                    "txt_size": len(raw_txt) if raw_txt is not None else 0,
                    "prior_sha": prior_sha,
                    "text_sha": m.get("text_sha256"),
                    "prior_text_sha": prior_text_sha,
                    # the ledger's common-extractor verdict for previous -> this
                    "diff_verdict": pair["verdict"] if pair else None,
                    "diff_words": pair.get("word_delta") if pair else None,
                    "diff_moved": pair.get("moved_words") if pair else None,
                    # set when the ledger compared like for like with an earlier
                    # same-method capture because the immediate predecessor's
                    # capture method differed
                    "diff_compared_with": pair.get("compared_with") if pair else None,
                }
                rows_data.append(row)
                n_versions_total += 1
                first_capture_overall = min(first_capture_overall, cap_slug[:8])
                export_rows.append({
                    "source_id": sid,
                    # the capture's own description wins over the source it is
                    # filed under — a provider's document mirrored by AIAL is
                    # that provider's, and the dataset must say so too
                    "provider": m.get("provider") or source["provider"],
                    "model": m.get("model") or source["model"],
                    "filed_under_source": sid, "kind": m["target_kind"],
                    "captured_utc": iso_date(cap_slug) or cap_slug,
                    "url": mask_tokens(m["http"]["url"]),
                    "sha256": m["sha256"], "size_bytes": m["size_bytes"],
                    "text_sha256": m.get("text_sha256"),
                    "ots": bool((m.get("ots") or {}).get("ok")),
                    "wayback": bool((m.get("wayback") or {}).get("ok")),
                    "permalink": f"{PREFIX}ledger/{sid}/v/{cap_slug}/",
                    # where a consumer fetches the bytes and the proof without the page
                    "blob_url": (f"{PREFIX}blob/{blob_name}"
                                 if raw_src.exists() and not restricted else None),
                    "ots_url": (f"{PREFIX}blob/{own_ots_name(m, cap_slug)}"
                                if ots_src.exists() else None),
                    "wayback_snapshot": ((m.get("wayback") or {}).get("snapshot")
                                         if (m.get("wayback") or {}).get("ok") else None),
                    # true only when the snapshot was taken at about the same
                    # time as this capture; false when it is an older capture the
                    # Wayback Machine returned instead of crawling, or one saved
                    # long afterwards, neither of which vouches for these bytes
                    "wayback_witnesses_capture": wayback_witnesses(m),
                    "wayback_snapshot_same_url": (m.get("wayback") or {}).get("same_url"),
                })
                # a genuine content change feeds the /changes/ log and the Atom
                # feed. The ledger's common-extractor verdict decides whether one
                # exists for this pair; only a pair without a record falls back
                # to the stored text hashes (which an extractor change can alter)
                if pair:
                    # a pure move (a running header on another page) changes no words
                    changed = pair["verdict"] == "changed" and bool(pair.get("word_delta"))
                else:
                    changed = (row["prior_text_sha"] is not None
                               and row["text_sha"] is not None
                               and row["prior_text_sha"] != row["text_sha"])
                if changed and is_document(row, inpage_urls):
                    change_entries.append({
                        "iso": iso_date(cap_slug), "ts": cap_slug,
                        "date": human_date(cap_slug),
                        "model": source["model"], "provider": source["provider"],
                        # For an in-page publication the summary IS a page, and
                        # the extracted text carries that page's own furniture —
                        # navigation, footers, calls to action. A rename in a
                        # header is a real difference between captures, but it is
                        # not "summary content changed", and a subscriber acting
                        # on it as a filing change is being misled. Say which one
                        # the ledger can actually see.
                        "what": (("the page carrying this summary changed vs the "
                                  "previous capture (the extracted text includes "
                                  "the page's own navigation and footer, so the "
                                  "difference need not be in the summary)"
                                  if row["url"] in inpage_urls else
                                  "summary content changed vs the previous capture")
                                 + (" made with the same capture method"
                                    if pair and pair.get("compared_with") else "")
                                 + (f" ({pair['word_delta']} word(s) differ in the "
                                    f"extracted text)" if pair else "")),
                        "word_delta": pair.get("word_delta") if pair else None,
                        "link": f"{PREFIX}ledger/{sid}/v/{cap_slug}/",
                        "prior_link": (f"{PREFIX}ledger/{sid}/v/{prior_slug}/"
                                       if prior_slug else None),
                        "prior_date": (human_date(prior_slug) if prior_slug
                                       else None),
                    })
                prior_sha = m["sha256"]
                prior_text_sha = m.get("text_sha256")
                prior_slug = cap_slug
                prior_dir = ver["dir"]

        # pass 2: write version pages (needs sha_first for canonical-of-duplicate)
        sha_first_page = {}
        for m, cap_slug, vdir, _t, _r, _o, _p in sorted(entries, key=lambda e: e[1]):
            sha_first_page.setdefault(m["sha256"], cap_slug)
        # same-day captures need the UTC time in the title to stay unique
        date_counts = {}
        for _m, cap_slug, _v, _t, _r, _o, _p in entries:
            d = human_date(cap_slug)
            date_counts[d] = date_counts.get(d, 0) + 1
        for m, cap_slug, vdir, extracted_text, raw_exists, ots_exists, prior_ref in entries:
            body = render_version_page(source, m, cap_slug, corpus_shas,
                                       extracted_text, raw_exists, ots_exists,
                                       prior_ref=prior_ref)
            row = next(r for r in rows_data if r["ts"] == cap_slug
                       and r["sha"] == m["sha256"])
            doc = is_document(row, inpage_urls)
            vpath = f"{PREFIX}ledger/{sid}/v/{cap_slug}/"
            crumb_items = [("GPAI Ledger", PREFIX),
                           (f"{source['model']} ({source['provider']})", model_path),
                           (f"Capture {human_date(cap_slug)}", None)]
            robots = None
            canonical = vpath
            when = human_date(cap_slug)
            if date_counts.get(when, 0) > 1:
                tm = re.search(r"T(\d{2})(\d{2})(\d{2})", cap_slug)
                if tm:
                    when += f", {tm.group(1)}:{tm.group(2)}:{tm.group(3)} UTC"
            if not doc:
                # thin watch-surface capture: keep the permalink, keep it out of
                # the index (noindex), never combined with a canonical override
                robots = "noindex"
                title = (f"{source['model']} ({source['provider']}) — "
                         f"watched-page capture {when} | GPAI Ledger")
                desc = ""
            else:
                first_slug = sha_first_page[m["sha256"]]
                if first_slug != cap_slug:
                    # byte-identical duplicate capture: canonicalize to the first
                    canonical = f"{PREFIX}ledger/{sid}/v/{first_slug}/"
                if (m["target_kind"] in ("cop-doc", "regulatory")
                        or source["status"] not in ("published", "missing")):
                    title = (f"{source['model']} — archived document, "
                             f"{when} | GPAI Ledger")
                    desc = (f"Archived copy of {source['model']} captured "
                            f"{when}: SHA-256 {m['sha256'][:12]}…, "
                            f"{m['size_bytes']:,} bytes, OpenTimestamps-attested.")
                else:
                    title = (f"{source['model']} training data summary — version of "
                             f"{when} (SHA-256 verified) | GPAI Ledger")
                    desc = (f"Archived copy of {source['model']}'s training-data "
                            f"summary captured {when}: SHA-256 "
                            f"{m['sha256'][:12]}…, {m['size_bytes']:,} bytes, "
                            f"OpenTimestamps-attested, full extracted text.")
            write(vdir / "index.html", title, body, desc=desc,
                  canonical_path=canonical, robots=robots,
                  jsonld=[breadcrumb_jsonld(crumb_items)],
                  crumbs=crumbs_html(crumb_items))
            if doc and canonical == vpath:
                sitemap.append((vpath, iso_date(cap_slug)))

        rows_data.sort(key=lambda r: r["ts"], reverse=True)
        vsections = render_version_sections(rows_data, inpage_urls)
        if sid in drift_notes:
            vsections.insert(0, (
                f"<p><strong>Version difference observed:</strong> the newest "
                f"live copy and the archived third-party copy of this summary "
                f"currently differ in content (text similarity "
                f"{drift_notes[sid]:.2f}). The rows below serve both — compare "
                f"the extracted text of the two document versions to see the "
                f"difference.</p>"))
        elif sid in near_notes:
            nr = near_notes[sid]
            n_words, n_moved = int(nr.get("word_delta") or 0), int(nr.get("moved_words") or 0)
            what = (f"differ in {n_words} word(s) of extracted text" if n_words else
                    f"differ only in the position of {n_moved} word(s) of extracted text "
                    f"(a block moved; no words changed)")
            vsections.insert(0, (
                f"<p><strong>Small difference observed:</strong> the newest live "
                f"copy and the archived third-party copy of this summary {what} "
                f"(similarity {float(nr['similarity']):.4f}"
                + ("; the comparison re-extracted both copies with one tool, so "
                   "the archived extracts on this site — made by different "
                   "extractor eras — need not reproduce this count"
                   if nr.get("same_tool") else "")
                + "). The differing words are listed in the repository's drift "
                "report; compare the extracted text of the two document versions "
                "below.</p>"))
        if sid in history_notes:
            hn = history_notes[sid]
            f_ts = str(hn.get("from_dir", "")).rsplit("/", 1)[-1]
            t_ts = str(hn.get("to_dir", "")).rsplit("/", 1)[-1]
            vsections.insert(0, (
                f"<p><strong>Latest version differs from the previous one:</strong> "
                f"{int(hn.get('word_delta') or 0)} word(s) of extracted text changed "
                f"between the captures of {esc(human_date(f_ts))} and "
                f"{esc(human_date(t_ts))}"
                + (" (the comparison re-extracted both copies with one tool; the "
                   "archived extracts on this site need not reproduce this count)"
                   if hn.get("same_tool") else "")
                + "; both versions are archived below with their full text.</p>"))
        if source.get("retired"):
            vsections.insert(0, (
                f"<p><strong>No longer tracked:</strong> {esc(str(source['retired']))}. "
                f"The versions archived below remain, and their permalinks stay "
                f"valid.</p>"))
        for note in reversed(gone_notes(source["id"])):
            vsections.insert(0, note)
        model_body, page_title = render_model_page(source, vsections, checked)
        crumb_items = [("GPAI Ledger", PREFIX),
                       (f"{source['model']} ({source['provider']})", None)]
        write(model_dir / "index.html", page_title, model_body,
              desc=model_page_desc(source, rows_data, inpage_urls),
              canonical_path=model_path,
              jsonld=[breadcrumb_jsonld(crumb_items)],
              crumbs=crumbs_html(crumb_items))
        last_mod = max((r["ts"] for r in rows_data), default="")
        sitemap.append((model_path, iso_date(last_mod) if last_mod else None))

        row = render_index_row(source, rows_data, inpage_urls)
        if source["status"] in ("published", "missing"):
            model_rows.append(((source["provider"].lower(), source["model"].lower()), row))
            n_docs = distinct_documents(rows_data, inpage_urls)
            content_changes = [c for c in change_entries
                               if c["model"] == source["model"]
                               and c["provider"] == source["provider"]]
            last = max(content_changes, key=lambda c: c["ts"], default=None)
            last_change = last["date"] if last else "—"
            badge = (f"<strong class='tag tag-{esc(source['status'])}'>"
                     f"{esc(STATUS_LABELS.get(source['status'], source['status']))}</strong>"
                     + (" <span class='muted'>(no longer tracked)</span>"
                        if source.get("retired") else "")
                     + (" <span class='muted'>(a tracked address no longer "
                        "resolves)</span>"
                        if any(s == source["id"] for s, _t in GONE_TARGETS) else ""))
            status_rows.append((
                (source["provider"].lower(), source["model"].lower()),
                f"<tr><td>{esc(source['provider'])}</td>"
                f"<td><a href='{esc(model_path)}'>{esc(source['model'])}</a></td>"
                f"<td>{badge}</td><td class='num'>{n_docs}</td>"
                f"<td>{esc(last_change)}</td>"
                f"<td>{esc(status_checked(source, checked))}</td></tr>"))
        else:
            other_rows.append(((source["provider"].lower(), source["model"].lower()), row))

    # ---- site-level pages -------------------------------------------------
    tracked = [s for s in registry["sources"] if not s.get("retired")]
    n_published = sum(1 for s in tracked if s["status"] == "published")
    n_missing = sum(1 for s in tracked if s["status"] == "missing")
    providers = {s["provider"] for s in tracked
                 if s["status"] in ("published", "missing")}
    last_sweep = max(checked.values(), default="")[:10]
    stats = {
        "Models tracked": f"{n_published + n_missing}",
        "Providers": f"{len(providers)}",
        "Summaries published": f"{n_published}",
        "Summaries missing": f"{n_missing}",
        # this counts CAPTURES, most of which are a third party's research whose
        # content the site does not serve; calling them "archived versions" of
        # summaries overstated the corpus by an order of magnitude
        "Captures": f"{n_versions_total}",
        "Last sweep": last_sweep or "—",
    }

    index_jsonld = [{
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "name": "GPAI Ledger",
             "alternateName": "GPAI Training Data Summary Ledger",
             **({"url": SITE_URL + PREFIX} if SITE_URL else {})},
            {"@type": "Organization", "name": "GPAI Ledger",
             "description": "Independent public evidence archive of EU AI Act "
                            "Article 53(1)(d) training-data summaries, capturing "
                            "every published version on a daily schedule with "
                            "SHA-256 hashes, OpenTimestamps proofs and, where "
                            "possible, Wayback snapshots.",
             "foundingDate": "2026",
             **({"url": SITE_URL + PREFIX,
                 "logo": SITE_URL + PREFIX + "static/logo.png"} if SITE_URL else {}),
             **({"email": CONTACT_EMAIL} if "@" in CONTACT_EMAIL else {}),
             **({"sameAs": [REPO_URL]} if REPO_URL else {})},
        ],
    }]
    write(DIST / "index.html",
          "GPAI Ledger — the public record of EU AI Act training-data summaries",
          render_index(model_rows, other_rows, stats),
          desc="Daily captures of every known Article 53(1)(d) public summary "
               "of training content — every version archived with SHA-256 hashes, "
               "OpenTimestamps proofs and, where possible, Wayback snapshots.",
          canonical_path=PREFIX, jsonld=index_jsonld)
    last_evt = max(checked.values(), default="")
    sitemap.insert(0, (PREFIX, last_evt or None))

    status_body = render_status_page([r for _, r in sorted(status_rows)])
    write(DIST / "status" / "index.html",
          "GPAI publication status: who has published an AI Act training-data "
          "summary — and who hasn't",
          status_body,
          desc=f"Publication status of Article 53(1)(d) summaries across "
               f"{n_published + n_missing} tracked GPAI models: published and "
               f"missing summaries, version counts, and last content changes — "
               f"each linked to archived evidence.",
          canonical_path=f"{PREFIX}status/")
    sitemap.append((f"{PREFIX}status/", None))

    change_entries.sort(key=lambda c: c["ts"], reverse=True)
    write(DIST / "changes" / "index.html",
          "Latest changes to AI training-data summaries | GPAI Ledger",
          render_changes_page(change_entries[:100]),
          desc="Every detected content change to a tracked Article 53(1)(d) "
               "training-data summary, dated and linked to before/after archived "
               "versions.",
          canonical_path=f"{PREFIX}changes/")
    sitemap.append((f"{PREFIX}changes/", None))
    if SITE_URL:
        (DIST / "changes").mkdir(exist_ok=True)
        feed_entries = "".join(
            f"<entry><title>{esc(c['model'])} ({esc(c['provider'])}): "
            f"{esc(c['what'])}</title>"
            f"<link href='{esc(SITE_URL + c['link'])}'/>"
            f"<id>{esc(SITE_URL + c['link'])}</id>"
            f"<updated>{esc(c['iso'])}</updated>"
            f"<summary>{esc(c['model'])} — {esc(c['what'])} "
            f"({esc(c['date'])}).</summary></entry>"
            for c in change_entries[:50])
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (DIST / "changes" / "atom.xml").write_text(
            f"<?xml version='1.0' encoding='utf-8'?>\n"
            f"<feed xmlns='http://www.w3.org/2005/Atom'>"
            f"<title>GPAI Ledger — changes to training-data summaries</title>"
            f"<link href='{SITE_URL}{PREFIX}changes/'/>"
            f"<link rel='self' href='{SITE_URL}{PREFIX}changes/atom.xml'/>"
            f"<id>{SITE_URL}{PREFIX}changes/</id>"
            f"<author><name>GPAI Ledger</name></author>"
            f"<updated>{now_iso}</updated>"
            f"{feed_entries}</feed>\n", encoding="utf-8")

    write(DIST / "what-is-a-training-data-summary" / "index.html",
          "What is an Article 53(1)(d) training-data summary? | GPAI Ledger",
          render_explainer(),
          desc="The EU AI Act requires every general-purpose AI provider to publish "
               "a 'sufficiently detailed summary' of training content. What it must "
               "contain, the deadlines, the template, the penalties — and where to "
               "read every published summary.",
          canonical_path=f"{PREFIX}what-is-a-training-data-summary/")
    sitemap.append((f"{PREFIX}what-is-a-training-data-summary/", None))

    write(DIST / "deadlines" / "index.html",
          "AI Act training-data summary deadlines: 2 August 2025, 2026, 2027 | GPAI Ledger",
          render_deadlines(),
          desc="When EU AI Act training-data summaries are due: obligation from "
               "2 Aug 2025, enforcement powers from 2 Aug 2026, legacy-model "
               "deadline 2 Aug 2027, six-month update cadence, and the penalties.",
          canonical_path=f"{PREFIX}deadlines/")
    sitemap.append((f"{PREFIX}deadlines/", None))

    write(DIST / "methodology" / "index.html",
          "How the GPAI Ledger captures and proves training-data summaries",
          render_methodology(),
          desc="Daily capture, SHA-256 hashing, OpenTimestamps anchoring, Wayback "
               "snapshots, and a verify-it-yourself walkthrough: how this archive's "
               "evidence is made and checked.",
          canonical_path=f"{PREFIX}methodology/")
    sitemap.append((f"{PREFIX}methodology/", None))

    write(DIST / "about" / "index.html",
          "About the GPAI Ledger — who runs it and why",
          render_about(),
          desc="The GPAI Ledger is an independent, self-funded public-interest "
               "archive of EU AI Act training-data summaries. Who runs it, what is "
               "automated, and how to get in touch.",
          canonical_path=f"{PREFIX}about/")
    sitemap.append((f"{PREFIX}about/", None))

    write(DIST / "corrections" / "index.html",
          "Corrections — GPAI Ledger",
          render_corrections(CORRECTION_LOG),
          desc="Dated log of corrections to the GPAI Ledger's captures, labels, and "
               "analysis. Originals are never silently rewritten.",
          canonical_path=f"{PREFIX}corrections/")
    sitemap.append((f"{PREFIX}corrections/", None))

    write(DIST / "privacy" / "index.html",
          "Privacy — GPAI Ledger",
          render_privacy(),
          desc="No cookies, no analytics, no third-party requests. What the GPAI "
               "Ledger does and does not collect.",
          canonical_path=f"{PREFIX}privacy/")
    sitemap.append((f"{PREFIX}privacy/", None))

    first_date_h = (human_date(first_capture_overall + "T000000Z")
                    if first_capture_overall != "99999999" else "launch")
    dataset_jsonld = [{
        "@context": "https://schema.org", "@type": "Dataset",
        "name": "GPAI Ledger: EU AI Act Article 53(1)(d) training-data summary archive",
        "description": "Versioned corpus of the public training-data summaries that "
                       "general-purpose AI providers publish under EU AI Act "
                       "Article 53(1)(d). One record per captured version: provider, "
                       "model, capture timestamp, source URL, SHA-256 hash, size, "
                       "OpenTimestamps and Wayback provenance status, and the "
                       "permalink serving the stored bytes and proof. Captured "
                       "daily; every content change preserved.",
        "license": "https://creativecommons.org/publicdomain/zero/1.0/",
        "isAccessibleForFree": True,
        "temporalCoverage": "2026-08-11/..",
        "keywords": ["EU AI Act", "Article 53(1)(d)", "training data summary",
                     "GPAI", "public summary of training content", "transparency"],
        **({"url": SITE_URL + PREFIX + "dataset/",
            "distribution": [{"@type": "DataDownload",
                              "encodingFormat": "application/json",
                              "contentUrl": SITE_URL + PREFIX + "ledger.json"}],
            "creator": {"@type": "Organization", "name": "GPAI Ledger",
                        "url": SITE_URL + PREFIX}} if SITE_URL else {}),
        "dateModified": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }]
    write(DIST / "dataset" / "index.html",
          "The GPAI Ledger corpus as a dataset (JSON export)",
          render_dataset_page(n_versions_total, first_date_h),
          desc="The complete GPAI Ledger capture index as machine-readable JSON: "
               "one record per archived training-data-summary version with hashes "
               "and provenance status. CC0 metadata.",
          canonical_path=f"{PREFIX}dataset/", jsonld=dataset_jsonld)
    sitemap.append((f"{PREFIX}dataset/", None))

    (DIST / "ledger.json").write_text(
        json.dumps({"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "license": "CC0-1.0", "source": (SITE_URL + PREFIX) if SITE_URL else "",
                    "repository": REPO_URL,
                    "records": export_rows}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    # GitHub Pages serves 404.html AT the missing URL's path, so links must be
    # root-absolute (prefix-absolute)
    write(DIST / "404.html", "Not found — GPAI Ledger", render_404(PREFIX),
          robots="noindex")

    if SITE_URL:
        urls = []
        for path, lastmod in sitemap:
            lm = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
            urls.append(f"<url><loc>{esc(SITE_URL + path)}</loc>{lm}</url>")
        (DIST / "sitemap.xml").write_text(
            "<?xml version='1.0' encoding='UTF-8'?>\n"
            "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>\n"
            + "\n".join(urls) + "\n</urlset>\n", encoding="utf-8")
        (DIST / "robots.txt").write_text(
            f"User-agent: *\nDisallow: {PREFIX}blob/\n\n"
            f"Sitemap: {SITE_URL}{PREFIX}sitemap.xml\n", encoding="utf-8")

    if CUSTOM_DOMAIN:
        (DIST / "CNAME").write_text(CUSTOM_DOMAIN + "\n", encoding="utf-8")

    # state entries whose source id left the registry would render nowhere;
    # surface them (site/lint.py L11 fails the build on them)
    for key in sorted(state):
        if key.split("::", 1)[0] not in registry_ids:
            print(f"WARNING: state entry {key} has no registry source — it renders nowhere")

    if skipped:
        for sid, vdir_ in skipped:
            print(f"MISSING MANIFEST: {sid} {vdir_} — version cannot be rendered")
        print(f"build FAILED: {len(skipped)} state-listed version(s) lack manifests; "
              f"a version must never silently vanish from the site")
        return 1

    print(f"built {sum(1 for _ in DIST.rglob('index.html')) + 1} pages into {DIST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
