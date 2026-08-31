"""Run a capture sweep over crawler/sources.json.

Usage:
  python run_capture.py [--only SUBSTRING] [--no-wayback] [--no-ots]
                        [--wayback-all] [--throttle SECONDS]

Per target: fetch, hash, compare to last stored version, store new versions via
capture.store_new_version (the single sanctioned write path), then trigger a
Wayback save and an OpenTimestamps stamp for new versions. Every check appends
one line to data/events.jsonl.
Wayback policy: provider-live / provider-page / regulatory / watch-page targets by
default; aial-archive only with --wayback-all (their archive is write-once + in git).
Politeness: non-rendered targets send If-None-Match/If-Modified-Since from the
prior capture; a 304 is logged as an origin-asserted "unchanged" (not_modified
marker). One day a week per URL (stable digest schedule) the fetch is forced
unconditional so a lying origin can never park a target on stale 304s.
Absence claims: a 404/410 for a target with a prior capture is re-checked, then
cross-checked against an independent witness before it is trusted — see
docs/runbooks.md, "Absence claims", and absence_streaks() below.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import capture as cap

REPO_ROOT = Path(__file__).resolve().parent.parent
WAYBACK_DEFAULT_KINDS = {"provider-live", "provider-page", "regulatory", "watch-page", "cop-doc"}

# A 404/410 for a target we have captured before is an ABSENCE CLAIM, and a single
# vantage point (a datacenter runner) is not enough to make one: re-check after a
# pause, then ask an independent witness. Only a corroborated absence is an error
# the health gate should redden the run for. See docs/runbooks.md, "Absence claims".
ABSENCE_STATUSES = {404, 410}


def _delay_from_env() -> float:
    raw = os.environ.get("GPAI_RECHECK_DELAY", "45")
    try:
        v = float(raw)
    except ValueError:
        print(f"WARNING: GPAI_RECHECK_DELAY={raw!r} is not a number; using 45", flush=True)
        v = 45.0
    return max(0.0, min(v, 300.0))


RECHECK_DELAY = _delay_from_env()
MAX_WITNESSES_PER_RUN = 5


def _budget_from_env() -> float:
    raw = os.environ.get("GPAI_SWEEP_BUDGET", "6000")
    try:
        v = float(raw)
    except ValueError:
        print(f"WARNING: GPAI_SWEEP_BUDGET={raw!r} is not a number; using 6000", flush=True)
        v = 6000.0
    return max(60.0, min(v, 14400.0))


# Wall-clock budget for one sweep. A host that accepts connections but never
# answers costs minutes per target (three 90 s attempts), and a job killed by the
# CI timeout commits nothing: past the budget the remaining targets are skipped,
# the skip is recorded in the event log, and the run is marked red — while every
# capture already on disk still reaches the commit step.
SWEEP_BUDGET_S = _budget_from_env()
# Single-vantage route: the witness is often inconclusive (Save Page Now cannot
# always capture an error page, and the runner may not reach the Archive at
# all), so an absence observed on this many distinct UTC dates in the current
# unbroken streak — the most recent prior one within ABSENCE_WINDOW_DAYS, so a
# single missed sweep does not break it — is recorded as PERSISTENT: it reddens
# the run and feeds the relocation hunt, but it is never `confirmed` — this
# vantage alone cannot tell a removed document from a datacenter address being
# refused (providers have answered 404 to the runner while serving everyone
# else). Same-day re-runs count once; any success resets. A fresh sighting of
# the document LIVE by the witness or by the operator's attestation
# (crawler/attest.py) outranks this route: the streak restarts at that day and
# the route stays vetoed until the sighting is older than ABSENCE_WINDOW_DAYS.
PERSISTENT_AFTER_DAYS = 2
ABSENCE_WINDOW_DAYS = 3
# A fresh witness seeing the document LIVE contradicts the claim outright; if that
# keeps happening the runner itself is blind to this document — surface it (red,
# but as a vantage problem, never as an absence) once it has been contradicted on
# this many distinct dates of the current streak (most recent prior one within
# ABSENCE_WINDOW_DAYS; an inconclusive day in between does not reset the count).
CONTRADICTED_ALERT_DAYS = 3
# A host that is simply down costs the sweep its whole budget: every target on it
# burns the full fetch timeout times its retries before failing, and on 29 Aug
# 2026 twenty-one timeouts to one host left sixty-eight other targets unchecked.
# After this many consecutive connection failures to a host — failures where the
# origin never answered at all, not 404s, which are answers — the rest of that
# host's targets are recorded as unreachable without a network attempt, and the
# time goes to hosts that are up. Any answer from the host clears the count, and
# the next sweep starts fresh.
HOST_FAILURES_BEFORE_SKIP = 3


def links_changed(data_root: Path, prior_dir: str, raw: bytes) -> bool:
    """Whether this HTML points at different documents than the retained capture
    it matches on text. The prior capture's fingerprint is computed from its
    stored bytes when the manifest predates the field, so a change is caught on
    the first sweep rather than only after a re-baseline."""
    now = cap.doc_links_sha(raw)
    prior = None
    mp = Path(data_root) / prior_dir / "manifest.json"
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
        prior = m.get("doc_links_sha256")
        if prior is None:
            stored = mp.parent / str(m.get("stored_as") or "raw.html")
            if stored.exists():
                prior = cap.doc_links_sha(stored.read_bytes())
    except (OSError, ValueError):
        return False          # unreadable neighbour: never mint on a guess
    if now is None or prior is None:
        return False          # nothing to compare
    return now != prior


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


# Only these mean the origin never answered. A 429, a 5xx, an oversize body or a
# refused redirect are all answers — the host is up and talking, and skipping the
# rest of its targets on that basis would hide real state behind a false verdict.
TRANSPORT_EXC = ("ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
                 "SSLError", "ProxyError", "NewConnectionError", "MaxRetryError",
                 "socket.timeout", "TimeoutError")


def is_transport_failure(exc) -> bool:
    if getattr(exc, "status_code", None) is not None:
        return False
    names = {type(exc).__name__}
    cause = exc
    for _ in range(4):          # requests wraps urllib3 which wraps socket
        cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
        if cause is None:
            break
        names.add(type(cause).__name__)
    blob = repr(exc)
    return bool(names & set(TRANSPORT_EXC)) or any(t in blob for t in TRANSPORT_EXC)


def absence_streaks(events_path: Path) -> dict:
    """(source, target) -> {"absent_on": {dates}, "contradicted_on": {dates}} for
    the target's current unbroken streak of absence events, reset by any success
    outcome (a recheck-recovered included). A contradicted day — a fresh witness
    saw the document live — or a `live-attested` day (the operator fetched it
    from a second network, crawler/attest.py) restarts absent_on (the document
    demonstrably existed that day) while contradicted_on keeps accumulating for
    the vantage alert. confirmed_on holds the dates on which the absence was
    CONFIRMED by an independent vantage: the first such date is news (red run),
    later ones are a fact the site already publishes.
    Plain errors (an 'error' event with no 'absence' field) neither reset nor
    count. A malformed timestamp is skipped, never fatal. Read once per run from
    the append-only event log."""
    tail = {}
    empty = {"absent_on": set(), "contradicted_on": set(), "confirmed_on": set(),
             "confirmed_by": set()}
    if not events_path.exists():
        return {}
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue      # valid JSON, but not an event: never fatal to a sweep
        key = (e.get("source"), e.get("target"))
        out, absence = e.get("outcome"), e.get("absence")
        ts = e.get("ts")
        day = ts[:10] if isinstance(ts, str) else ""
        if (out == "error" and absence and day) or (out == "live-attested" and day):
            try:
                date.fromisoformat(day)
            except ValueError:
                continue  # a damaged line must not abort the sweep that reads it
            entry = tail.setdefault(key, {k: set() for k in empty})
            if absence == "contradicted" or out == "live-attested":
                # a fresh witness, or the operator from a second network, saw
                # the document live: it demonstrably existed that day
                entry["contradicted_on"].add(day)
                entry["absent_on"].clear()
                # the confirmation is void in its entirety once the document is
                # seen alive: keeping the vantages would let a later streak
                # re-publish, as corroboration, a vantage whose last observation
                # here was that the document EXISTED
                entry["confirmed_on"].clear()
                entry["confirmed_by"].clear()
            else:
                entry["absent_on"].add(day)
                if absence == "confirmed":
                    entry["confirmed_on"].add(day)
                    entry["confirmed_by"].update(
                        v for v in (e.get("confirmed_by") or []) if isinstance(v, str))
        elif out in ("new", "unchanged", "unchanged-content", "recheck-recovered"):
            tail.pop(key, None)
    return tail


def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def _obs(exc) -> dict:
    """One observation: exactly what this exception carried, never backfilled."""
    return {"status_code": getattr(exc, "status_code", None),
            "headers": getattr(exc, "headers", None) or {},
            "error": repr(exc), "ts": cap.utc_now()}


def _recheck(url: str, rendered: bool):
    """Second observation after RECHECK_DELAY, with the SAME capture method as the
    first (a rendered target must be re-rendered, not plain-fetched). Returns
    (raw, meta, None) on success or (None, None, exc) on failure."""
    time.sleep(RECHECK_DELAY)
    try:
        raw, meta = cap.fetch_rendered(url) if rendered else cap.fetch(url)
        return raw, meta, None
    except Exception as exc2:  # noqa: BLE001
        return None, None, exc2


def validators_for(store: cap.Store, data_root: Path, source_id: str, tslug: str,
                   url: str, now=None):
    """Prior capture's ETag/Last-Modified for a conditional GET — or None when a
    full fetch is required: on this URL's weekly forced-full-fetch day (stale-304
    guard), when there is no prior capture, or when its manifest is unusable."""
    now = now or datetime.now(timezone.utc)
    if int(hashlib.sha256(url.encode()).hexdigest(), 16) % 7 == now.weekday():
        return None
    last_dir = store.state.get(store.key(source_id, tslug), {}).get("last_capture")
    if not last_dir:
        return None
    try:
        m = json.loads((data_root / last_dir / "manifest.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — unreadable manifest: fetch unconditionally
        return None
    http = m.get("http") or {}
    if http.get("status_code") != 200:
        return None
    if not (http.get("etag") or http.get("last_modified")):
        return None
    return {"etag": http.get("etag"), "last_modified": http.get("last_modified")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default=str(Path(__file__).parent / "sources.json"))
    ap.add_argument("--data-root", default=str(REPO_ROOT / "data"))
    ap.add_argument("--only", default="")
    ap.add_argument("--throttle", type=float, default=1.5)
    ap.add_argument("--wayback-throttle", type=float, default=6.0)
    ap.add_argument("--no-wayback", action="store_true")
    ap.add_argument("--no-ots", action="store_true")
    ap.add_argument("--wayback-all", action="store_true")
    args = ap.parse_args()

    registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    data_root = Path(args.data_root)
    store = cap.Store(data_root)

    stats = {"checked": 0, "new": 0, "unchanged": 0, "errors": 0,
             "unconfirmed_absence": 0, "persistent_absence": 0, "known_absence": 0,
             "vantage_blocked": 0,
             "wayback_ok": 0, "wayback_fail": 0, "ots_ok": 0, "ots_fail": 0}
    failures = []
    fetch_cache = {}  # url -> (raw, meta); several sources share one portal URL
    # cache_key -> {source, second, witness, witness_skipped, claims}: the re-check
    # and witness of the first sibling that hit a 404/410 are reused by later
    # siblings of the same URL; "claims" lists every event written from the memo
    # so that a sibling which later fetches the URL live can supersede them
    absence_memo = {}
    witnesses_used = 0
    witness_halt = None  # "rate-limited" once SPN answered 429: no more witnesses this run
    streaks = absence_streaks(store.events_path)
    t_start = time.monotonic()
    budget_skipped = []  # "source::target" keys not checked: the time budget ran out
    host_failures = {}   # host -> consecutive failures where the origin never answered
    host_skipped = {}    # host -> keys skipped because the host is down
    # host -> failures counted as errors where the origin never answered. If the
    # host goes on to trip the breaker, these were the same outage as the skips
    # that follow, and the split at HOST_FAILURES_BEFORE_SKIP is arbitrary.
    host_outage_errors = {}

    for source in registry["sources"]:
        if args.only:
            if "/" in args.only:
                if source["id"].lower() != args.only.lower():
                    continue
            elif args.only.lower() not in source["id"].lower():
                continue
        if source.get("retired"):
            # retired sources keep their archived versions and permalinks but are
            # no longer fetched
            print(f"  SKIP   {source['id']} — retired: {source['retired']}", flush=True)
            continue
        for target in source.get("targets", []):
            url, kind = target["url"], target["kind"]
            tslug = cap.target_slug(kind, url)
            if time.monotonic() - t_start > SWEEP_BUDGET_S:
                budget_skipped.append(f"{source['id']}::{tslug}")
                continue
            host = host_of(url)
            if host_failures.get(host, 0) >= HOST_FAILURES_BEFORE_SKIP:
                # the host is down, not this document: record it and spend the
                # remaining time on hosts that answer
                host_skipped.setdefault(host, []).append(f"{source['id']}::{tslug}")
                store.event(source=source["id"], target=tslug, url=url, kind=kind,
                            outcome="host-unreachable", host=host,
                            after_failures=host_failures[host])
                print(f"  SKIP   {source['id']} [{kind}] — {host} unreachable "
                      f"({host_failures[host]} consecutive connection failures)",
                      flush=True)
                continue
            rendered = bool(target.get("render"))
            stats["checked"] += 1
            cache_key = (url, rendered)
            stale = None
            try:
                if cache_key in fetch_cache:
                    raw, meta = fetch_cache[cache_key]
                else:
                    if rendered:
                        raw, meta = cap.fetch_rendered(url)
                    else:
                        raw, meta = cap.fetch(
                            url, validators=validators_for(store, data_root,
                                                           source["id"], tslug, url))
                    # a 304 vouches only for THIS source's prior capture (its own
                    # validators): never replay it to a sibling sharing the URL
                    if raw is not None:
                        fetch_cache[cache_key] = (raw, meta)
                    stale = absence_memo.pop(cache_key, None)
                host_failures.pop(host, None)      # the host answered
            except Exception as exc:  # noqa: BLE001
                status = getattr(exc, "status_code", None)
                transport = is_transport_failure(exc)
                if transport:
                    # the origin never answered: the host, not the document
                    host_failures[host] = host_failures.get(host, 0) + 1
                else:
                    host_failures.pop(host, None)
                first = _obs(exc)
                had_capture = bool(store.last_sha(source["id"], tslug))
                if not (status in ABSENCE_STATUSES and had_capture):
                    # not an absence claim: a plain error, red like any failure
                    stats["errors"] += 1
                    if transport:
                        host_outage_errors[host] = host_outage_errors.get(host, 0) + 1
                    failures.append((source["id"], url, repr(exc)))
                    store.event(source=source["id"], target=tslug, url=url,
                                kind=kind, outcome="error", error=repr(exc),
                                **({"status_code": status} if status else {}),
                                **({"headers": first["headers"]} if first["headers"] else {}))
                    print(f"  ERROR  {source['id']} [{kind}] {exc!r}", flush=True)
                    time.sleep(args.throttle)
                    continue

                memo = absence_memo.get(cache_key)
                shared_from = memo["source"] if memo else None
                if memo is None:
                    # 1. re-check once after a pause, SAME capture method
                    raw, meta, exc2 = _recheck(url, rendered)
                    if exc2 is None:
                        fetch_cache[cache_key] = (raw, meta)
                        store.event(source=source["id"], target=tslug, url=url,
                                    kind=kind, outcome="recheck-recovered",
                                    observations=[first], rechecked_after_s=RECHECK_DELAY)
                        print(f"  RECOVERED {source['id']} [{kind}] {status} then OK "
                              f"on re-check", flush=True)
                        # memo stays None; fall through to normal processing below
                    else:
                        second = _obs(exc2)
                        witness, skipped = None, None
                        if second["status_code"] in ABSENCE_STATUSES:
                            # 2. ask an independent witness before calling it absent
                            if args.no_wayback:
                                skipped = "no-wayback"
                            elif witness_halt:
                                skipped = witness_halt
                            elif witnesses_used >= MAX_WITNESSES_PER_RUN:
                                skipped = "budget"
                            else:
                                witnesses_used += 1
                                witness = cap.wayback_witness(url)
                                if (witness or {}).get("reason") == "rate-limited":
                                    witness_halt = "rate-limited"
                        memo = {"source": source["id"], "second": second,
                                "witness": witness, "witness_skipped": skipped,
                                "claims": []}
                        absence_memo[cache_key] = memo

                if memo is not None:
                    second = memo["second"]
                    witness, skipped = memo["witness"], memo["witness_skipped"]
                    common = {"observations": [first, second],
                              "rechecked_after_s": RECHECK_DELAY,
                              "status_code": second["status_code"],
                              "headers": second["headers"]}
                    if shared_from:
                        common["shared_from"] = shared_from
                    if second["status_code"] not in ABSENCE_STATUSES:
                        # the re-check failed for a NON-absence reason: plain error
                        stats["errors"] += 1
                        failure = (source["id"], url, second["error"])
                        failures.append(failure)
                        store.event(source=source["id"], target=tslug, url=url,
                                    kind=kind, outcome="error", error=second["error"],
                                    **common)
                        memo["claims"].append({"source": source["id"], "tslug": tslug,
                                               "kind": kind, "first": first, "second": second,
                                               "absence": None, "stat_keys": ["errors"],
                                               "failure": failure})
                        print(f"  ERROR  {source['id']} [{kind}] {second['error']} "
                              f"(after {status} on first try)", flush=True)
                        time.sleep(args.throttle)
                        continue

                    # 3. classify the absence claim — one clock reading stamps the
                    # event and every date derived for it (a run may cross midnight)
                    ts = cap.utc_now()
                    today = ts[:10]
                    streak = streaks.get((source["id"], tslug),
                                         {"absent_on": set(), "contradicted_on": set(),
                                          "confirmed_on": set(), "confirmed_by": set()})
                    witness_saw = (witness or {}).get("saw")
                    stat_keys, failure = [], None
                    if witness_saw == "live":
                        # the runner cannot fetch a document the witness sees live
                        prior = streak["contradicted_on"] - {today}
                        dates = sorted(prior | {today})
                        within = bool(prior) and _days_between(today, max(prior)) <= ABSENCE_WINDOW_DAYS
                        absence, confirmed_by = "contradicted", []
                        stats["vantage_blocked"] += 1
                        stat_keys.append("vantage_blocked")
                        if len(dates) >= CONTRADICTED_ALERT_DAYS and within:
                            stats["errors"] += 1
                            stat_keys.append("errors")
                            failure = (source["id"], url,
                                       f"vantage problem: this runner cannot fetch a "
                                       f"document the witness sees live ({len(dates)} days)")
                            failures.append(failure)
                        extra = {"contradicted_on": dates,
                                 "consecutive_contradicted_days": len(dates)}
                    else:
                        prior = streak["absent_on"] - {today}
                        dates = sorted(prior | {today})
                        within = bool(prior) and _days_between(today, max(prior)) <= ABSENCE_WINDOW_DAYS
                        # a fresh witness that saw the document LIVE within the window
                        # outranks this vantage: the day route stays vetoed until that
                        # sighting ages out (absence_streaks restarted absent_on there)
                        last_live = max(streak["contradicted_on"], default=None)
                        live_recent = (last_live is not None
                                       and _days_between(today, last_live) <= ABSENCE_WINDOW_DAYS)
                        confirmed_by = []
                        if witness_saw == "absent":
                            confirmed_by.append("witness")
                        persistent = (len(dates) >= PERSISTENT_AFTER_DAYS and within
                                      and not live_recent)
                        # An independent vantage may have confirmed this absence
                        # on an earlier date of the same streak (the Archive witness,
                        # or the operator via crawler/attest.py). That confirmation
                        # stands until the document is seen again, so today's 404 is
                        # another observation of a settled fact, not a fresh claim.
                        # the streak was read from the log before this run began, so
                        # any confirmation in it predates this observation — including
                        # one the operator recorded earlier the same day
                        confirmed_earlier = bool(streak["confirmed_on"])
                        if confirmed_by or confirmed_earlier:
                            absence = "confirmed"
                            if confirmed_earlier:
                                confirmed_by = sorted(set(confirmed_by)
                                                      | streak["confirmed_by"])
                            # News exactly once. From the first confirmation the model
                            # page says the provider's copy is gone, so repeating it
                            # daily would redden the run forever and teach the operator
                            # to ignore red. Red means new or unresolved, never
                            # already-published.
                            if confirmed_earlier:
                                stats["known_absence"] += 1
                                stat_keys.append("known_absence")
                            else:
                                stats["errors"] += 1
                                stat_keys.append("errors")
                                failure = (source["id"], url, second["error"])
                                failures.append(failure)
                        elif persistent:
                            # this vantage alone, on several dates: red so the
                            # operator looks (crawler/attest.py) — never confirmed
                            absence = "persistent"
                            stats["errors"] += 1
                            stats["persistent_absence"] += 1
                            stat_keys += ["errors", "persistent_absence"]
                            failure = (source["id"], url,
                                       f"{second['error']} — persistent from this vantage "
                                       f"only ({len(dates)} dates, no independent "
                                       f"corroboration): verify from another network "
                                       f"with crawler/attest.py")
                            failures.append(failure)
                        else:
                            absence = "unconfirmed"
                            stats["unconfirmed_absence"] += 1
                            stat_keys.append("unconfirmed_absence")
                        extra = {"absent_on": dates, "consecutive_absent_days": len(dates)}
                        if live_recent:
                            extra["last_live_witness"] = last_live
                    store.event(source=source["id"], target=tslug, url=url,
                                kind=kind, outcome="error", error=second["error"],
                                witness=witness, witness_skipped=skipped,
                                absence=absence, confirmed_by=confirmed_by,
                                ts=ts, **extra, **common)
                    memo["claims"].append({"source": source["id"], "tslug": tslug,
                                           "kind": kind, "first": first, "second": second,
                                           "absence": absence, "stat_keys": stat_keys,
                                           "failure": failure})
                    print(f"  ERROR  {source['id']} [{kind}] {second['error']} — absence "
                          f"{absence}" + (f" (witness saw {witness_saw})" if witness else
                                          f" (witness skipped: {skipped})"), flush=True)
                    time.sleep(args.throttle)
                    continue

            for c in (stale or {}).get("claims", []):
                # an earlier sibling of this URL was recorded absent (or errored on
                # re-check) minutes ago, and this vantage has now fetched the URL
                # live: the claim is superseded in the log and withdrawn from the
                # health gate — the run itself holds the proof it was transient
                store.event(source=c["source"], target=c["tslug"], url=url,
                            kind=c["kind"], outcome="recheck-recovered",
                            observations=[c["first"], c["second"]],
                            rechecked_after_s=RECHECK_DELAY,
                            recovered_by=source["id"],
                            recovered_at=meta.get("fetched_at"),
                            prior_absence=c["absence"])
                for k in c["stat_keys"]:
                    stats[k] -= 1
                if c["failure"] in failures:
                    failures.remove(c["failure"])
                print(f"  RECOVERED {c['source']} [{c['kind']}] earlier "
                      f"{c['absence'] or 'error'} superseded: live for {source['id']}",
                      flush=True)

            if raw is None:
                # 304 Not Modified: the origin asserts no change against the prior
                # capture's validators. Logged distinctly (not_modified) because it
                # is origin-asserted, not hash-verified; the weekly forced full
                # fetch re-verifies by hash.
                stats["unchanged"] += 1
                store.event(source=source["id"], target=tslug, url=url, kind=kind,
                            outcome="unchanged",
                            sha256=store.last_sha(source["id"], tslug),
                            not_modified=True)
                time.sleep(args.throttle)
                continue

            sha = cap.sha256_hex(raw)
            # rendered DOM bytes are unstable (JS nonces); dedupe on text only below
            if not rendered and sha == store.last_sha(source["id"], tslug):
                stats["unchanged"] += 1
                store.event(source=source["id"], target=tslug, url=url,
                            kind=kind, outcome="unchanged", sha256=sha)
                time.sleep(args.throttle)
                continue

            ext = ".html" if rendered else cap.guess_ext(meta["content_type"], meta["final_url"], raw)
            text, notes = cap.extract_text(raw, ext)
            # canonical change key: inner-member hash set for zips (outer bytes of
            # regenerated archives rotate; verify_corpus C5 recomputes this exact
            # key for raw.zip), whitespace-collapsed text hash for everything else
            if ext == ".zip":
                text_sha = cap.zip_content_key(notes)
            else:
                text_sha = cap.canonical_text_sha(text) if text else None

            # Dynamic HTML (nonces, csrf tokens) mints new byte-hashes on every fetch;
            # for HTML targets a version exists only when the *text* changed — and
            # the text is compared with EVERY retained version of the target, so a
            # page that flips between two chrome states (a consent banner stripped
            # on one run and not the next) cannot mint a version on every flip
            known = (store.known_text_shas(source["id"], tslug)
                     if ext == ".html" and text_sha else {})
            if text_sha in known and links_changed(data_root, known[text_sha], raw):
                # same words, different documents linked: a tracker page that
                # re-points a summary at another model's file reads as unchanged
                # to text extraction, so this is the only thing that catches it
                print(f"  LINKS  {source['id']} [{kind}] same text, different "
                      f"document links — recording a version", flush=True)
            elif text_sha in known:
                stats["unchanged"] += 1
                store.event(source=source["id"], target=tslug, url=url, kind=kind,
                            outcome="unchanged-content", sha256=sha, text_sha256=text_sha,
                            matches=known[text_sha])
                time.sleep(args.throttle)
                continue

            wayback_url = (url if not args.no_wayback
                           and (args.wayback_all or kind in WAYBACK_DEFAULT_KINDS)
                           else None)
            rel, manifest = cap.store_new_version(
                store, source_id=source["id"], provider=source["provider"],
                model=source["model"], kind=kind, tslug=tslug, event_url=url,
                raw=raw, meta=meta, ext=ext, text=text, notes=notes,
                text_sha=text_sha, wayback_url=wayback_url,
                do_ots=not args.no_ots, event_extra={"extracted": bool(text)})
            if "wayback" in manifest:
                stats["wayback_ok" if manifest["wayback"].get("ok") else "wayback_fail"] += 1
                time.sleep(args.wayback_throttle)
            if "ots" in manifest:
                stats["ots_ok" if manifest["ots"].get("ok") else "ots_fail"] += 1
            stats["new"] += 1
            print(f"  NEW    {source['id']} [{kind}] sha={sha[:12]} "
                  f"{len(raw):,}B text={'y' if text else 'n'}", flush=True)
            time.sleep(args.throttle)

    if host_skipped:
        stats["host_skipped"] = sum(len(v) for v in host_skipped.values())
        for h, keys in sorted(host_skipped.items()):
            store.event(outcome="host-unreachable-summary", host=h,
                        skipped=keys, after_failures=host_failures.get(h),
                        outage_errors=host_outage_errors.get(h, 0))
            print(f"  HOST    {h} unreachable — {len(keys)} target(s) skipped, "
                  f"and the {host_outage_errors.get(h, 0)} failure(s) that "
                  f"proved it unreachable do not redden this run", flush=True)

    if budget_skipped:
        stats["skipped"] = len(budget_skipped)
        stats["errors"] += 1
        failures.append(("sweep", "", f"time budget of {SWEEP_BUDGET_S:.0f}s exhausted: "
                                      f"{len(budget_skipped)} target(s) not checked this run"))
        store.event(outcome="sweep-budget-exhausted", budget_s=SWEEP_BUDGET_S,
                    checked=stats["checked"], skipped=budget_skipped)
        print(f"  BUDGET  {len(budget_skipped)} target(s) skipped after "
              f"{SWEEP_BUDGET_S:.0f}s", flush=True)
    # Failures where the origin never answered, on a host that then proved
    # unreachable, are one outage rather than that many findings. They stay in the
    # event log exactly as they happened; they simply stop deciding the exit code,
    # so a red run keeps meaning "a document this project tracks needs attention"
    # rather than "someone else's server was down". Anything the host DID answer
    # still counts, on that host as on any other.
    outage_errors = sum(host_outage_errors.get(h, 0) for h in host_skipped)
    if outage_errors:
        stats["errors_from_host_outage"] = outage_errors
    store.save_state()
    print("\n=== sweep summary ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if failures:
        print("\n=== failures ===")
        for sid, url, err in failures:
            print(f"  {sid}: {url}\n    {err}")
    # non-zero on the FIRST confirmation of an absence, on every PERSISTENT one
    # (single vantage, still unresolved) and on plain errors, so CI shows red —
    # except failures that were a whole host being unreachable (see above) —
    # the workflow runs this step with continue-on-error, so captured data is
    # still committed, but a partial sweep is never silently reported as a
    # success. A confirmed absence the site already publishes (known_absence),
    # an unconfirmed one, and a contradicted one are fully logged but do not
    # redden the run, except a repeated contradiction, which reddens as a vantage
    # problem.
    return 1 if (stats.get("errors", 0) - outage_errors) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
