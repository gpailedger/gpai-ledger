"""Meta transparency-hub extractor: EU AI Act Transparency Reports series.

Meta serves Art. 53(1)(d) summaries through signed, expiring CDN URLs embedded in
the hub page's JSON payload (no stable document URLs exist). This module renders the
hub, isolates the "EU AI Act Transparency Reports" series, and captures each edition
keyed on its hub EDITION label (Meta content-addresses every upload, so the CDN
path rotates per re-issue), so signatures and re-uploads never mint versions but
text changes do.

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
        # stable identity = the hub EDITION ("2026 - Muse Spark"), not the CDN
        # path: Meta content-addresses every upload, so the path rotates on
        # each re-issue (21 Aug 2026: all three editions re-uploaded as V1.1/
        # V3.1 at new paths). Keying on the edition keeps one version chain
        # per document; the current path is recorded in the manifest notes.
        from urllib.parse import urlsplit
        path = urlsplit(ed["url"]).path
        model = re.sub(r"^\d{4}\s*-\s*", "", ed["period"])          # "2026 - Muse Spark" -> "Muse Spark"
        stable = f"meta-hub:{ed['period']}"
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
        ext = cap.guess_ext(meta["content_type"], path, raw)
        text, notes = cap.extract_text(raw, ext)
        text_sha = (cap.zip_content_key(notes) if ext == ".zip"
                    else cap.canonical_text_sha(text) if text else None)
        # a re-issued file whose canonical text is unchanged (re-render, metadata
        # churn) is not a new version — same rule as run_capture applies to HTML
        if (text_sha and text_sha == store.last_text_sha(source_id, tslug)):
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
                "stable key = hub edition label (CDN path rotates per re-issue)",
                {"stable_base": path, "hub_period": ed["period"],
                 "edition_key": stable}],
            managed="meta_hub", event_extra={"via": "meta-hub"})
        changed += 1
        print(f"  NEW {ed['period']} sha={sha[:12]} {len(raw):,}B ots={manifest['ots'].get('ok')}")
    print(f"done: {changed} new versions, {errors} fetch errors")
    # non-zero on fetch errors so the workflow's health gate can fire (data
    # already stored/committed for the editions that succeeded)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
