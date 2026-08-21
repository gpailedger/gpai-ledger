"""Upgrade pending OpenTimestamps proofs to bitcoin-anchored attestations.

Calendars anchor submitted digests in bitcoin within hours. This walks every stored
.ots file still carrying pending attestations, asks each calendar for the completed
timestamp, merges any bitcoin attestation, and rewrites the proof in place. Files
already anchored are skipped. Failures are non-fatal (calendar not ready yet).

Run daily after the sweep: python crawler/upgrade_ots.py
"""
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.serialize import (BytesDeserializationContext,
                                           BytesSerializationContext)
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

import capture as cap

DATA = Path(__file__).resolve().parent.parent / "data"
# Calendar URIs come from inside .ots files, which live in a public repo and could be
# influenced by a malicious PR. Only ever contact known calendar infrastructure.
# NOTE: proofs carry the CALENDAR hosts (the attestation servers), which differ from
# the SUBMISSION pool aliases in cap.OTS_CALENDARS — both sets are allowed; they are
# operated by the same parties.
ATTESTATION_CALENDAR_HOSTS = {
    "alice.btc.calendar.opentimestamps.org",
    "bob.btc.calendar.opentimestamps.org",
    "finney.calendar.eternitywall.com",
    "btc.calendar.catallaxy.com",
}
ALLOWED_CALENDAR_HOSTS = ({urlparse(c).netloc for c in cap.OTS_CALENDARS}
                          | ATTESTATION_CALENDAR_HOSTS)
# Proofs younger than this can't be bitcoin-anchored yet — polling them is futile.
MIN_PROOF_AGE_HOURS = 24


def _allowed_calendar(uri: str) -> bool:
    p = urlparse(uri)
    return p.scheme == "https" and p.netloc in ALLOWED_CALENDAR_HOSTS


def _proof_age_hours(p: Path) -> float:
    """Age from the capture-dir timestamp (YYYYMMDDTHHMMSSZ); inf if unparsable."""
    import re
    from datetime import datetime, timezone
    m = re.match(r"(\d{8}T\d{6})Z$", p.parent.name)
    if not m:
        return float("inf")
    dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def is_anchored(ts: Timestamp) -> bool:
    return any(isinstance(att, BitcoinBlockHeaderAttestation)
               for _, att in ts.all_attestations())


def upgrade_timestamp(ts: Timestamp) -> bool:
    """Recursively replace pending attestations that calendars have completed."""
    def walk(t: Timestamp) -> bool:
        ch = False
        for att in list(t.attestations):
            if isinstance(att, PendingAttestation):
                uri = att.uri.decode() if isinstance(att.uri, bytes) else att.uri
                if not _allowed_calendar(uri):
                    continue  # never contact a non-allowlisted calendar host
                try:
                    r = requests.get(f"{uri.rstrip('/')}/timestamp/{t.msg.hex()}",
                                     headers={"User-Agent": cap.USER_AGENT,
                                              "Accept": "application/vnd.opentimestamps.v1"},
                                     timeout=30)
                    if r.status_code == 200 and r.content:
                        upgraded = Timestamp.deserialize(
                            BytesDeserializationContext(r.content), t.msg)
                        t.merge(upgraded)
                        t.attestations.discard(att)
                        ch = True
                except Exception:  # noqa: BLE001 — calendar not ready / unreachable
                    continue
        for _, sub in t.ops.items():
            ch = walk(sub) or ch
        return ch
    return walk(ts)


def restamp_missing() -> int:
    """Stamp captures whose OTS submission failed at capture time (ots.ok false and
    no .ots beside the raw file). A late stamp is still evidence: it proves the
    bytes existed no later than the (later) stamp date, and the manifest records
    both dates. Runs before the upgrade pass each day."""
    import json
    stamped = 0
    for mp in sorted(DATA.rglob("manifest.json")):
        m = json.loads(mp.read_text(encoding="utf-8"))
        ots_meta = m.get("ots") or {}
        raw_p = mp.parent / m["stored_as"]
        ots_p = mp.parent / (m["stored_as"] + ".ots")
        if ots_meta.get("ok") or ots_p.exists() or not raw_p.exists():
            continue
        digest = bytes.fromhex(m["sha256"])
        ots_bytes, meta = cap.ots_stamp(digest)
        if not ots_bytes:
            continue
        cap.atomic_write_bytes(ots_p, ots_bytes)
        meta["restamped_at"] = cap.utc_now()
        meta["note"] = ("stamp submitted after capture (original submission "
                        "failed); proves existence no later than restamped_at")
        m["ots"] = meta
        cap.atomic_write_text(mp, json.dumps(m, indent=2, ensure_ascii=False))
        stamped += 1
        print(f"restamped: {m['source_id']} {mp.parent.name}")
    if stamped:
        print(f"restamped {stamped} captures whose original OTS submission failed")
    return stamped


def main() -> int:
    restamp_missing()
    upgraded = anchored = pending = errors = 0
    for p in sorted(DATA.rglob("*.ots")):
        if _proof_age_hours(p) < MIN_PROOF_AGE_HOURS:
            pending += 1  # too young to be anchored; next daily run will get it
            continue
        try:
            dtf = DetachedTimestampFile.deserialize(
                BytesDeserializationContext(p.read_bytes()))
        except Exception:  # noqa: BLE001 — any deserialization failure = unreadable
            errors += 1
            continue
        if is_anchored(dtf.timestamp):
            anchored += 1
            continue
        if upgrade_timestamp(dtf.timestamp):
            ctx = BytesSerializationContext()
            dtf.serialize(ctx)
            cap.atomic_write_bytes(p, ctx.getbytes())
            if is_anchored(dtf.timestamp):
                upgraded += 1
            else:
                pending += 1
        else:
            pending += 1
    print(f"ots proofs: {anchored} already anchored, {upgraded} upgraded now, "
          f"{pending} still pending, {errors} unreadable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
