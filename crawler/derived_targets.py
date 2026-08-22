"""Capture documents whose URLs must be re-mined from a watched page each run.

Some providers publish documents only behind rotating URLs. For each such source
this module reads the CURRENT sweep's rendered capture of the watch page, mines the
document URL, fetches the document, and stores it keyed on a STABLE identifier so
rotating signatures never mint versions but content changes do.

Covered:
- Cohere Command A+: docs page embeds a pre-signed S3 URL (expires weekly);
  stable key = the unsigned S3 path.
- Anthropic trust-center bundle: portal page carries /doc/trust-zip?r=<token>
  (token rotates); stable key = the unsigned bundle path.

Run after the main sweep: python crawler/derived_targets.py
Exits non-zero if a miner finds no URL (watch-page layout changed) OR a mined
fetch errored, so the workflow surfaces the failure — after the corpus commit
step has already run.
"""
import re
import sys
from pathlib import Path

import capture as cap

DATA = Path(__file__).resolve().parent.parent / "data"


def render_watch_page(url: str):
    """Render the watch page live this run. Reading the last STORED capture is wrong:
    rotating signatures/tokens live in href/script attributes that the text-dedupe
    never sees, so a stored capture can carry an expired URL. Rendering fresh each run
    (this runs in the CI job that already has Chromium) always yields a current URL."""
    try:
        dom, _ = cap.fetch_rendered(url)
        return dom.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        print(f"  render failed for {url}: {exc!r}")
        return None


def store_document(store: cap.Store, source_id: str, provider: str, model: str,
                   fetch_url: str, stable_key: str, note: str) -> str:
    """Fetch via a mined URL, dedupe and store keyed on stable_key.
    Returns "new" | "unchanged" | "error"."""
    tslug = cap.target_slug("provider-live", stable_key)
    try:
        raw, meta = cap.fetch(fetch_url)
    except Exception as exc:  # noqa: BLE001
        store.event(source=source_id, target=tslug, url=stable_key, kind="provider-live",
                    outcome="error", error=repr(exc), via="derived-targets")
        print(f"  ERROR {source_id}: {exc!r}")
        return "error"
    ext = cap.guess_ext(meta["content_type"], stable_key, raw)
    excluded_members = None
    if ext == ".zip":
        # Scope filter FIRST: provider bundles can contain confidential-marked
        # documents; only Art. 53 members are stored/served, the rest recorded by
        # name+hash. A bundle over the zip caps is an error event, not a version.
        try:
            raw, excluded_members = cap.filter_zip_art53(raw)
        except Exception as exc:  # noqa: BLE001
            store.event(source=source_id, target=tslug, url=stable_key,
                        kind="provider-live", outcome="error",
                        error=f"bundle rejected: {exc!r}", via="derived-targets")
            print(f"  ERROR {source_id}: bundle rejected: {exc!r}")
            return "error"
    text, notes = cap.extract_text(raw, ext)
    sha = cap.sha256_hex(raw)
    if ext == ".zip":
        # dedupe on the inner-member hash set of the FILTERED zip — byte-exact per
        # member and immune to regenerated-archive byte churn
        text_sha = cap.zip_content_key(notes)
        if text_sha == store.last_text_sha(source_id, tslug):
            store.event(source=source_id, target=tslug, url=stable_key,
                        kind="provider-live", outcome="unchanged-content",
                        sha256=sha, text_sha256=text_sha, via="derived-targets")
            print(f"  unchanged-content {source_id} (zip regenerated, contents identical)")
            return "unchanged"
    else:
        text_sha = cap.canonical_text_sha(text) if text else None
        if sha == store.last_sha(source_id, tslug):
            store.event(source=source_id, target=tslug, url=stable_key,
                        kind="provider-live", outcome="unchanged", sha256=sha,
                        via="derived-targets")
            print(f"  unchanged {source_id} ({sha[:12]})")
            return "unchanged"
        # a mined URL can start answering with an HTML page (landing/error page
        # swap); HTML bytes churn per fetch, so dedupe those on text
        if (ext == ".html" and text_sha
                and text_sha == store.last_text_sha(source_id, tslug)):
            store.event(source=source_id, target=tslug, url=stable_key,
                        kind="provider-live", outcome="unchanged-content",
                        sha256=sha, text_sha256=text_sha, via="derived-targets")
            print(f"  unchanged-content {source_id}")
            return "unchanged"
    extra_notes = [note, {"stable_key": stable_key}]
    if excluded_members:
        extra_notes.append({"members_not_stored": excluded_members})
    rel, manifest = cap.store_new_version(
        store, source_id=source_id, provider=provider, model=model,
        kind="provider-live", tslug=tslug, event_url=stable_key,
        raw=raw, meta=meta, ext=ext, text=text, notes=notes, text_sha=text_sha,
        wayback_url=fetch_url, extra_notes=extra_notes,
        managed="derived_targets", event_extra={"via": "derived-targets"})
    print(f"  NEW {source_id} sha={sha[:12]} {len(raw):,}B ots={manifest['ots'].get('ok')}")
    return "new"


def cohere(store: cap.Store) -> bool:
    html = render_watch_page("https://docs.cohere.com/docs/command-a-plus")
    if not html:
        return False
    # host anchored to the bucket's amazonaws.com origin: a link injected into the
    # docs page must not redirect the miner to a look-alike host
    m = re.search(r'https://fdr-prod-docs-files-public\.s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com/'
                  r'[^"\s?]*eu-ai-public-summary[^"\s]*', html)
    if not m:
        print("  cohere: no signed summary URL on docs page — layout changed?")
        return False
    signed = m.group(0).replace("&amp;", "&")
    status = store_document(store, "cohere/command-a-plus", "Cohere", "Command A Plus",
                            signed, signed.split("?")[0],
                            "downloaded via the document link publicly offered on the provider's docs page (the link embeds an expiring signature)")
    return status != "error"


def anthropic_bundle(store: cap.Store) -> bool:
    html = render_watch_page("https://trust.anthropic.com/resources")
    if not html:
        return False
    m = re.search(r'/doc/trust-zip\?r=[A-Za-z0-9]+', html)
    if not m:
        print("  anthropic: no trust-zip link in portal DOM — layout changed?")
        return False
    url = "https://trust.anthropic.com" + m.group(0)
    status = store_document(store, "anthropic/trust-center-bundle", "Anthropic",
                            "Trust-center document bundle",
                            url, "https://trust.anthropic.com/doc/trust-zip",
                            "downloaded via the bulk-download link publicly offered on the provider's trust-center page (the link embeds a rotating token)")
    return status != "error"


def main() -> int:
    store = cap.Store(DATA)
    ok = True
    print("cohere/command-a-plus:")
    ok = cohere(store) and ok
    print("anthropic/trust-center-bundle:")
    ok = anthropic_bundle(store) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
