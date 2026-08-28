"""Retry failed Wayback saves recorded in capture manifests, and re-verify
recorded snapshots.

Default mode scans manifests that have no capture-time witness — no wayback
block at all (an early capture that predates the save step: 43 of them were
invisible to this tool until 28 Aug 2026), a recorded failure, or a snapshot
Save Page Now deduplicated to an EARLIER capture, which witnesses the page
before our fetch rather than the document we stored. It re-attempts the save
with generous throttling and updates the manifest atomically (previous attempts
kept under wayback_attempts). An existing snapshot is never dropped for a failed
retry. Safe to run repeatedly.

--verify mode re-checks manifests whose wayback.ok is TRUE: Save Page Now
acceptance does not guarantee durable indexing (we have seen an accepted save
whose snapshot URL later 404'd). A dead snapshot is demoted to ok=false with a
dated note, so the next default run re-attempts it.

Skipped permanently: URLs with expiring signatures (retrying a dead signed URL can
never succeed — the extractor that owns the source re-mines a fresh URL instead),
and targets that have exhausted MAX_ATTEMPTS (marked gave_up in the manifest).
Only attempts Save Page Now actually answered count toward MAX_ATTEMPTS: a
connection error, a timeout, a 429 or a 5xx says nothing about the URL — an
Archive outage must not strip the witness from every version captured during
it. MAX_ATTEMPT_DAYS distinct dates tried, any outcome, is the hard stop.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

import capture as cap

DATA = Path(__file__).resolve().parent.parent / "data"
MAX_ATTEMPTS = 5          # answered attempts (an SPN verdict other than 429/5xx)
MAX_ATTEMPT_DAYS = 30     # distinct UTC dates tried, any outcome: the hard stop
# Save Page Now is rate-limited, so the backlog is worked in bounded slices
# rather than all at once: a sweep must stay predictable. Captures with no
# witness at all are served before ones that only need a fresher snapshot, and
# each run drains the queue further. A count alone does not bound the time — a
# refused save blocks until its own timeout, and 25 of those cost half an hour
# (observed 28 Aug 2026) — so the pass also has a wall-clock budget.
MAX_PER_RUN = 25
BUDGET_S = 600
# The Internet Archive answers a burst it does not want by dropping connections
# and timing out. Once that has happened this many times in a row the service is
# telling us to stop: further attempts this run would be futile and impolite,
# and they teach us nothing about the URLs.
TRANSPORT_FAILURES_BEFORE_STOP = 3


def budget_s() -> float:
    raw = os.environ.get("GPAI_WAYBACK_BUDGET", "")
    try:
        v = float(raw) if raw else float(BUDGET_S)
    except ValueError:
        print(f"WARNING: GPAI_WAYBACK_BUDGET={raw!r} is not a number; "
              f"using {BUDGET_S}", flush=True)
        v = float(BUDGET_S)
    return max(60.0, min(v, 3600.0))


def is_transport_failure(result: dict) -> bool:
    """The attempt never reached a verdict: a dropped connection, a timeout, or
    a throttling status. It says nothing about the URL."""
    if result.get("ok") or result.get("answered"):
        return False          # a verdict about the URL, not the Archive refusing us
    if result.get("error"):
        return True
    return result.get("status_code") in TRANSPORT_STATUSES
TRANSPORT_STATUSES = (429, 500, 502, 503, 504)
SIGNED_URL_MARKERS = re.compile(
    r"X-Amz-Signature=|[?&]oh=|Key-Pair-Id=|[?&]Policy=|trust-zip\?r=")


def answered_attempts(attempts) -> list:
    """The attempts that carry a verdict from Save Page Now itself.

    An attempt explicitly marked `superseded` does not count: it was made
    through a mechanism that no longer applies (the anonymous endpoint, which
    answered by handing back a pre-existing capture rather than crawling), so
    it says nothing about what the current one can archive. Such attempts stay
    in the manifest as history."""
    return [a for a in attempts
            if not a.get("superseded")
            and (a.get("answered") or (isinstance(a.get("status_code"), int)
                                       and a["status_code"] not in TRANSPORT_STATUSES))]


def attempt_dates(attempts) -> set:
    return {str(a.get("at"))[:10] for a in attempts if a.get("at")}


def _save(p: Path, m: dict) -> None:
    cap.atomic_write_text(p, json.dumps(m, indent=2, ensure_ascii=False))


def _capture_ts(url: str):
    m = re.search(r"/web/(\d{14})", url or "")
    return m.group(1) if m else None


def has_witness(m: dict):
    """Whether this capture already has a Wayback snapshot that witnesses IT.
    A snapshot older than the capture (SPN answering with an existing one) is
    not a witness of what we stored, so it stays on the retry list. Manifests
    written before wayback_save recorded `fresh` are judged from the recorded
    capture time."""
    wb = m.get("wayback") or {}
    snap = wb.get("snapshot")
    if not wb.get("ok") or not snap:
        return False
    if "fresh" in wb:
        return wb["fresh"] is not False
    asked = wb.get("requested_at") or (m.get("http") or {}).get("fetched_at") or wb.get("at")
    fresh = cap.snapshot_is_fresh(snap, asked)
    return fresh is not False


def verify_snapshots() -> int:
    """Verify wayback.ok snapshot URLs still resolve; demote only on PROOF of
    death. archive.org throttles bursts by dropping connections, and a dropped
    connection is not a dead snapshot: only a definitive HTTP 404/410 — or a
    200 that Wayback served from a DIFFERENT capture timestamp (it redirects a
    missing timestamp to the nearest neighbour, which says nothing about the
    recorded capture) — counts, and only when observed on two separate UTC
    dates (first sighting records suspect_dead_since; a later day's second
    strike demotes). GET, not HEAD — Wayback answers HEAD unreliably. Anything
    else (timeouts, resets, 429, 5xx, other statuses) is skipped for a future
    run."""
    checked = verified = suspected = demoted = 0
    for p in sorted(DATA.rglob("manifest.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        wb = m.get("wayback") or {}
        snap = wb.get("snapshot")
        if not wb.get("ok") or not snap or wb.get("snapshot_verified"):
            continue
        checked += 1
        try:
            r = requests.get(snap, timeout=60, allow_redirects=True,
                             headers=cap.HEADERS, stream=True)
            status = r.status_code
        except Exception:  # noqa: BLE001 — throttling/network: no verdict
            time.sleep(10)
            continue
        getattr(r, "close", lambda: None)()
        final = getattr(r, "url", None) or snap
        wanted, served = _capture_ts(snap), _capture_ts(final)
        strike = None
        if status == 200:
            if served is None or wanted is None or served == wanted:
                wb["snapshot_verified"] = cap.utc_now()
                wb.pop("suspect_dead_since", None)
                wb.pop("suspect_reason", None)
                m["wayback"] = wb
                verified += 1
            else:
                strike = f"replayed as a different capture ({served}, not {wanted})"
        elif status in (404, 410):
            strike = f"returned {status}"
        if strike:
            now = cap.utc_now()
            since = wb.get("suspect_dead_since")
            if since and since[:10] < now[:10]:
                m.setdefault("wayback_attempts", []).append(dict(wb))
                m["wayback"] = {"ok": False, "snapshot": snap,
                                "error": f"snapshot {strike} on two dated checks "
                                         f"({since} and {now})"}
                demoted += 1
                print(f"DEMOTED (two dated strikes): {m['source_id']} {snap[:90]}")
            elif not since:
                wb["suspect_dead_since"] = now
                wb["suspect_reason"] = strike
                m["wayback"] = wb
                suspected += 1
                print(f"suspect (first strike, {strike}): {m['source_id']} {snap[:90]}")
            # a second strike on the SAME UTC date is not a second dated check
        # other statuses: no verdict, leave untouched
        _save(p, m)
        time.sleep(6)
    print(f"\nchecked {checked}: verified {verified}, newly suspect {suspected}, "
          f"demoted {demoted}")
    return 0


def _queue():
    """(path, manifest, wayback block, url) for every capture without a witness,
    neediest first: no snapshot at all before one that is merely older than the
    capture. Ordering is deterministic so a slice is reproducible."""
    out = []
    for p in sorted(DATA.rglob("manifest.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        # a missing wayback block means the save was never attempted for this
        # capture — it needs one just as much as a recorded failure does
        wb = m.get("wayback") or {}
        if wb.get("gave_up") or has_witness(m):
            continue
        url = (m.get("http") or {}).get("url")
        if not url:
            continue
        rank = 1 if wb.get("snapshot") else 0      # 0 = nothing archived at all
        out.append((rank, str(p), p, m, wb, url))
    out.sort(key=lambda t: (t[0], t[1]))
    return [(p, m, wb, url) for _rank, _s, p, m, wb, url in out]


def main() -> int:
    if "--verify" in sys.argv:
        return verify_snapshots()
    retried = ok = skipped = 0
    budget, started, transport_run = budget_s(), time.monotonic(), 0
    for p, m, wb, url in _queue():
        if time.monotonic() - started > budget:
            print(f"\nwayback budget of {budget:.0f}s spent; the rest are retried "
                  f"on the next sweep", flush=True)
            break
        if SIGNED_URL_MARKERS.search(url):
            m["wayback"] = dict(wb, gave_up="signed URL; save must use a freshly mined URL")
            _save(p, m)
            skipped += 1
            continue
        attempts = m.get("wayback_attempts", []) + ([wb] if wb else [])
        answered, dates = answered_attempts(attempts), attempt_dates(attempts)
        if len(answered) >= MAX_ATTEMPTS:
            m["wayback"] = dict(wb, gave_up=f"exhausted {MAX_ATTEMPTS} answered attempts "
                                            f"({len(attempts)} in total)")
            _save(p, m)
            skipped += 1
            continue
        if len(dates) >= MAX_ATTEMPT_DAYS:
            m["wayback"] = dict(wb, gave_up=f"tried on {len(dates)} distinct dates "
                                            f"without a save")
            _save(p, m)
            skipped += 1
            continue
        why = "never attempted" if not wb else (
            "snapshot predates the capture" if wb.get("ok") else "previous attempt failed")
        print(f"retrying ({why}): {m['source_id']} {url[:90]}", flush=True)
        result = cap.wayback_save(url)
        if wb:
            m.setdefault("wayback_attempts", []).append(wb)
        # never trade a snapshot we hold for a failed retry: an older snapshot is
        # weaker evidence than a fresh one, but it is evidence
        if result.get("ok") or not wb.get("snapshot"):
            m["wayback"] = result
        else:
            # keep the snapshot, but say what the refresh attempt answered —
            # otherwise a target that can never be refreshed looks unexplained
            m["wayback"] = dict(wb, last_refresh_attempt=result.get("at"),
                                last_refresh_error=str(result.get("error")
                                                       or result.get("status_code"))[:300],
                                last_refresh_via=result.get("via", "anonymous"))
            m.setdefault("wayback_attempts", []).append(result)
        _save(p, m)
        retried += 1
        ok += 1 if result.get("ok") and result.get("fresh") is not False else 0
        print(f"  -> {'OK' if result.get('ok') else 'FAIL ' + str(result.get('status_code') or result.get('error', ''))[:80]}"
              + (" (still an older capture)" if result.get("fresh") is False else ""),
              flush=True)
        if is_transport_failure(result):
            transport_run += 1
            if transport_run >= TRANSPORT_FAILURES_BEFORE_STOP:
                print(f"\nthe Wayback Machine dropped {transport_run} attempts in a "
                      f"row (it is refusing this runner, not answering about these "
                      f"URLs); stopping this pass", flush=True)
                break
        else:
            transport_run = 0
        time.sleep(12)
        if retried >= MAX_PER_RUN:
            print(f"\nreached the per-run limit of {MAX_PER_RUN}; the rest are "
                  f"retried on the next sweep", flush=True)
            break
    print(f"\nretried {retried}, now witnessed: {ok}, "
          f"marked gave-up/skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
