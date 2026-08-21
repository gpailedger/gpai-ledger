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
"""
import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import capture as cap

REPO_ROOT = Path(__file__).resolve().parent.parent
WAYBACK_DEFAULT_KINDS = {"provider-live", "provider-page", "regulatory", "watch-page", "cop-doc"}


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
             "wayback_ok": 0, "wayback_fail": 0, "ots_ok": 0, "ots_fail": 0}
    failures = []
    fetch_cache = {}  # url -> (raw, meta); several sources share one portal URL

    for source in registry["sources"]:
        if args.only:
            if "/" in args.only:
                if source["id"].lower() != args.only.lower():
                    continue
            elif args.only.lower() not in source["id"].lower():
                continue
        for target in source.get("targets", []):
            url, kind = target["url"], target["kind"]
            tslug = cap.target_slug(kind, url)
            rendered = bool(target.get("render"))
            stats["checked"] += 1
            try:
                cache_key = (url, rendered)
                if cache_key in fetch_cache:
                    raw, meta = fetch_cache[cache_key]
                else:
                    if rendered:
                        raw, meta = cap.fetch_rendered(url)
                    else:
                        raw, meta = cap.fetch(
                            url, validators=validators_for(store, data_root,
                                                           source["id"], tslug, url))
                    fetch_cache[cache_key] = (raw, meta)
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                failures.append((source["id"], url, repr(exc)))
                store.event(source=source["id"], target=tslug, url=url,
                            kind=kind, outcome="error", error=repr(exc))
                print(f"  ERROR  {source['id']} [{kind}] {exc!r}", flush=True)
                time.sleep(args.throttle)
                continue

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
            # for HTML targets a version exists only when the *text* changed.
            if ext == ".html" and text_sha and \
                    text_sha == store.last_text_sha(source["id"], tslug):
                stats["unchanged"] += 1
                store.event(source=source["id"], target=tslug, url=url, kind=kind,
                            outcome="unchanged-content", sha256=sha, text_sha256=text_sha)
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

    store.save_state()
    print("\n=== sweep summary ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if failures:
        print("\n=== failures ===")
        for sid, url, err in failures:
            print(f"  {sid}: {url}\n    {err}")
    # non-zero on fetch errors so CI shows red — the workflow runs this step with
    # continue-on-error, so captured data is still committed, but a partial sweep
    # is never silently reported as a success
    return 1 if stats.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
