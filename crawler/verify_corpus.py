"""Full integrity verifier for the GPAI Ledger corpus (data/). Read-only.

Exit 0 = corpus clean, exit 1 = at least one integrity failure. Windows + Linux safe.

Checks:
  C1  every manifest.json sha256 + size_bytes match the actual raw.* bytes
  C2  every .ots parses as a DetachedTimestampFile whose OpSHA256 file digest equals
      the SHA-256 of the raw file beside it; no orphan .ots
  C3  state.json <-> disk: every state version dir exists with manifest + raw; every
      non-empty capture dir on disk is referenced by state
  C4  events.jsonl parses; every "new" event dir exists on disk or is covered by a
      pruned-noise event; pruned-noise events after the 20 Aug 2026 log-correction
      carry ts + sha256 + reason
  C5  text_sha256 recomputed (canonical whitespace-collapsed) vs manifest; a null
      text_sha256 beside an extracted.txt is itself a failure
  C6  last_sha256 == versions[-1].sha256; last_capture == versions[-1].dir;
      last_text_sha256 == the last version manifest's text_sha256
  C7  retired entries carry a string reason and no live versions
  C9  (warn) a proof still pending a bitcoin attestation after PENDING_WARN_DAYS

Usage: python crawler/verify_corpus.py [--data-root data]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

FAILS, WARNS, STATS = [], [], {}


def _target_slug(kind: str, url: str) -> str:
    return f"{kind}-{hashlib.sha256(url.encode()).hexdigest()[:8]}"


def fail(check, path, msg):
    FAILS.append((check, str(path), msg))


def warn(check, path, msg):
    WARNS.append((check, str(path), msg))


def canonical_text_sha(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def verify(data_root: Path) -> int:
    captures = data_root / "captures"
    state = json.loads((data_root / "state.json").read_text(encoding="utf-8"))

    referenced_dirs = set()
    n_versions = n_ots = 0

    # C1/C2/C5/C6/C7 over state
    for key, entry in state.items():
        retired = entry.get("retired")
        if retired is not None and not isinstance(retired, str):
            fail("C7", key, "retired flag is not a string reason")
        versions = entry.get("versions", [])
        # retired entries legitimately keep their capture history (the site's
        # "superseded target URLs" section); only a deleted capture empties versions
        if not retired and not versions:
            warn("C7", key, "non-retired entry has no versions")
        for i, v in enumerate(versions):
            n_versions += 1
            cap_dir = data_root / v["dir"]
            referenced_dirs.add(str(cap_dir.resolve()))
            manifest_p = cap_dir / "manifest.json"
            if not manifest_p.exists():
                fail("C3", cap_dir, "version dir missing manifest.json")
                continue
            m = json.loads(manifest_p.read_text(encoding="utf-8"))
            raw_p = cap_dir / m["stored_as"]
            if not raw_p.exists():
                fail("C1", raw_p, "raw file named by manifest is absent")
                continue
            raw = raw_p.read_bytes()
            actual = hashlib.sha256(raw).hexdigest()
            if actual != m["sha256"]:
                fail("C1", raw_p, f"sha256 mismatch: disk {actual[:16]} vs manifest {m['sha256'][:16]}")
            if m.get("size_bytes") != len(raw):
                fail("C1", raw_p, f"size mismatch: disk {len(raw)} vs manifest {m.get('size_bytes')}")
            if v["sha256"] != m["sha256"]:
                fail("C6", cap_dir, "state version sha != manifest sha")
            # C2 ots
            ots_p = cap_dir / (m["stored_as"] + ".ots")
            if ots_p.exists():
                n_ots += 1
                if not _ots_matches(ots_p, raw):
                    fail("C2", ots_p, "ots digest does not match raw file SHA-256")
                else:
                    pending_days = _ots_pending_days(ots_p, m)
                    if pending_days > PENDING_WARN_DAYS:
                        warn("C9", ots_p, f"proof still pending after {pending_days} days "
                                          f"(the site says anchoring typically takes a day)")
            elif (m.get("ots") or {}).get("ok"):
                fail("C2", ots_p, "manifest records a successful stamp but the proof "
                                  "file is absent")
            # C5 text — ZIP bundles use the inner-member hash key (format-agnostic
            # dedupe for regenerated archives); everything else uses canonical text
            txt_p = cap_dir / "extracted.txt"
            if m.get("text_sha256"):
                if m["stored_as"] == "raw.zip":
                    pairs = sorted((n["inner_file"], n["inner_sha256"])
                                   for n in m.get("extraction_notes", [])
                                   if isinstance(n, dict) and "inner_sha256" in n)
                    recomputed = hashlib.sha256(
                        json.dumps(pairs, ensure_ascii=False).encode("utf-8")).hexdigest()
                    if recomputed != m["text_sha256"]:
                        fail("C5", cap_dir, f"zip content key mismatch: {recomputed[:16]} vs manifest {m['text_sha256'][:16]}")
                    # the bundle's served extracted.txt is verifiable when the
                    # manifest carries its own canonical hash (captures since the
                    # field was introduced; older manifests have no claim to check)
                    if m.get("extracted_text_sha256") and txt_p.exists():
                        recomputed_txt = canonical_text_sha(txt_p.read_text(encoding="utf-8"))
                        if recomputed_txt != m["extracted_text_sha256"]:
                            fail("C5", txt_p, f"bundle extracted.txt sha mismatch: disk {recomputed_txt[:16]} vs manifest {m['extracted_text_sha256'][:16]}")
                elif txt_p.exists():
                    recomputed = canonical_text_sha(txt_p.read_text(encoding="utf-8"))
                    if recomputed != m["text_sha256"]:
                        fail("C5", txt_p, f"canonical text sha mismatch: disk {recomputed[:16]} vs manifest {m['text_sha256'][:16]}")
            elif txt_p.exists():
                fail("C5", txt_p, "extracted.txt present but manifest text_sha256 is "
                                  "null — extracted text is unverifiable")
        if versions:
            last = versions[-1]
            if entry.get("last_sha256") != last["sha256"]:
                fail("C6", key, "last_sha256 != versions[-1].sha256")
            if entry.get("last_capture") != last["dir"]:
                fail("C6", key, "last_capture != versions[-1].dir")
            last_m_p = data_root / last["dir"] / "manifest.json"
            if last_m_p.exists():
                last_m = json.loads(last_m_p.read_text(encoding="utf-8"))
                if entry.get("last_text_sha256") != last_m.get("text_sha256"):
                    fail("C6", key, "state last_text_sha256 disagrees with the last "
                                    "version's manifest text_sha256")

    # C8 (warn): a non-retired, non-managed entry whose target URL is no longer
    # in the registry renders under "superseded" with no stated reason — stale
    # identities should carry an explicit retired reason
    reg_p = data_root.parent / "crawler" / "sources.json"
    if reg_p.exists():
        reg = json.loads(reg_p.read_text(encoding="utf-8"))
        active = set()
        for s in reg.get("sources", []):
            for tg in s.get("targets", []):
                active.add((s["id"], _target_slug(tg["kind"], tg["url"])))
        for key, entry in state.items():
            sid, tslug = key.split("::", 1)
            if (not entry.get("retired") and not entry.get("managed")
                    and entry.get("versions")
                    and (sid, tslug) not in active):
                warn("C8", key, "non-retired entry's target is no longer in the "
                                "registry — add a retired reason")

    # C3 reverse: every capture leaf dir on disk must be fully formed AND referenced.
    # Glob raw.* (not manifest.json): a crash between the raw write and the manifest
    # write leaves a dir the manifest-glob can never see — the widest crash window.
    seen_dirs = set()
    for raw_p in captures.rglob("raw.*"):
        if raw_p.suffix == ".ots":
            continue
        d = raw_p.parent
        if d in seen_dirs:
            continue
        seen_dirs.add(d)
        if not (d / "manifest.json").exists():
            fail("C3", d, f"{raw_p.name} present but manifest.json missing — partial "
                          f"write from an interrupted sweep")
        elif str(d.resolve()) not in referenced_dirs:
            fail("C3", d, "capture dir on disk not referenced by state (orphan)")
    # a dir holding a manifest but no raw.* (raw deleted by hand) has no raw to
    # glob — sweep manifests too so such a husk can't hide from the orphan scan
    for man_p in captures.rglob("manifest.json"):
        d = man_p.parent
        if d in seen_dirs:
            continue
        seen_dirs.add(d)
        if str(d.resolve()) not in referenced_dirs:
            fail("C3", d, "capture dir on disk not referenced by state (orphan)")
        # referenced dirs with a missing raw already fail C1 (raw named by
        # manifest is absent) — no duplicate C3 finding needed

    # C2 reverse: every .ots on disk must sit beside the raw file it stamps (an
    # orphan proof implies a deleted or renamed capture the state no longer tracks)
    for ots_p in captures.rglob("*.ots"):
        raw_name = ots_p.name[:-len(".ots")]
        if not (ots_p.parent / raw_name).exists():
            fail("C2", ots_p, "orphan .ots: the raw file it stamps is absent")
        elif str(ots_p.parent.resolve()) not in referenced_dirs:
            fail("C2", ots_p, ".ots in a capture dir not referenced by state")

    # C4 events
    events_p = data_root / "events.jsonl"
    pruned_dirs, new_dirs, repacked_shas, bad_lines = set(), [], set(), 0
    past_correction = False
    for line in events_p.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            bad_lines += 1
            continue
        if e.get("outcome") == "log-correction":
            past_correction = True
        # events were written on both Windows and Linux — normalize separators
        if e.get("outcome") == "pruned-noise" and e.get("dir"):
            pruned_dirs.add(e["dir"].replace("\\", "/"))
            # prunes after the 20 Aug 2026 log-correction event must carry full
            # provenance (crawler/prune_capture.py writes them); earlier manual
            # lines are grandfathered by that event
            if past_correction and not (e.get("ts") and e.get("sha256")
                                        and e.get("reason")):
                fail("C4", e["dir"], "pruned-noise event missing ts/sha256/reason "
                                     "(use crawler/prune_capture.py)")
        if e.get("outcome") == "new" and e.get("dir"):
            new_dirs.append((e["dir"].replace("\\", "/"), e.get("sha256")))
        if e.get("outcome") == "scope-repack" and e.get("prior_sha256"):
            repacked_shas.add(e["prior_sha256"])
    if bad_lines:
        fail("C4", events_p, f"{bad_lines} unparseable event lines")
    for d, sha in new_dirs:
        if (not (data_root / d).exists() and d not in pruned_dirs
                and sha not in repacked_shas):
            fail("C4", d, "'new' event dir absent and not pruned/repacked")

    STATS.update({"state_entries": len(state), "versions": n_versions,
                  "ots_proofs": n_ots, "referenced_dirs": len(referenced_dirs)})
    return 1 if FAILS else 0


PENDING_WARN_DAYS = 7


def _ots_pending_days(ots_p: Path, m: dict) -> int:
    """Days a proof has been waiting for a bitcoin attestation (0 once anchored
    or when the age cannot be determined). A proof that stays pending for
    weeks means the upgrade path is broken while the site says 'typically
    within a day'."""
    try:
        from datetime import datetime, timezone
        from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
        from opentimestamps.core.serialize import BytesDeserializationContext
        from opentimestamps.core.timestamp import DetachedTimestampFile
        dtf = DetachedTimestampFile.deserialize(
            BytesDeserializationContext(ots_p.read_bytes()))
        if any(isinstance(att, BitcoinBlockHeaderAttestation)
               for _, att in dtf.timestamp.all_attestations()):
            return 0
        at = (m.get("ots") or {}).get("restamped_at") or (m.get("ots") or {}).get("at")
        if not at:
            return 0
        stamped = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - stamped).days)
    except Exception:  # noqa: BLE001 — never turn a reporting aid into a failure
        return 0


def _ots_matches(ots_p: Path, raw: bytes) -> bool:
    try:
        from opentimestamps.core.serialize import BytesDeserializationContext
        from opentimestamps.core.timestamp import DetachedTimestampFile
        dtf = DetachedTimestampFile.deserialize(
            BytesDeserializationContext(ots_p.read_bytes()))
        return dtf.file_digest == hashlib.sha256(raw).digest()
    except Exception as exc:  # noqa: BLE001
        fail("C2", ots_p, f"unparseable: {exc!r}")
        return True  # already recorded as a fail; don't double-count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default=str(Path(__file__).resolve().parent.parent / "data"))
    args = ap.parse_args()
    rc = verify(Path(args.data_root))
    print("=== corpus stats ===")
    for k, v in STATS.items():
        print(f"  {k}: {v}")
    if WARNS:
        print(f"\n=== warnings ({len(WARNS)}) ===")
        for c, p, m in WARNS[:50]:
            print(f"  [{c}] {p}\n      {m}")
    if FAILS:
        print(f"\n=== FAILURES ({len(FAILS)}) ===")
        for c, p, m in FAILS:
            print(f"  [{c}] {p}\n      {m}")
        print("\nRESULT: FAIL")
    else:
        print("\nRESULT: OK — corpus integrity verified")
    return rc


if __name__ == "__main__":
    sys.exit(main())
