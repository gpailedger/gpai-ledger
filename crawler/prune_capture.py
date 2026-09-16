"""Prune a capture from the corpus, with full provenance. The ONLY sanctioned way
to remove a capture: it proves the capture is genuinely redundant before touching
anything, then removes the directory, repairs state.json, and appends a fully
attributed prune event (ts, sha256, reason) that verify_corpus C4 requires for all
prunes after the 20 Aug 2026 log-correction event.

A capture qualifies in exactly two cases, and in no other:

1. NOISE (`pruned-noise`) — its canonical text is identical to the PREVIOUS
   version of the same target: the bytes changed, the content did not (banner
   churn, re-serialization).
2. DUPLICATE (`pruned-duplicate`) — another capture the ledger still stands behind
   holds the same bytes, sha256 for sha256. Upstream renames mint these: AIAL
   renamed 100 evaluation files on 14 Sep 2026, and the harvest stored 57 states it
   already held while 136 regenerated pages were filed under the tracker instead of
   under their model. The event names the capture the content survives in, so
   nothing the ledger held becomes unfindable.

Either way the removed capture's content survives in a retained capture, and the
event records the removed sha256, so it stays identifiable forever.

Usage:
  python crawler/prune_capture.py <source_id> <target_slug> <capture_ts> --reason "..."
  python crawler/prune_capture.py --batch <file.tsv> --reason "..."

A batch file is one capture per line — source_id, target_slug, capture_ts,
separated by tabs. A capture listed in the batch never counts as the survivor of
another, so a batch can never remove both copies of the same bytes.
"""
import argparse
import datetime
import json
import shutil
import sys
from pathlib import Path

import capture as cap

DATA = Path(__file__).resolve().parent.parent / "data"
NOISE, DUPLICATE = "pruned-noise", "pruned-duplicate"


def _manifest(root: Path, rel_dir: str) -> dict:
    try:
        return json.loads((root / rel_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def survivors(state: dict, victims: set) -> dict:
    """sha256 -> [(state key, capture dir)] for every capture that is still in the
    corpus, and stays in it, after this run.

    A capture queued for removal is not one, so a batch cannot take both copies of
    the same bytes; neither is a capture under a retired entry, because a survivor
    has to be a capture the ledger still stands behind.
    """
    out = {}
    for key, entry in state.items():
        if entry.get("retired"):
            continue
        for v in entry.get("versions", []):
            if (key, v["dir"]) in victims:
                continue
            out.setdefault(v["sha256"], []).append((key, v["dir"]))
    return out


def prune_one(root: Path, state: dict, source_id: str, target_slug: str,
              capture_ts: str, reason: str, twins: dict):
    """Remove one capture and repair its state entry.

    Returns (event, None) when it was removed, (None, refusal) when it was not.
    Nothing is touched on a refusal.
    """
    key = f"{source_id}::{target_slug}"
    entry = state.get(key)
    if entry is None:
        return None, f"no state entry {key}"
    versions = entry.get("versions", [])
    idx = next((i for i, v in enumerate(versions)
                if v["dir"].rstrip("/").endswith("/" + capture_ts)), None)
    if idx is None:
        return None, f"no version of {key} captured at {capture_ts}"
    victim = versions[idx]
    manifest = _manifest(root, victim["dir"])
    if not manifest:
        return None, f"{victim['dir']} has no readable manifest"
    victim_text = manifest.get("text_sha256")

    # noise: the canonical text of the PREVIOUS version. Only the previous one
    # counts — a capture whose sole identical neighbour comes later is the earliest
    # dated sighting of that content (its OTS proof and fetch time are evidence of
    # when it was first observed), never noise.
    earlier = versions[idx - 1] if idx > 0 else None
    noise = bool(victim_text and earlier is not None
                 and _manifest(root, earlier["dir"]).get("text_sha256") == victim_text)

    # duplicate: the same bytes, retained elsewhere. A twin under the same source
    # is preferred, so the survivor named is the one a reader of this model's page
    # can see.
    same_bytes = [t for t in twins.get(manifest.get("sha256"), [])
                  if t[1] != victim["dir"]]
    here = [t for t in same_bytes if t[0].split("::")[0] == source_id]
    twin = (here or same_bytes or [None])[0]

    if not noise and twin is None:
        return None, ("canonical text does not match the previous version and no "
                      "retained capture holds these bytes — a content-bearing "
                      "capture, or the earliest sighting of its content")

    shutil.rmtree(root / victim["dir"])
    del versions[idx]
    if versions:
        entry["last_sha256"] = versions[-1]["sha256"]
        entry["last_capture"] = versions[-1]["dir"]
        entry["last_text_sha256"] = _manifest(root, versions[-1]["dir"]).get("text_sha256")
    else:
        # the chain is empty; tail pointers into a directory that no longer exists
        # would dangle, and every reader of state would follow them
        for tail in ("last_sha256", "last_capture", "last_text_sha256"):
            entry.pop(tail, None)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {"ts": ts, "source": source_id, "target": target_slug,
             "outcome": NOISE if noise else DUPLICATE, "dir": victim["dir"],
             "sha256": manifest["sha256"], "text_sha256": victim_text,
             "reason": reason, "via": "prune_capture"}
    if not noise:
        twin_source, twin_target = twin[0].split("::", 1)
        event["survives_in"] = {"source": twin_source, "target": twin_target,
                                "dir": twin[1]}
    return event, None


def _write(state_p: Path, state: dict, events_p: Path, event: dict) -> None:
    cap.atomic_write_text(state_p, json.dumps(state, indent=2))
    with open(events_p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(event) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source_id", nargs="?")
    ap.add_argument("target_slug", nargs="?")
    ap.add_argument("capture_ts", nargs="?")
    ap.add_argument("--batch", help="TSV of source_id, target_slug, capture_ts")
    ap.add_argument("--reason", required=True)
    args = ap.parse_args()

    if args.batch:
        if args.source_id:
            print("REFUSED: give a batch file or one capture, not both")
            return 1
        wanted = []
        for line in Path(args.batch).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                print(f"REFUSED: {args.batch} line is not three tab-separated "
                      f"fields: {line!r}")
                return 1
            wanted.append(tuple(p.strip() for p in parts))
    elif args.source_id and args.target_slug and args.capture_ts:
        wanted = [(args.source_id, args.target_slug, args.capture_ts)]
    else:
        print("REFUSED: give source_id, target_slug and capture_ts, or --batch")
        return 1

    state_p, events_p = DATA / "state.json", DATA / "events.jsonl"
    state = json.loads(state_p.read_text(encoding="utf-8"))

    victims = set()
    for source_id, target_slug, capture_ts in wanted:
        entry = state.get(f"{source_id}::{target_slug}", {})
        for v in entry.get("versions", []):
            if v["dir"].rstrip("/").endswith("/" + capture_ts):
                victims.add((f"{source_id}::{target_slug}", v["dir"]))
    twins = survivors(state, victims)

    pruned = 0
    for source_id, target_slug, capture_ts in wanted:
        event, refusal = prune_one(DATA, state, source_id, target_slug, capture_ts,
                                   args.reason, twins)
        if refusal:
            print(f"REFUSED {source_id}::{target_slug} {capture_ts}: {refusal}")
            if not args.batch:
                return 1
            continue
        _write(state_p, state, events_p, event)
        pruned += 1
        where = event.get("survives_in", {}).get("dir")
        print(f"pruned {event['dir']} (sha {event['sha256'][:12]}) — "
              + (f"content survives in {where}" if where
                 else "hash preserved in the event log"))
    if args.batch:
        print(f"pruned {pruned} of {len(wanted)} capture(s)")
    return 0 if pruned == len(wanted) else 1


if __name__ == "__main__":
    sys.exit(main())
