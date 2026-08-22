"""Prune a noise capture from the corpus, with full provenance. The ONLY sanctioned
way to remove a capture: it verifies the capture is genuinely redundant before
touching anything, then removes the directory, repairs state.json, and appends a
fully-attributed pruned-noise event (ts, sha256, reason) that verify_corpus C4
requires for all prunes after the 20 Aug 2026 log-correction event.

A capture qualifies as noise ONLY if its canonical text is identical to the
neighbouring (previous or next) version of the same target — i.e. the bytes
changed but the content did not (banner churn, re-serialization). Anything else
is refused: content-bearing versions are never pruned.

Usage: python crawler/prune_capture.py <source_id> <target_slug> <capture_ts> --reason "..."
   e.g. python crawler/prune_capture.py meta/muse-spark provider-live-1a2b3c4d 20260818T061512Z --reason "banner-only re-render, text identical to predecessor"

The event records the sha256 so the removed capture stays identifiable
forever, and the content-identity precondition means the pruned capture's
content always survives in a retained neighbouring version.
"""
import argparse
import datetime
import json
import shutil
import sys
from pathlib import Path

import capture as cap

DATA = Path(__file__).resolve().parent.parent / "data"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source_id")
    ap.add_argument("target_slug")
    ap.add_argument("capture_ts")
    ap.add_argument("--reason", required=True)
    args = ap.parse_args()

    state_p = DATA / "state.json"
    state = json.loads(state_p.read_text(encoding="utf-8"))
    key = f"{args.source_id}::{args.target_slug}"
    if key not in state:
        print(f"REFUSED: no state entry {key}")
        return 1
    entry = state[key]
    versions = entry.get("versions", [])
    idx = next((i for i, v in enumerate(versions)
                if v["dir"].rstrip("/").endswith("/" + args.capture_ts)), None)
    if idx is None:
        print(f"REFUSED: no version of {key} captured at {args.capture_ts}")
        return 1
    victim = versions[idx]
    cap_dir = DATA / victim["dir"]
    manifest = json.loads((cap_dir / "manifest.json").read_text(encoding="utf-8"))

    # noise test: canonical text identical to the previous or next version
    def text_sha_of(v):
        m_p = DATA / v["dir"] / "manifest.json"
        if not m_p.exists():
            return None
        return json.loads(m_p.read_text(encoding="utf-8")).get("text_sha256")

    victim_text = manifest.get("text_sha256")
    # only the PREVIOUS version counts: a capture whose sole identical neighbour
    # comes later is the earliest dated sighting of that content (its OTS proof
    # and fetch time are evidence of when it was first observed), never noise
    earlier = versions[idx - 1] if idx > 0 else None
    if not victim_text or earlier is None or text_sha_of(earlier) != victim_text:
        print("REFUSED: capture's canonical text does not match the previous "
              "version — this is a content-bearing capture or the earliest "
              "sighting of its content, not noise")
        return 1

    shutil.rmtree(cap_dir)
    del versions[idx]
    if versions:
        entry["last_sha256"] = versions[-1]["sha256"]
        entry["last_capture"] = versions[-1]["dir"]
        last_m = json.loads((DATA / versions[-1]["dir"] / "manifest.json")
                            .read_text(encoding="utf-8"))
        entry["last_text_sha256"] = last_m.get("text_sha256")
    cap.atomic_write_text(state_p, json.dumps(state, indent=2))

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(DATA / "events.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"ts": ts, "source": args.source_id,
                             "target": args.target_slug, "outcome": "pruned-noise",
                             "dir": victim["dir"], "sha256": manifest["sha256"],
                             "text_sha256": victim_text, "reason": args.reason,
                             "via": "prune_capture"}) + "\n")
    print(f"pruned {victim['dir']} (sha {manifest['sha256'][:12]}); "
          f"hash preserved in the event log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
