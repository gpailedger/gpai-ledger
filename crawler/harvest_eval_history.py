"""Harvest every historical state of AIAL's evaluations from their git history.

The daily sweep sees an evaluation only as it stands at 05:47. AIAL commits
several times a day and revises grades in place, so a sweep-only record loses
every intermediate state, and the grade a model was given in March becomes
unrecoverable the moment the file is edited. It also loses evaluations whose file
was renamed or deleted: 16 eval files that existed in March are gone from the
current tree, several of them fully scored.

Their repository history is public and immutable, so it is the one place the full
record survives. Each historical state is fetched from its OWN permanently
commit-pinned URL, which keeps the capture honest: we fetched it today (the OTS
proof dates our observation, not theirs) and the URL itself pins whose content it
is. The upstream commit and its date are recorded as provenance, not as a claim
that this project observed the file then.

This is a third party's research: captures are restricted (see
capture.RESTRICTED_KINDS) — held, hashed and proved, never republished.

Run after the sweep: python crawler/harvest_eval_history.py
Idempotent: a state already held is skipped without being re-fetched, so routine
runs cost a handful of API calls and nothing else. Exits non-zero only if a state
it decided to fetch could not be stored.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import capture as cap

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REGISTRY = ROOT / "crawler" / "sources.json"

REPO = "AIAccountabilityLab/gpai-training-transparency"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
EVAL_DIR = "evals"
KIND = "aial-eval-history"

# AIAL's website is SERVED from public/ in this repository, so the repository is
# the whole data surface and then some: it carries the rendered pages (including
# models and grade rounds no longer on the live site), the archived provider
# documents (including ones since deleted), and conf/config.json - the scoring
# model itself, as data rather than as prose on a methodology page.
#
# Order matters: the first group whose prefix matches owns the path, so the two
# public/ subtrees must be listed before public/ itself.
GROUPS = (
    {"prefix": "evals/", "deep": False, "exts": (".yaml", ".yml"),
     "kind": KIND, "place": "evaluation"},
    {"prefix": "public/evals/", "deep": True, "exts": (".html",),
     "kind": "aial-eval-page", "place": "slug"},
    {"prefix": "public/archive/", "deep": True,
     "exts": (".pdf", ".zip", ".docx", ".doc"),
     "kind": "aial-archive", "place": "archive", "only_new_names": True},
    # AIAL's own framework pages. Named explicitly, because until May 2026 the
    # per-model evaluations ALSO lived at public/<model>.html, and a rule that
    # took every top-level page for the framework filed an evaluation of GPT-5
    # under "AIAL scoring framework".
    {"prefix": "public/", "deep": False, "exts": (".html",),
     "names": ("about.html", "detailed-overview.html", "index.html",
               "list_summaries.html", "methodology.html", "recommendations.html"),
     "kind": "aial-method", "place": "aial"},
    # anything else at the top level is a per-model page from that earlier layout
    {"prefix": "public/", "deep": False, "exts": (".html",),
     "kind": "aial-eval-page", "place": "flat"},
    {"prefix": "conf/", "deep": False, "exts": (".json",),
     "kind": "aial-method", "place": "aial"},
)
HARVEST_KINDS = {g["kind"] for g in GROUPS}
# the top-level paths whose commits are worth walking; everything else in the
# repository is application code and presentation, not the record
WATCH_PATHS = ("evals", "public", "conf")


def group_for(path: str):
    """Which group harvests this repository path, or None to ignore it.

    Static assets - 248 files of CSS, JavaScript and logos - are deliberately not
    a group: they are how the site looks, not what it says."""
    for g in GROUPS:
        if not path.startswith(g["prefix"]):
            continue
        if not path.lower().endswith(g["exts"]):
            continue
        if not g["deep"] and "/" in path[len(g["prefix"]):]:
            continue
        if g.get("names") and path.rsplit("/", 1)[-1] not in g["names"]:
            continue
        return g
    return None
# where a history state lands when its model cannot be resolved — AIAL's own
# source, which is where an artifact of theirs belongs when it names no model we
# track (a deleted eval for a model the ledger never had)
FALLBACK_SOURCE = "aial/tracker"

# a run harvests at most this many states, so a first backfill cannot run
# unbounded inside a scheduled job; the rest are picked up next run
MAX_PER_RUN = int(os.environ.get("GPAI_EVAL_HISTORY_MAX", "400") or "400")
# ...and a count is not a clock. Each state costs a fetch plus a timestamp
# submission, so a large backfill under a step timeout would be KILLED rather
# than stopping cleanly, leaving the run red and the work half done. Stop early
# and leave the rest for tomorrow: the harvest is idempotent by design.
BUDGET_S = float(os.environ.get("GPAI_EVAL_HISTORY_BUDGET_S", "1200") or "1200")
# GitHub's raw host is robust, but this project does not hammer anyone
PAUSE_S = float(os.environ.get("GPAI_EVAL_HISTORY_PAUSE_S", "0.3") or "0.3")


def token() -> str:
    """A GitHub token, or "" for anonymous access. Anonymous is rate-limited to
    60 requests/hour, which is below the cost of a first backfill, so CI passes
    github.token and a developer falls back to the gh CLI's own credential."""
    for var in ("GPAI_GH_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=20)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def api(path: str, tok: str):
    req = urllib.request.Request(
        f"{API}/{path}",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": cap.USER_AGENT,
                 **({"Authorization": f"Bearer {tok}"} if tok else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def commits(tok: str) -> list:
    """Every commit touching anything the harvest tracks, oldest first.

    Asked per tracked top-level path and unioned, rather than listing every
    commit in the repository: the application code changes far more often than
    the record does, and each extra commit costs a tree walk."""
    seen, out = set(), []
    for tracked in WATCH_PATHS:
        page = 1
        while True:
            batch = api(f"repos/{REPO}/commits?path={tracked}&per_page=100"
                        f"&page={page}", tok)
            for c in batch:
                if c["sha"] not in seen:
                    seen.add(c["sha"])
                    out.append(c)
            if len(batch) < 100:
                break
            page += 1
            if page > 50:        # a runaway guard, not a real bound
                break
    return sorted(out, key=lambda c: c["commit"]["author"]["date"])


def tree_at(sha: str, tok: str) -> dict:
    """path -> blob sha for everything the harvest tracks at one commit.

    A commit that predates the directory genuinely has no tree there (404) and
    that is normal. ANY other failure — a rate limit, a 5xx, a dropped
    connection — must NOT be read as "no evaluations existed at this commit":
    that silently drops states, and worse, re-dates the ones that follow, because
    plan() records the first commit at which it SEES a state. A grade would be
    published as having stood from a date months after it really did."""
    try:
        t = api(f"repos/{REPO}/git/trees/{sha}?recursive=1", tok)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise
    except (urllib.error.URLError, ValueError) as exc:
        raise RuntimeError(f"tree lookup failed for {sha[:8]}: {exc!r}") from exc
    if t.get("truncated"):
        # a partial tree is indistinguishable from "these files did not exist
        # yet", which is the same silent mis-dating the note above describes
        raise RuntimeError(f"tree for {sha[:8]} came back truncated - refusing to "
                           f"read a partial tree as the state of the repository")
    return {e["path"]: e["sha"] for e in t.get("tree", [])
            if e.get("type") == "blob" and group_for(e["path"])}


def plan(tok: str) -> list:
    """Every distinct (file, content) state ever committed, chronologically.

    Keyed on the blob sha, so a file rewritten to a value it already held is not
    harvested twice, and the FIRST commit that introduced each state is the one
    recorded — that is the date the grade began to stand."""
    seen, out = set(), []
    for c in commits(tok):
        sha, when = c["sha"], c["commit"]["author"]["date"]
        for path, blob in sorted(tree_at(sha, tok).items()):
            if (path, blob) in seen:
                continue
            seen.add((path, blob))
            out.append({"path": path, "blob": blob, "commit": sha, "date": when,
                        "group": group_for(path)})
    return out


def held(store: cap.Store) -> set:
    """Blob shas already stored. Read from the manifests rather than from state,
    so a state whose capture dir was pruned is harvested again rather than
    silently treated as held."""
    done = set()
    for m in (DATA / "captures").glob("*/*/*/manifest.json"):
        try:
            j = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if j.get("target_kind") in HARVEST_KINDS and j.get("git_blob_sha"):
            # keyed with the path: two evaluations can hold identical bytes (a
            # freshly added file is often a copy of the template, and a rename
            # lands the same blob under a new name). Keying on the blob alone
            # would silently drop the second file's entire history.
            done.add((j.get("git_path", ""), j["git_blob_sha"]))
    return done


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _norm_url(u: str) -> str:
    """A document URL reduced to what identifies it: scheme and query dropped,
    because providers serve the same file with rotating signatures and tracking
    parameters, and a trailing slash means nothing here."""
    u = (u or "").split("?")[0].split("#")[0].strip().lower()
    for pre in ("https://", "http://"):
        if u.startswith(pre):
            u = u[len(pre):]
    return u.rstrip("/")


def registry_index() -> dict:
    """(organization, model_name) -> source id, normalised for matching, plus the
    eval filename each source already tracks so a renamed file still resolves."""
    if not REGISTRY.exists():
        return {}
    srcs = json.loads(REGISTRY.read_text(encoding="utf-8"))["sources"]
    idx = {}
    for s in srcs:
        idx[(_norm(s.get("provider")), _norm(s.get("model")))] = s["id"]
        # a model renamed upstream is carried in the registry as "Old / New"
        # (Anthropic's "Claude Mythos 5 / Claude Fable 5"); each side on its own
        # is an exact name this project claims for that source, so matching one
        # is a match, not a guess
        for alias in str(s.get("model") or "").split("/"):
            if alias.strip():
                idx.setdefault((_norm(s.get("provider")), _norm(alias)), s["id"])
        for t in s.get("targets", []):
            if t.get("kind") == "aial-eval":
                idx[("file", t["url"].rsplit("/", 1)[-1].lower())] = s["id"]
            # the DOCUMENT an evaluation points at identifies the model even when
            # the model has been renamed on both sides (AIAL's "Sintesi" is the
            # ledger's "FastwebMIIA"); a name match cannot see that, a URL can
            elif t.get("kind") in ("provider-live", "provider-page", "aial-archive"):
                idx.setdefault(("url", _norm_url(t["url"])), s["id"])
    return idx


def resolve(text: str, filename: str, idx: dict) -> str:
    """Which ledger source this historical evaluation belongs to.

    Matched on what the file SAYS it is (organization + model_name), because the
    filename is the thing that changed under renames. Falls back to the filename,
    then to AIAL's own source — never guesses a neighbouring model."""
    def field(name):
        return _field(text, name)
    hit = idx.get((_norm(field("organization")), _norm(field("model_name"))))
    # then the document it graded: survives a rename on either side
    hit = hit or idx.get(("url", _norm_url(field("public_summary_link"))))
    return hit or idx.get(("file", filename.lower())) or FALLBACK_SOURCE


def _field(text: str, name: str) -> str:
    for line in (text or "").splitlines():
        if line.startswith(name + ":"):
            return line.split(":", 1)[1].strip().strip('"').strip()
    return ""


def _identity(text: str, filename: str) -> tuple:
    """What the evaluation says it is: the provider and model it assesses. Used
    when the ledger cannot place it, so the capture still describes itself."""
    return (_field(text, "organization") or "AI Accountability Lab (AIAL)",
            _field(text, "model_name") or filename.rsplit(".", 1)[0])


def _fold(name: str) -> str:
    """An archive filename as a lookup key: AIAL writes the same document as
    Nova_2_Lite_2026_08_03.pdf and nova_2_lite_2026_08_03.pdf, and a
    case-sensitive miss used to file a provider's filing under AIAL's own name."""
    return "".join(c for c in (name or "").lower() if c.isalnum())


def owners_by_hash() -> dict:
    """sha256 -> (source_id, provider, model) for every capture already filed
    under a real source. An archived copy of a provider's document is the SAME
    BYTES as the provider's own filing, so the corpus itself identifies the owner
    when a filename lookup cannot."""
    out = {}
    for mp in (DATA / "captures").glob("*/*/*/manifest.json"):
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if m.get("source_id") and m["source_id"] != FALLBACK_SOURCE and m.get("sha256"):
            out.setdefault(m["sha256"], (m["source_id"], m.get("provider"),
                                         m.get("model")))
    return out


def place(state, text, idx, by_id, archive_owners, raw=None, by_hash=None) -> tuple:
    """(source_id, provider, model) for one harvested state.

    Filing and description are separate decisions. A state the ledger cannot
    place is filed under AIAL's own source, but it must still describe what it
    IS - an evaluation of Claude Fable 5 filed there is not a capture of AIAL's
    tracker - so the provider and model never come from the filing cabinet.
    AIAL's own framework pages are the exception: those really are theirs."""
    g = state["group"]
    name = state["path"].rsplit("/", 1)[-1]
    tracker = by_id.get(FALLBACK_SOURCE) or {}
    if g["place"] == "evaluation":
        sid = resolve(text or "", name, idx)
        own = _identity(text or "", name)
    elif g["place"] == "flat":
        # public/<model>.html, AIAL's layout before evaluations moved into
        # public/evals/<model>/ — the stem is the same slug
        slug = name.rsplit(".", 1)[0]
        sid = idx.get(("file", slug.lower() + ".yaml")) or FALLBACK_SOURCE
        own = (tracker.get("provider") or "AI Accountability Lab (AIAL)", slug)
    elif g["place"] == "slug":
        # public/evals/<slug>/... - the slug is the evaluation file's stem, which
        # the registry already keys every AIAL evaluation target by
        slug = state["path"][len(g["prefix"]):].split("/")[0]
        sid = idx.get(("file", slug.lower() + ".yaml")) or FALLBACK_SOURCE
        own = (tracker.get("provider") or "AI Accountability Lab (AIAL)", slug)
    elif g["place"] == "archive":
        owner = archive_owners.get(name) or archive_owners.get(_fold(name))
        if not owner and raw is not None and by_hash:
            # the same bytes filed under a real source identify the owner
            hit = by_hash.get(cap.sha256_hex(raw))
            if hit:
                owner = hit
        sid = owner[0] if owner else FALLBACK_SOURCE
        # This is a PROVIDER's mandated document that AIAL mirrors. Inheriting the
        # tracker's provider and model published five companies' Article 53
        # filings as "AI Accountability Lab (AIAL) — GPAI Training Transparency
        # tracker". When the owner cannot be established, say so; never borrow
        # the identity of the source it happens to be filed under.
        own = ((owner[1], owner[2]) if owner
               else ("provider not identified by this project",
                     name.rsplit(".", 1)[0]))
    else:                       # AIAL's own framework pages and scoring config
        return (FALLBACK_SOURCE,
                tracker.get("provider") or "AI Accountability Lab (AIAL)",
                tracker.get("model") or "GPAI Training Transparency tracker")
    src = by_id.get(sid) if sid != FALLBACK_SOURCE else None
    if src:
        return sid, src.get("provider") or own[0], src.get("model") or own[1]
    return sid, own[0], own[1]


def newest_upstream_dates() -> dict:
    """git_path -> the newest upstream date already stored for that file.

    Keyed on the PATH, not on (source, target). A state's source id is resolved
    from the file's own contents, so an upstream model_name edit moves the next
    state to a different source — and a guard keyed on the source it landed in
    would not see the newer state already held elsewhere, and would append an
    older one after it. prior_sha256 would then assert a succession that never
    happened, which is the one thing this record must never do."""
    out = {}
    for mp in (DATA / "captures").glob("*/*/*/manifest.json"):
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        path, when = m.get("git_path"), m.get("git_commit_date")
        if path and when:
            out[path] = max(out.get(path, ""), str(when))
    return out


def pinned_url(commit: str, path: str) -> str:
    # a path can contain a space ("evals/inkling small.yaml"); the separators are
    # structure, so they must survive quoting
    return f"{RAW}/{REPO}/{commit}/{urllib.parse.quote(path, safe='/')}"


def identity_url(path: str) -> str:
    """The stable identity a file's history is keyed on: its main-branch URL. Every
    state of one file is a version of ONE target, not a target of its own."""
    return f"{RAW}/{REPO}/main/{urllib.parse.quote(path, safe='/')}"


VERSION_KIND = "aial-eval-page"
VERSION_RE = None            # compiled on first use; see version_links()
MAX_VERSION_PAGES = int(os.environ.get("GPAI_EVAL_VERSION_MAX", "60") or "60")
# the sweep has a per-host circuit breaker for exactly this; so does this loop
HOST_FAILURES_BEFORE_STOP = 3

ARCHIVE_BASE = "https://aial.ie/research/gpai-training-transparency/archive/"
ARCHIVE_KIND = "aial-archive"
MAX_ARCHIVES = int(os.environ.get("GPAI_EVAL_ARCHIVE_MAX", "40") or "40")


def version_links(html: str, page_url: str) -> list:
    """Absolute URLs of the earlier-version pages an evaluation page links to.

    AIAL keys these on the evaluation DATE, so they expose one page per date the
    assessment carries — not per revision. A score edited in place under an
    unchanged evaluation_date (grok-4.5 went 814 -> 809 on 5 Aug 2026 while its
    date stayed 20 Jul) never gets a page here, which is exactly why the git
    history above is harvested as well and is the more complete record."""
    global VERSION_RE
    import re as _re
    if VERSION_RE is None:
        # AIAL emits these as BARE relative hrefs - href="version-2026-03-30",
        # no leading slash and no trailing one. Requiring "/version-" meant
        # this matched nothing, ever: zero version pages have been captured
        # since it was written, and the run said nothing because the summary
        # line was suppressed whenever the list came back empty.
        VERSION_RE = _re.compile(
            r'href=["\']([^"\']*version-[0-9]{4}-[0-9]{2}-[0-9]{2}/?)["\']')
    out = []
    for href in VERSION_RE.findall(html or ""):
        url = urllib.parse.urljoin(page_url, href)
        if url.startswith("https://aial.ie/") and url not in out:
            out.append(url)
    return out


def _latest_pages(kind: str) -> dict:
    """(source_id, url) -> newest stored capture dir, for one target kind."""
    best = {}
    for m in (DATA / "captures").glob("*/*/*/manifest.json"):
        try:
            j = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if j.get("target_kind") != kind:
            continue
        key = (j.get("source_id"), j.get("http", {}).get("url"))
        if key[1] and (key not in best or m.parent.name > best[key][0]):
            best[key] = (m.parent.name, m.parent, j)
    return best


def harvest_version_pages(store, started=None) -> tuple:
    """Capture the earlier-version pages AIAL publishes for each evaluation.

    Discovery costs nothing: the links are read out of evaluation pages the sweep
    has ALREADY captured, so in steady state this fetches only pages that appeared
    since the last run. A page here is immutable once published, so one capture is
    the whole story and it is never re-fetched."""
    pages = _latest_pages(VERSION_KIND)
    held_urls = {u for (_sid, u) in pages}
    wanted, queued = [], set()
    for (sid, page_url), (_ts, cdir, mani) in sorted(pages.items()):
        raw = cdir / str(mani.get("stored_as") or "raw.html")
        if not raw.exists():
            continue
        html = raw.read_bytes().decode("utf-8", errors="replace")
        for url in version_links(html, page_url):
            if url not in held_urls and url not in queued:
                queued.add(url)
                wanted.append((sid, url))
    stored = errors = 0
    # Say the count even when it is zero. A silent nothing is how a regex that
    # matched no href for its whole life went unnoticed.
    print(f"harvest_eval_history: {len(wanted)} version page(s) to fetch",
          flush=True)
    started = time.monotonic() if started is None else started
    consecutive = 0
    for i, (sid, url) in enumerate(wanted[:MAX_VERSION_PAGES]):
        # this is the module's only traffic to a third party's own server, and
        # the loop most likely to meet an outage: pace it, stop on the clock, and
        # give up on the host rather than hammering it while it is down
        if time.monotonic() - started > BUDGET_S:
            print(f"  budget reached — {len(wanted) - i} version page(s) left "
                  f"for the next run", flush=True)
            break
        if consecutive >= HOST_FAILURES_BEFORE_STOP:
            print(f"  aial.ie unreachable ({consecutive} consecutive failures) — "
                  f"stopping; the rest are fetched on the next run", flush=True)
            break
        if i:
            time.sleep(PAUSE_S)
        try:
            raw, meta = cap.fetch(url)
            consecutive = 0
        except cap.PermanentFetchError as exc:
            # AIAL links a few version pages that do not resolve. That is a fact
            # about their site, not a failure of this harvest; counting it makes
            # the daily run red forever over a page nobody can fetch.
            print(f"  GONE   version page {url} — {exc.status_code}: linked by an "
                  f"evaluation page but not published", flush=True)
            consecutive = 0
            continue
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ERROR version page {url}: {exc!r}", flush=True)
            errors += 1
            consecutive += 1
            continue
        ext = cap.guess_ext(meta.get("content_type", ""), url, raw)
        text, notes = cap.extract_text(raw, ext)
        tslug = cap.target_slug(VERSION_KIND, url)
        if cap.sha256_hex(raw) == store.last_sha(sid, tslug):
            continue
        cap.store_new_version(
            store, source_id=sid, provider="AI Accountability Lab (AIAL)",
            model=sid.split("/")[-1], kind=VERSION_KIND, tslug=tslug,
            event_url=url, raw=raw, meta=meta, ext=ext, text=text, notes=notes,
            text_sha=cap.canonical_text_sha(text) if text else None,
            wayback_url=None,
            manifest_extra={
                "aial_published_version": url.rstrip("/").rsplit("version-", 1)[-1],
                "harvested_from": "an evaluation page already captured"})
        stored += 1
        print(f"  NEW  {sid} published version page {url}", flush=True)
    left = max(0, len(wanted) - MAX_VERSION_PAGES)
    if wanted or stored:
        print(f"harvest_eval_history: version pages — stored {stored}, "
              f"errors {errors}, {left} left for the next run")
    return stored, errors


def named_archives() -> dict:
    """archive_file_name -> (source_id, provider, model) for every snapshot any
    evaluation has EVER named, current or historical.

    An evaluation names the write-once copy of the provider's document that was
    graded. When AIAL re-grades against a newer document the old filename stops
    being referenced by the current file, but the document it names is the one a
    past grade was given to — and, for a document the provider has since
    replaced, may be the only surviving copy. The registry only ever carries the
    CURRENT filename, so these are reachable from the harvested history alone."""
    out = {}
    for mp in sorted((DATA / "captures").glob("*/*/*/manifest.json")):
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if m.get("target_kind") not in ("aial-eval", KIND):
            continue
        tp = mp.parent / "extracted.txt"
        if not tp.exists():
            continue
        name = _field(tp.read_text(encoding="utf-8"), "archive_file_name")
        # AIAL's field is free text and has carried a bare slug ("gpt-5-5") and a
        # provider CDN string ("...docx-fc25014a") as well as real filenames.
        # Only something that ends in a document extension is an address in their
        # archive; the rest would 404 every run forever.
        if name and name.lower().endswith((".pdf", ".zip", ".docx", ".doc")):
            out.setdefault(name, (m.get("source_id"), m.get("provider"),
                                  m.get("model")))
    return out


def held_archive_names() -> set:
    out = set()
    for mp in (DATA / "captures").glob("*/*/*/manifest.json"):
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if m.get("target_kind") == ARCHIVE_KIND:
            out.add(urllib.parse.unquote(
                (m.get("http") or {}).get("url", "")).rsplit("/", 1)[-1])
    return out


def harvest_named_archives(store, started=None) -> tuple:
    """Fetch every graded snapshot an evaluation names that the ledger does not
    already hold. These are the PROVIDERS' own documents that AIAL mirrors, so
    they are archived on the same terms as the rest of the corpus and are not
    withheld — unlike AIAL's assessment of them."""
    # held_archive_names() globs every manifest in the corpus; calling it inside
    # the comprehension re-scanned 1,640 manifests once per candidate and cost
    # ~55s of the daily run for an answer that does not change mid-loop
    have = held_archive_names()
    wanted = [(n, meta) for n, meta in sorted(named_archives().items())
              if n not in have]
    if not wanted:
        return 0, 0
    started = time.monotonic() if started is None else started
    stored = errors = consecutive = 0
    for i, (name, (sid, provider, model)) in enumerate(wanted[:MAX_ARCHIVES]):
        if time.monotonic() - started > BUDGET_S:
            print(f"  budget reached — {len(wanted) - i} graded snapshot(s) left "
                  f"for the next run", flush=True)
            break
        if consecutive >= HOST_FAILURES_BEFORE_STOP:
            print(f"  aial.ie unreachable ({consecutive} consecutive failures) — "
                  f"stopping; the rest are fetched on the next run", flush=True)
            break
        if i:
            time.sleep(PAUSE_S)
        url = ARCHIVE_BASE + urllib.parse.quote(name)
        try:
            raw, meta = cap.fetch(url)
            consecutive = 0
        except cap.PermanentFetchError as exc:
            # AIAL never published a file under this name, or withdrew it. That
            # is a fact about their archive, not a failure of this harvest: count
            # it as an error and the daily run is red forever over a document
            # nobody can fetch.
            print(f"  GONE   graded snapshot {name} — {exc.status_code}: named by "
                  f"an evaluation but not in AIAL's archive", flush=True)
            consecutive = 0
            continue
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ERROR graded snapshot {name}: {exc!r}", flush=True)
            errors += 1
            consecutive += 1
            continue
        ext = cap.guess_ext(meta.get("content_type", ""), url, raw)
        text, notes = cap.extract_text(raw, ext)
        tslug = cap.target_slug(ARCHIVE_KIND, url)
        if cap.sha256_hex(raw) == store.last_sha(sid, tslug):
            continue
        cap.store_new_version(
            store, source_id=sid, provider=provider, model=model,
            kind=ARCHIVE_KIND, tslug=tslug, event_url=url, raw=raw, meta=meta,
            ext=ext, text=text, notes=notes,
            text_sha=cap.canonical_text_sha(text) if text else None,
            wayback_url=url,
            manifest_extra={"harvested_from": "a filename named by an evaluation",
                            "aial_archive_file_name": name})
        stored += 1
        print(f"  NEW  {sid} graded snapshot {name}", flush=True)
    # "left" must count what was not attempted, not what the cap would have
    # allowed: the two lines used to disagree with each other on one run
    left = max(0, len(wanted) - i - 1) if wanted else 0
    print(f"harvest_eval_history: graded snapshots — stored {stored}, "
          f"errors {errors}, {left} left for the next run")
    return stored, errors


def main() -> int:
    tok = token()
    if not tok:
        print("harvest_eval_history: no GitHub token — anonymous API limits make a "
              "backfill unreliable; set GITHUB_TOKEN or run `gh auth login`")
    store = cap.Store(DATA)
    idx = registry_index()
    by_id = {x["id"]: x for x in
             (json.loads(REGISTRY.read_text(encoding="utf-8"))["sources"]
              if REGISTRY.exists() else [])}
    done = held(store)
    newest_by_path = newest_upstream_dates()
    archive_owners = named_archives()
    for key in list(archive_owners):
        archive_owners.setdefault(_fold(key), archive_owners[key])
    by_hash = owners_by_hash()
    have_names = held_archive_names()
    todo = []
    for s in plan(tok):
        if (s["path"], s["blob"]) not in done:
            # A provider document the ledger already holds under its own address
            # does not need a second copy from the repository: the bytes are the
            # same and the archive is large. Only a document we hold NOWHERE -
            # the ones AIAL has since deleted - is worth the fetch.
            if s["group"].get("only_new_names") and \
                    s["path"].rsplit("/", 1)[-1] in have_names:
                continue
            todo.append(s)
    print(f"harvest_eval_history: {len(done)} state(s) already held, "
          f"{len(todo)} to fetch", flush=True)
    stored = errors = 0
    started = time.monotonic()
    attempted = 0
    for state in todo[:MAX_PER_RUN]:
        if time.monotonic() - started > BUDGET_S:
            print(f"  budget reached after {attempted} state(s) — the rest are "
                  f"harvested on the next run", flush=True)
            break
        attempted += 1
        if attempted > 1:
            time.sleep(PAUSE_S)
        url = pinned_url(state["commit"], state["path"])
        try:
            raw, meta = cap.fetch(url)
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ERROR {state['path']}@{state['commit'][:8]}: {exc!r}",
                  flush=True)
            errors += 1
            continue
        ext = cap.guess_ext(meta.get("content_type", ""), url, raw)
        text, notes = cap.extract_text(raw, ext)
        kind = state["group"]["kind"]
        sid, provider, model = place(state, text, idx, by_id, archive_owners,
                                     raw=raw, by_hash=by_hash)
        tslug = cap.target_slug(kind, identity_url(state["path"]))
        if cap.sha256_hex(raw) == store.last_sha(sid, tslug):
            continue          # unchanged from the state already stored for it
        # States are harvested oldest-first, so a state OLDER than the newest one
        # already stored means an earlier run skipped it (a transient failure).
        # Appending it now would record it as following a state it preceded, and
        # prior_sha256 would assert a succession that never happened. Refuse, and
        # say so: a gap that is reported can be repaired, a false chain cannot.
        newest = newest_by_path.get(state["path"], "")
        if newest and state["date"] < newest:
            print(f"  SKIP   {state['path']} @{state['commit'][:8]} "
                  f"({state['date'][:10]}) predates the newest state held for "
                  f"this file ({newest[:10]}) — storing it would imply a "
                  f"succession that did not happen; re-harvest this target from "
                  f"scratch to repair the gap", flush=True)
            errors += 1
            continue
        cap.store_new_version(
            store, source_id=sid, provider=provider, model=model,
            kind=kind, tslug=tslug, event_url=url, raw=raw, meta=meta, ext=ext,
            text=text, notes=notes,
            text_sha=cap.canonical_text_sha(text) if text else None,
            # GitHub's raw host is not the Wayback Machine's job, and the commit
            # URL is already immutable: a snapshot would add nothing
            wayback_url=None,
            manifest_extra={"git_commit": state["commit"],
                            "git_commit_date": state["date"],
                            "git_blob_sha": state["blob"],
                            "git_path": state["path"],
                            # this is provenance from the upstream repository, not
                            # an observation this project made at that date
                            "harvested_from": "upstream git history"},
            event_extra={"git_commit": state["commit"],
                         "git_commit_date": state["date"]})
        stored += 1
        newest_by_path[state["path"]] = max(newest_by_path.get(state["path"], ""),
                                            state["date"])
        print(f"  NEW  {sid} {state['path']} @{state['commit'][:8]} "
              f"({state['date'][:10]})", flush=True)
    left = max(0, len(todo) - attempted)
    print(f"harvest_eval_history: stored {stored}, errors {errors}, "
          f"{left} left for the next run")
    _vstored, verrors = harvest_version_pages(store, started=started)
    _astored, aerrors = harvest_named_archives(store, started=started)
    return 1 if (errors or verrors or aerrors) else 0


if __name__ == "__main__":
    sys.exit(main())
