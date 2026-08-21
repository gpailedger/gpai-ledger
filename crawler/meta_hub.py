"""Meta transparency-hub extractor: EU AI Act Transparency Reports series.

Meta serves Art. 53(1)(d) summaries through signed, expiring CDN URLs embedded in
the hub page's JSON payload (no stable document URLs exist). This module renders the
hub, isolates the "EU AI Act Transparency Reports" series, and captures each edition
keyed on the STABLE part of its CDN path (content-addressed by Meta), so rotating
signatures don't mint versions but content changes do.

Run after the main sweep: python crawler/meta_hub.py
"""
import json
import re
import sys
from pathlib import Path

import capture as cap

HUB = "https://transparency.meta.com/reports/regulatory-transparency-reports/"
SERIES = "EU AI Act Transparency Reports"
DATA = Path(__file__).resolve().parent.parent / "data"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def hub_editions():
    dom, _ = cap.fetch_rendered(HUB)
    dom = dom.decode("utf-8", errors="replace")
    i = dom.find(f'"static_report_series_title":"{SERIES}"')
    if i == -1:
        raise SystemExit("EU AI Act series not found on hub page — layout changed?")
    j = dom.find('"static_report_series_title"', i + 10)
    seg = dom[i:j if j != -1 else i + 30000]
    # parse each edition object key-order-independently
    out = []
    for obj in re.findall(r'\{[^{}]*"cdn_url"[^{}]*\}', seg):
        um = re.search(r'"cdn_url":"(https:[^"]+)"', obj)
        pm = re.search(r'"time_period":"([^"]*)"', obj)
        if um and pm:
            period = pm.group(1).replace("\\u2013", "-").replace("–", "-").replace("—", "-")
            out.append({"url": um.group(1).replace("\\/", "/"), "period": period})
    if not out:
        raise SystemExit("EU AI Act series present but zero editions parsed — payload shape changed?")
    return out


def main() -> int:
    store = cap.Store(DATA)
    registry_ids = {s["id"] for s in json.loads(
        (Path(__file__).parent / "sources.json").read_text(encoding="utf-8"))["sources"]}
    editions = hub_editions()
    print(f"{SERIES}: {len(editions)} editions on hub")
    changed = errors = 0
    for ed in editions:
        # stable identity = the content-addressed PATH only: the fbcdn hostname
        # is a rotating CDN shard (scontent-mxp2-1 vs -mxp1-1 for identical
        # bytes), so including it would mint a fresh target per shard rotation
        from urllib.parse import urlsplit
        stable = urlsplit(ed["url"]).path
        model = re.sub(r"^\d{4}\s*-\s*", "", ed["period"])          # "2026 - Muse Spark" -> "Muse Spark"
        source_id = f"meta/{slug(model)}"
        if source_id not in registry_ids:
            # capture anyway (evidence first), but surface loudly: a source id the
            # registry lacks renders NOWHERE on the site until a human registers it
            store.event(source=source_id, target="registry-gap", url=stable,
                        kind="provider-live", outcome="unregistered-source",
                        note="captured but not in sources.json — add a registry entry "
                             "or the site will never show it", via="meta-hub")
            print(f"  WARNING unregistered source id {source_id} — registry entry needed")
        tslug = cap.target_slug("provider-live", stable)
        try:
            raw, meta = cap.fetch(ed["url"])
        except Exception as exc:  # noqa: BLE001
            errors += 1
            store.event(source=source_id, target=tslug, url=stable, kind="provider-live",
                        outcome="error", error=repr(exc), via="meta-hub")
            print(f"  ERROR {ed['period']}: {exc!r}")
            continue
        sha = cap.sha256_hex(raw)
        if sha == store.last_sha(source_id, tslug):
            store.event(source=source_id, target=tslug, url=stable, kind="provider-live",
                        outcome="unchanged", sha256=sha, via="meta-hub")
            print(f"  unchanged {ed['period']} ({sha[:12]})")
            continue
        ext = cap.guess_ext(meta["content_type"], stable, raw)
        text, notes = cap.extract_text(raw, ext)
        text_sha = (cap.zip_content_key(notes) if ext == ".zip"
                    else cap.canonical_text_sha(text) if text else None)
        # if an edition ever becomes an HTML page, its bytes churn per fetch —
        # a version exists only when the text changed (same rule as run_capture)
        if (ext == ".html" and text_sha
                and text_sha == store.last_text_sha(source_id, tslug)):
            store.event(source=source_id, target=tslug, url=stable,
                        kind="provider-live", outcome="unchanged-content",
                        sha256=sha, text_sha256=text_sha, via="meta-hub")
            print(f"  unchanged-content {ed['period']}")
            continue
        rel, manifest = cap.store_new_version(
            store, source_id=source_id, provider="Meta", model=model,
            kind="provider-live", tslug=tslug, event_url=stable,
            raw=raw, meta=meta, ext=ext, text=text, notes=notes,
            text_sha=text_sha, wayback_url=ed["url"],
            extra_notes=[
                "fetched via signed CDN URL embedded in transparency hub JSON; "
                "stable key = unsigned CDN path",
                {"stable_base": stable, "hub_period": ed["period"]}],
            managed="meta_hub", event_extra={"via": "meta-hub"})
        changed += 1
        print(f"  NEW {ed['period']} sha={sha[:12]} {len(raw):,}B ots={manifest['ots'].get('ok')}")
    print(f"done: {changed} new versions, {errors} fetch errors")
    # non-zero on fetch errors so the workflow's health gate can fire (data
    # already stored/committed for the editions that succeeded)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
