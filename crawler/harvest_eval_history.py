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
    """Every commit touching evals/, oldest first."""
    out, page = [], 1
    while True:
        batch = api(f"repos/{REPO}/commits?path={EVAL_DIR}&per_page=100&page={page}", tok)
        out += batch
        if len(batch) < 100:
            break
        page += 1
        if page > 50:            # a runaway guard, not a real bound
            break
    return sorted(out, key=lambda c: c["commit"]["author"]["date"])


def tree_at(sha: str, tok: str) -> dict:
    """filename -> blob sha for evals/ at one commit.

    A commit that predates the directory genuinely has no tree there (404) and
    that is normal. ANY other failure — a rate limit, a 5xx, a dropped
    connection — must NOT be read as "no evaluations existed at this commit":
    that silently drops states, and worse, re-dates the ones that follow, because
    plan() records the first commit at which it SEES a state. A grade would be
    published as having stood from a date months after it really did."""
    try:
        t = api(f"repos/{REPO}/git/trees/{sha}:{EVAL_DIR}", tok)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise
    except (urllib.error.URLError, ValueError) as exc:
        raise RuntimeError(f"tree lookup failed for {sha[:8]}: {exc!r}") from exc
    return {e["path"]: e["sha"] for e in t.get("tree", [])
            if e.get("type") == "blob" and e["path"].lower().endswith((".yaml", ".yml"))}


def plan(tok: str) -> list:
    """Every distinct (file, content) state ever committed, chronologically.

    Keyed on the blob sha, so a file rewritten to a value it already held is not
    harvested twice, and the FIRST commit that introduced each state is the one
    recorded — that is the date the grade began to stand."""
    seen, out = set(), []
    for c in commits(tok):
        sha, when = c["sha"], c["commit"]["author"]["date"]
        for name, blob in sorted(tree_at(sha, tok).items()):
            if (name, blob) in seen:
                continue
            seen.add((name, blob))
            out.append({"file": name, "blob": blob, "commit": sha, "date": when})
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
        if j.get("target_kind") == KIND and j.get("git_blob_sha"):
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
        for line in text.splitlines():
            if line.startswith(name + ":"):
                return line.split(":", 1)[1].strip().strip('"').strip()
        return ""
    hit = idx.get((_norm(field("organization")), _norm(field("model_name"))))
    # then the document it graded: survives a rename on either side
    hit = hit or idx.get(("url", _norm_url(field("public_summary_link"))))
    return hit or idx.get(("file", filename.lower())) or FALLBACK_SOURCE


def pinned_url(commit: str, filename: str) -> str:
    # a filename can contain a space ("inkling small.yaml"); quote the path only
    return f"{RAW}/{REPO}/{commit}/{EVAL_DIR}/{urllib.parse.quote(filename)}"


def identity_url(filename: str) -> str:
    """The stable identity a file's history is keyed on: its main-branch URL. Every
    state of one evaluation is a version of ONE target, not a target of its own."""
    return f"{RAW}/{REPO}/main/{EVAL_DIR}/{urllib.parse.quote(filename)}"


VERSION_KIND = "aial-eval-page"
VERSION_RE = None            # compiled on first use; see version_links()
MAX_VERSION_PAGES = int(os.environ.get("GPAI_EVAL_VERSION_MAX", "60") or "60")


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
        VERSION_RE = _re.compile(r'href=["\']([^"\']*?/version-[0-9]{4}-[0-9]{2}-[0-9]{2}/?)["\']')
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


def harvest_version_pages(store) -> tuple:
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
    for sid, url in wanted[:MAX_VERSION_PAGES]:
        try:
            raw, meta = cap.fetch(url)
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ERROR version page {url}: {exc!r}", flush=True)
            errors += 1
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
    todo = [s for s in plan(tok)
            if (f"{EVAL_DIR}/{s['file']}", s["blob"]) not in done]
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
        url = pinned_url(state["commit"], state["file"])
        try:
            raw, meta = cap.fetch(url)
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ERROR {state['file']}@{state['commit'][:8]}: {exc!r}", flush=True)
            errors += 1
            continue
        ext = cap.guess_ext(meta.get("content_type", ""), url, raw)
        text, notes = cap.extract_text(raw, ext)
        sid = resolve(text or "", state["file"], idx)
        tslug = cap.target_slug(KIND, identity_url(state["file"]))
        if cap.sha256_hex(raw) == store.last_sha(sid, tslug):
            continue          # unchanged from the state already stored for it
        src = by_id.get(sid)
        cap.store_new_version(
            store, source_id=sid,
            provider=(src or {}).get("provider", "AI Accountability Lab"),
            model=(src or {}).get("model", state["file"].rsplit(".", 1)[0]),
            kind=KIND, tslug=tslug, event_url=url, raw=raw, meta=meta, ext=ext,
            text=text, notes=notes,
            text_sha=cap.canonical_text_sha(text) if text else None,
            # GitHub's raw host is not the Wayback Machine's job, and the commit
            # URL is already immutable: a snapshot would add nothing
            wayback_url=None,
            manifest_extra={"git_commit": state["commit"],
                            "git_commit_date": state["date"],
                            "git_blob_sha": state["blob"],
                            "git_path": f"{EVAL_DIR}/{state['file']}",
                            # this is provenance from the upstream repository, not
                            # an observation this project made at that date
                            "harvested_from": "upstream git history"},
            event_extra={"git_commit": state["commit"],
                         "git_commit_date": state["date"]})
        stored += 1
        print(f"  NEW  {sid} {state['file']} @{state['commit'][:8]} "
              f"({state['date'][:10]})", flush=True)
    left = max(0, len(todo) - attempted)
    print(f"harvest_eval_history: stored {stored}, errors {errors}, "
          f"{left} left for the next run")
    _vstored, verrors = harvest_version_pages(store)
    return 1 if (errors or verrors) else 0


if __name__ == "__main__":
    sys.exit(main())
