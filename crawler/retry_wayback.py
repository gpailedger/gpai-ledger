"""Retry failed Wayback saves recorded in capture manifests, and re-verify
recorded snapshots.

Default mode scans manifests for wayback.ok == false, re-attempts the save with
generous throttling, and updates the manifest atomically (previous attempts kept
under wayback_attempts). Safe to run repeatedly.

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
import re
import sys
import time
from pathlib import Path

import requests

import capture as cap

DATA = Path(__file__).resolve().parent.parent / "data"
MAX_ATTEMPTS = 5          # answered attempts (an SPN verdict other than 429/5xx)
MAX_ATTEMPT_DAYS = 30     # distinct UTC dates tried, any outcome: the hard stop
TRANSPORT_STATUSES = (429, 500, 502, 503, 504)
SIGNED_URL_MARKERS = re.compile(
    r"X-Amz-Signature=|[?&]oh=|Key-Pair-Id=|[?&]Policy=|trust-zip\?r=")


def answered_attempts(attempts) -> list:
    """The attempts that carry a verdict from Save Page Now itself."""
    return [a for a in attempts if isinstance(a.get("status_code"), int)
            and a["status_code"] not in TRANSPORT_STATUSES]


def attempt_dates(attempts) -> set:
    return {str(a.get("at"))[:10] for a in attempts if a.get("at")}


def _save(p: Path, m: dict) -> None:
    cap.atomic_write_text(p, json.dumps(m, indent=2, ensure_ascii=False))


def _capture_ts(url: str):
    m = re.search(r"/web/(\d{14})", url or "")
    return m.group(1) if m else None


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


def main() -> int:
    if "--verify" in sys.argv:
        return verify_snapshots()
    retried = ok = skipped = 0
    for p in sorted(DATA.rglob("manifest.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        wb = m.get("wayback")
        if wb is None or wb.get("ok") or wb.get("gave_up"):
            continue
        url = m["http"]["url"]
        if SIGNED_URL_MARKERS.search(url):
            m["wayback"]["gave_up"] = "signed URL; save must use a freshly mined URL"
            _save(p, m)
            skipped += 1
            continue
        attempts = m.get("wayback_attempts", []) + [wb]
        answered, dates = answered_attempts(attempts), attempt_dates(attempts)
        if len(answered) >= MAX_ATTEMPTS:
            m["wayback"]["gave_up"] = (f"exhausted {MAX_ATTEMPTS} answered attempts "
                                       f"({len(attempts)} in total)")
            _save(p, m)
            skipped += 1
            continue
        if len(dates) >= MAX_ATTEMPT_DAYS:
            m["wayback"]["gave_up"] = f"tried on {len(dates)} distinct dates without a save"
            _save(p, m)
            skipped += 1
            continue
        print(f"retrying: {m['source_id']} {url[:90]}", flush=True)
        result = cap.wayback_save(url)
        m.setdefault("wayback_attempts", []).append(wb)
        m["wayback"] = result
        _save(p, m)
        retried += 1
        ok += 1 if result.get("ok") else 0
        print(f"  -> {'OK' if result.get('ok') else 'FAIL ' + str(result.get('status_code') or result.get('error', ''))[:80]}",
              flush=True)
        time.sleep(12)
    print(f"\nretried {retried}, now ok: {ok}, marked gave-up/skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
