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
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

import capture as cap

DATA = Path(__file__).resolve().parent.parent / "data"
MAX_ATTEMPTS = 5
SIGNED_URL_MARKERS = re.compile(
    r"X-Amz-Signature=|[?&]oh=|Key-Pair-Id=|[?&]Policy=|trust-zip\?r=")


def _save(p: Path, m: dict) -> None:
    cap.atomic_write_text(p, json.dumps(m, indent=2, ensure_ascii=False))


def verify_snapshots() -> int:
    """Verify wayback.ok snapshot URLs still resolve; demote only on PROOF of
    death. archive.org throttles bursts by dropping connections, and a dropped
    connection is not a dead snapshot: only a definitive HTTP 404/410 counts,
    and only when observed on two separate dated runs (first sighting records
    suspect_dead_since; a later run's second 404 demotes). GET, not HEAD —
    Wayback answers HEAD unreliably. Anything else (timeouts, resets, 429,
    5xx, other statuses) is skipped for a future run."""
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
        if status == 200:
            wb["snapshot_verified"] = cap.utc_now()
            wb.pop("suspect_dead_since", None)
            m["wayback"] = wb
            verified += 1
        elif status in (404, 410):
            if wb.get("suspect_dead_since"):
                m.setdefault("wayback_attempts", []).append(dict(wb))
                m["wayback"] = {"ok": False, "snapshot": snap,
                                "error": f"snapshot returned {status} on two "
                                         f"dated checks ({wb['suspect_dead_since']} "
                                         f"and {cap.utc_now()})"}
                demoted += 1
                print(f"DEMOTED (404 twice): {m['source_id']} {snap[:90]}")
            else:
                wb["suspect_dead_since"] = cap.utc_now()
                m["wayback"] = wb
                suspected += 1
                print(f"suspect (first {status}): {m['source_id']} {snap[:90]}")
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
        attempts = m.get("wayback_attempts", [])
        if len(attempts) >= MAX_ATTEMPTS:
            m["wayback"]["gave_up"] = f"exhausted {MAX_ATTEMPTS} attempts"
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
