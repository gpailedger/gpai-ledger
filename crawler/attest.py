"""Second-vantage attestation for a target the sweep reports absent.

The daily sweep runs from one network (a GitHub-hosted runner). A 404 seen only
from there is not evidence that a document is gone: providers have answered 404
to datacenter addresses while serving the document to everyone else. When the
Internet Archive witness cannot settle it, the sweep records the absence as
`persistent` (red run, relocation hunt) — never `confirmed`. This tool records
what a SECOND vantage point — the operator's own network — sees, so the event
log carries an independent observation:

    python crawler/attest.py --source speakleash/bielik
    python crawler/attest.py --source microsoft/mai-image-2 --target provider-live-b829216d

For every selected target it fetches the archived URL through the same guarded
fetch the sweep uses and appends ONE event:

  outcome `live-attested`  — HTTP 200: status, content type, size, SHA-256 and
                              whether the bytes equal the archived version. The
                              sweep treats the day like a live witness sighting:
                              the absence streak restarts and the single-vantage
                              route stays vetoed for ABSENCE_WINDOW_DAYS.
  outcome `error`          — HTTP 404/410: `absence: confirmed`,
                              `confirmed_by: ["operator"]` — two independent
                              vantages agree the document is gone.

Anything else (timeout, 5xx, 403) records nothing: it is not an observation of
presence or absence. No version is ever minted here — a changed document is
captured by the sweep (`run_capture.py --only <source>`), not by an attestation.

A host may refuse the project's declared user agent while serving the document
to a browser. The tool never disguises itself; instead the operator downloads
the document in a browser and attests it with `--file <path>`: the event is a
`live-attested` with `observed_via: "operator-supplied file"` (no HTTP fields),
the file's size and SHA-256, and the archive comparison.
Exit status: 0 attested (either way), 2 nothing recorded.
"""
import argparse
import json
import sys
from pathlib import Path

import capture as cap

ROOT = Path(__file__).resolve().parent.parent
ABSENCE_STATUSES = (404, 410)


def _targets(store: cap.Store, source_id: str, only: str):
    for key, entry in store.state.items():
        sid, tslug = key.split("::", 1)
        if sid != source_id or entry.get("retired"):
            continue
        if only and tslug != only:
            continue
        if not only and not tslug.startswith(("provider-live", "provider-page")):
            continue
        yield tslug, entry


def _url_of(store: cap.Store, entry: dict):
    m = store.captures.parent / entry["last_capture"] / "manifest.json"
    if not m.exists():
        return None, None
    man = json.loads(m.read_text(encoding="utf-8"))
    return (man.get("http") or {}).get("url"), man.get("target_kind")


def _last_absence_ts(events_path: Path, source_id: str, tslug: str):
    last = None
    if not events_path.exists():
        return None
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("source") == source_id and e.get("target") == tslug and e.get("absence"):
            last = e.get("ts")
    return last


def attest_file(store: cap.Store, source_id: str, tslug: str, entry: dict, path: Path,
                note: str = "") -> dict:
    """Attest a document the operator obtained outside the tool (a browser
    download): live by a human's observation, hashed here."""
    url, kind = _url_of(store, entry)
    body = Path(path).read_bytes()
    sha = cap.sha256_hex(body)
    rec = {"source": source_id, "target": tslug, "url": url, "kind": kind,
           "outcome": "live-attested", "vantage": "operator",
           "observed_via": "operator-supplied file", "size_bytes": len(body), "sha256": sha,
           "same_as_archived": sha == entry.get("last_sha256"),
           "archived_version": entry.get("last_capture"),
           "supersedes_absence": _last_absence_ts(store.events_path, source_id, tslug)}
    if note:
        rec["note"] = note
    store.event(**rec)
    return {"recorded": True, **rec}


def attest(store: cap.Store, source_id: str, tslug: str, entry: dict, note: str = "") -> dict:
    url, kind = _url_of(store, entry)
    if not url:
        return {"recorded": False, "reason": "no manifest URL for the last capture"}
    try:
        body, meta = cap.fetch(url)
    except cap.PermanentFetchError as exc:
        status = getattr(exc, "status_code", None)
        if status not in ABSENCE_STATUSES:
            return {"recorded": False, "reason": f"not an observation of absence: {exc}"}
        store.event(source=source_id, target=tslug, url=url, kind=kind, outcome="error",
                    error=str(exc), vantage="operator", absence="confirmed",
                    confirmed_by=["operator"], observations=[{"status_code": status}],
                    supersedes_absence=_last_absence_ts(store.events_path, source_id, tslug),
                    note=note or None)
        return {"recorded": True, "outcome": "error", "status_code": status}
    except Exception as exc:  # noqa: BLE001 — timeouts, 5xx: not an observation
        return {"recorded": False, "reason": f"not an observation: {exc!r}"}
    if body is None or meta.get("status_code") != 200:
        return {"recorded": False, "reason": f"HTTP {meta.get('status_code')}: not an observation"}
    sha = cap.sha256_hex(body)
    rec = {"source": source_id, "target": tslug, "url": url, "kind": kind,
           "outcome": "live-attested", "vantage": "operator",
           "status_code": 200, "content_type": meta.get("content_type"),
           "size_bytes": len(body), "sha256": sha,
           "same_as_archived": sha == entry.get("last_sha256"),
           "archived_version": entry.get("last_capture"),
           "supersedes_absence": _last_absence_ts(store.events_path, source_id, tslug)}
    if note:
        rec["note"] = note
    store.event(**rec)
    return {"recorded": True, **rec}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", required=True, help="registry source id, e.g. speakleash/bielik")
    ap.add_argument("--target", default="", help="target slug (default: the source's live targets)")
    ap.add_argument("--data-root", default=str(ROOT / "data"))
    ap.add_argument("--note", default="", help="free text kept with the event (no names)")
    ap.add_argument("--file", default="", help="a copy of the document obtained in a browser "
                                              "(for a host that refuses the project's user agent)")
    args = ap.parse_args(argv)
    store = cap.Store(Path(args.data_root))
    recorded = 0
    targets = list(_targets(store, args.source, args.target))
    if args.file and len(targets) != 1:
        print("--file needs exactly one target (use --target)", flush=True)
        return 2
    for tslug, entry in targets:
        res = (attest_file(store, args.source, tslug, entry, Path(args.file), note=args.note)
               if args.file else attest(store, args.source, tslug, entry, note=args.note))
        flag = "ATTESTED" if res.get("recorded") else "skipped "
        detail = (f"{res.get('outcome')} sha={res.get('sha256', '')[:12]} "
                  f"same_as_archived={res.get('same_as_archived')}"
                  if res.get("recorded") else res.get("reason"))
        print(f"  {flag} {args.source} [{tslug}] {detail}", flush=True)
        recorded += bool(res.get("recorded"))
    if not recorded:
        print("nothing recorded", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
