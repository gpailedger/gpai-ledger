"""Tests for site/build.py's pure render layer and site/lint.py's checks."""
import json
import sys
from pathlib import Path

import pytest
import capture as cap

from conftest import load_module

ROOT = Path(__file__).resolve().parent.parent
build = load_module(str(ROOT / "site" / "build.py"), "site_build")

# lint.py rewraps sys.stdout at import; detach the wrapper so it never closes
# pytest's capture buffer, and restore the original stream
_orig_stdout = sys.stdout
lint = load_module(str(ROOT / "site" / "lint.py"), "site_lint")
if sys.stdout is not _orig_stdout:
    sys.stdout.detach()
    sys.stdout = _orig_stdout

SHA = "0123456789abcdef" * 4

SRC = {"id": "prov/model", "provider": "Prov", "model": "Model"}


def mk_manifest(**over):
    m = {"stored_as": "raw.pdf", "sha256": SHA, "size_bytes": 16,
         "target_kind": "provider-live", "extraction_notes": [],
         "http": {"url": "https://example.org/doc.pdf",
                  "fetched_at": "2026-08-15T06:00:00Z"}}
    http = over.pop("http", None)
    if http:
        m["http"].update(http)
    m.update(over)
    return m


# --- A. blob_names ---

def test_blob_names_pdf():
    ext, blob, ots = build.blob_names({"stored_as": "raw.pdf", "sha256": SHA})
    assert (ext, blob, ots) == (".pdf", SHA + ".pdf", SHA + ".pdf.ots")


def test_blob_names_html_gets_txt_suffix_but_plain_ots_name():
    # .txt suffix defangs captured HTML; the ots name matches the capture file
    ext, blob, ots = build.blob_names({"stored_as": "raw.html", "sha256": SHA})
    assert (ext, blob, ots) == (".html", SHA + ".html.txt", SHA + ".html.ots")


# --- B. render_version_page ---

def test_version_page_normal_pdf():
    m = mk_manifest()
    out = build.render_version_page(SRC, m, "20260815T060000Z", set(),
                                    "hello text", True, True)
    assert f"href='../../../../../blob/{SHA}.pdf' download" in out
    assert f"sha256sum {SHA}.pdf" in out
    assert f"<tr><th>SHA-256</th><td><code>{SHA}</code></td></tr>" in out
    assert "first capture of this target" in out


def test_version_page_restricted_source():
    src = dict(SRC, restricted="provider objection honored")
    m = mk_manifest()
    out = build.render_version_page(src, m, "20260815T060000Z", set(),
                                    "alpha beta", True, False)
    assert "not served (provider objection honored)" in out
    assert f"{SHA}.pdf</a>" not in out
    assert "<h2>Structured facts</h2>" in out
    assert "obtain the document from the provider or the Wayback snapshot" in out


def test_version_page_cop_doc_warning():
    m = mk_manifest(target_kind="cop-doc")
    out = build.render_version_page(SRC, m, "20260815T060000Z", set(),
                                    "text", True, True)
    assert "this is a GPAI Code of Practice document" in out


def test_version_page_rendered_html_serve_note():
    m = mk_manifest(stored_as="raw.html")
    out = build.render_version_page(SRC, m, "20260815T060000Z", set(),
                                    "text", True, True)
    assert f"blob/{SHA}.html.txt" in out
    assert "served with a .txt suffix" in out


def test_version_page_signed_url_masked_not_linked():
    url = "https://cdn.example.net/doc.pdf?X-Amz-Signature=SECRET123"
    m = mk_manifest(http={"url": url})
    out = build.render_version_page(SRC, m, "20260815T060000Z", set(),
                                    "text", True, True)
    assert "signed URL; token masked, not linked" in out
    assert "SECRET123" not in out
    assert "href='https://cdn.example.net" not in out


# --- C. wayback_cell ---

def _wb(snap=None, ok=None, fetched="2026-08-15T06:00:00Z", rendered=False):
    wb = {}
    if snap:
        wb["snapshot"] = snap
    if ok is not None:
        wb["ok"] = ok
    http = {"fetched_at": fetched}
    if rendered:
        http["rendered"] = True
    return {"wayback": wb, "http": http}


def test_wayback_pre_existing_snapshot_caption():
    m = _wb(snap="https://web.archive.org/web/20260810120000/https://x/d.pdf")
    assert "pre-existing snapshot" in build.wayback_cell(m)


def test_wayback_save_after_capture_caption():
    m = _wb(snap="https://web.archive.org/web/20260820120000/https://x/d.pdf")
    assert "save triggered after capture" in build.wayback_cell(m)


def test_wayback_rendered_caption_wins():
    m = _wb(snap="https://web.archive.org/web/20260810120000/https://x/d.pdf",
            rendered=True)
    assert "separate fetch of a rendered page" in build.wayback_cell(m)


def test_wayback_pending_when_ok_without_snapshot():
    assert build.wayback_cell(_wb(ok=True)) == "saved (snapshot pending)"


def test_wayback_not_saved_when_nothing():
    assert build.wayback_cell(_wb()) == "not saved"


# --- D. prior_cell ---

def test_prior_cell_sha_in_corpus_is_plain():
    assert build.prior_cell(SHA, {SHA}) == f"<code>{SHA}</code>"


def test_prior_cell_pruned_sha_is_annotated():
    # the identity claim is made only for a prune the tool recorded; an
    # unknown removal gets no claim at all
    tool = {"via": "prune_capture", "reason": "r", "ts": "2026-08-21T00:00:00Z"}
    assert "pruned as content-identical noise" in build.prior_cell(SHA, set(), pruned={SHA: tool})
    assert "no prune event names it" in build.prior_cell(SHA, set(), pruned={})


def test_prior_cell_none_is_first_capture():
    assert "first capture of this target" in build.prior_cell(None, {SHA})


def test_prior_cell_scope_repack_is_captioned_as_repack_not_prune():
    out = build.prior_cell(SHA, set(), repacked_shas={SHA})
    assert "scope repack" in out.lower() and "pruned" not in out


# --- C0. escaping of third-party strings ---

def test_jsonld_cannot_break_out_of_its_script_element():
    hostile = "</script><script>alert(1)</script>&"
    head = build.head_meta(jsonld=[{"@type": "BreadcrumbList", "name": hostile}])
    block = head.split('<script type="application/ld+json">')[1].split("</script>")[0]
    assert "<" not in block and ">" not in block and "&" not in block
    assert json.loads(block)["name"] == hostile


def test_url_attr_neutralizes_protocol_relative_destinations():
    assert build.url_attr("//evil.example/x") == "#"
    assert build.url_attr("/local/path") == "/local/path"


def test_extract_display_drops_nul_bytes_and_says_so():
    out = build.extract_display(mk_manifest(), "abc\x00def")
    assert "\x00" not in out and "abcdef" in out and "NUL" in out
    assert "<pre class='extract'>" in out


def test_version_page_restamped_proof_is_captioned_honestly():
    m = mk_manifest(ots={"ok": True, "restamped_at": "2026-08-20T06:00:00Z"})
    out = build.render_version_page(SRC, m, "20260815T060000Z", set(), "hello", True, True)
    assert "stamp submitted 2026-08-20T06:00:00Z" in out and "no later than" in out
    assert "proves the capture time" not in out


# --- E. version_row_html notes ---

def mk_row(**over):
    r = {"ts": "20260815T060000Z", "kind": "provider-live",
         "url": "https://example.org/doc.pdf", "stored_as": "raw.pdf",
         "rendered": False, "sha": "2" * 64, "size": 10, "retired": None,
         "managed": None, "active": True, "txt_size": 5000,
         "prior_sha": None, "text_sha": None, "prior_text_sha": None}
    r.update(over)
    return r


def _first(r):
    return {r["sha"]: r["ts"]}


def test_row_note_ledger_identical_text_overrides_text_sha_difference():
    # stored text hashes differ (different extractor eras) but the ledger's
    # common-extractor verdict says the text is identical: no content-change claim
    r = mk_row(prior_sha="1" * 64, text_sha="a", prior_text_sha="b",
               diff_verdict="identical-text")
    out = build.version_row_html(r, set(), _first(r))
    assert "extracted text is identical" in out and "content changed" not in out


def test_row_note_ledger_changed_reports_word_count():
    r = mk_row(prior_sha="1" * 64, text_sha="a", prior_text_sha="a",
               diff_verdict="changed", diff_words=3)
    out = build.version_row_html(r, set(), _first(r))
    assert "content changed vs the previous capture of this target (3 word(s) differ" in out


def test_row_note_ledger_method_change_is_not_a_content_claim():
    r = mk_row(prior_sha="1" * 64, text_sha="a", prior_text_sha="b",
               stored_as="raw.html", diff_verdict="method-changed")
    out = build.version_row_html(r, set(), _first(r))
    assert "captured with a different method" in out and "content changed" not in out


def test_row_note_non_document_page_says_page_text_not_content():
    r = mk_row(prior_sha="1" * 64, stored_as="raw.html", kind="watch-page",
               url="https://example.org/catalog", diff_verdict="changed", diff_words=7)
    out = build.version_row_html(r, set(), _first(r))
    assert "page text changed vs the previous capture" in out and "content changed" not in out


def test_row_note_unverified_change_is_not_published_as_a_content_change():
    r = mk_row(prior_sha="1" * 64, diff_verdict="changed-unverified", diff_words=2)
    out = build.version_row_html(r, set(), _first(r))
    assert "not verified as a content change" in out and "content changed" not in out


def test_row_note_ledger_no_text_makes_no_content_claim():
    r = mk_row(prior_sha="1" * 64, text_sha=None, prior_text_sha=None,
               diff_verdict="no-text")
    out = build.version_row_html(r, set(), _first(r))
    assert "no extracted text on one side" in out and "content changed" not in out


def test_row_note_inpage():
    r = mk_row()
    out = build.version_row_html(r, {r["url"]}, _first(r))
    assert "document published as in-page web content" in out


def test_row_note_rendered():
    r = mk_row(rendered=True, stored_as="raw.html")
    out = build.version_row_html(r, set(), _first(r))
    assert "rendered page (captured with a browser)" in out


def test_row_note_app_shell():
    r = mk_row(stored_as="raw.html", txt_size=100)
    out = build.version_row_html(r, set(), _first(r))
    assert "mostly empty shell" in out


def test_row_note_bytes_identical_to_first_capture():
    r = mk_row()
    out = build.version_row_html(r, set(), {r["sha"]: "20260814T000000Z"})
    assert "bytes identical to 20260814T000000Z" in out


def test_row_note_content_changed_via_text_sha_pair():
    r = mk_row(prior_text_sha="3" * 64, text_sha="4" * 64)
    out = build.version_row_html(r, set(), _first(r))
    assert "content changed vs the previous capture of this target" in out


def test_row_note_retired_reason_passthrough():
    r = mk_row(retired="superseded: bucket URL rotated")
    out = build.version_row_html(r, set(), _first(r))
    assert "superseded: bucket URL rotated" in out


# --- F. structured_facts + extract_display ---

def test_structured_facts_word_count():
    m = {"size_bytes": 2048, "text_sha256": "8" * 64, "extraction_notes": []}
    out = build.structured_facts(m, "alpha beta gamma delta")
    assert "<tr><th>Extracted text length</th><td>4 words</td></tr>" in out


def test_structured_facts_contained_files_sorted():
    m = {"size_bytes": 2048, "extraction_notes": [
        {"inner_file": "b_report.pdf", "inner_sha256": "9" * 64},
        {"inner_file": "a_summary.pdf", "inner_sha256": "6" * 64}]}
    out = build.structured_facts(m, None)
    assert "<li><code>a_summary.pdf</code>" in out
    assert "<code>" + "6" * 16 + "…</code>" in out
    assert out.index("a_summary.pdf") < out.index("b_report.pdf")


def test_extract_display_zip_shows_only_art53_text():
    m = {"stored_as": "raw.zip", "extraction_notes": [
        {"inner_file": "training_data_summary.pdf", "inner_sha256": "6" * 64},
        {"inner_file": "model_card_ab_2013.pdf", "inner_sha256": "7" * 64}]}
    text = ("===== training_data_summary.pdf =====\n"
            "EU public summary body text\n"
            "===== model_card_ab_2013.pdf =====\n"
            "california only text\n")
    out = build.extract_display(m, text)
    assert "EU public summary body text" in out
    assert "california only text" not in out
    assert "Text of 1 other document(s) omitted" in out
    assert "California AB 2013 disclosure (not Art. 53)" in out


def test_extract_display_no_text_message():
    out = build.extract_display({"stored_as": "raw.pdf"}, None)
    assert "No text extracted for this format" in out


def test_extract_display_truncation_marker():
    text = "x" * (build.EXTRACT_DISPLAY_LIMIT + 10)
    out = build.extract_display({"stored_as": "raw.pdf"}, text)
    assert "Extract truncated at 200,000" in out


# --- G. render_model_page ---

def mk_source(**over):
    s = {"id": "prov/model", "provider": "Prov", "model": "Model",
         "status": "published"}
    s.update(over)
    return s


def test_model_page_missing_with_aial_note():
    src = mk_source(status="missing",
                    aial={"public_summary_date": None,
                          "model_publication_date": "2025-01-01",
                          "eval_page": "https://aial.ie/x"})
    body, _ = build.render_model_page(src, [], {})
    assert "AI Accountability Lab (AIAL) assesses this model" in body


def test_model_page_missing_without_aial_note():
    body, _ = build.render_model_page(mk_source(status="missing"), [], {})
    assert "has not been assessed" in body
    assert "AIAL" not in body


def test_model_page_title_by_status():
    # published models get the search-facing summary title; missing models say
    # "none located" (honest: absence of discovery, not a compliance claim);
    # non-model sources fall back to plain labels without doubled parentheses
    src = mk_source(provider="Meta", model="Llama 5", status="published")
    _, title = build.render_model_page(src, [], {})
    assert title == "Llama 5 training data summary (Meta) | GPAI Ledger"
    src = mk_source(provider="xAI", model="Grok 4", status="missing")
    _, title = build.render_model_page(src, [], {})
    assert title == ("Grok 4 training data summary — none located "
                     "(xAI) | GPAI Ledger")
    src = mk_source(provider="EU", model="Commission template", status="regulatory")
    _, title = build.render_model_page(src, [], {})
    assert title == "Commission template (EU) — GPAI Ledger"
    # parenthesized model names take a middot separator, keeping provider
    # uniqueness without nested parens
    src = mk_source(provider="xAI", model="Model catalog (developer docs)",
                    status="watch")
    _, title = build.render_model_page(src, [], {})
    assert title == "Model catalog (developer docs) · xAI — GPAI Ledger"


# --- H. lint ---

def _page(title, body=""):
    # about.html as plain text satisfies L8 without adding a resolvable-link duty;
    # the description satisfies L13 so fixtures only trip the check under test
    return (f"<html><head><title>{title}</title>"
            f'<meta name="description" content="fixture"></head><body>'
            f"<p>See about.html for verification.</p>{body}</body></html>")


def _mk_dist(tmp_path):
    dist = tmp_path / "site" / "dist"
    (dist / "blob").mkdir(parents=True)
    (dist / "index.html").write_text(_page("GPAI Ledger"), encoding="utf-8")
    return dist


def _vpage(dist, stored_cell, sha64):
    # the real build layout: ledger/<provider>/<model>/v/<capture>/index.html
    vdir = dist / "ledger" / "prov" / "model" / "v" / "20260101T000000Z"
    vdir.mkdir(parents=True)
    body = (f"<table><tr><th>Stored file</th><td>{stored_cell}</td></tr>"
            f"<tr><th>SHA-256</th><td><code>{sha64}</code></td></tr></table>")
    (vdir / "index.html").write_text(_page("Model @ 20260101T000000Z", body),
                                     encoding="utf-8")


def run_lint(monkeypatch, dist):
    monkeypatch.setattr(lint, "DIST", dist)
    lines = []
    monkeypatch.setattr("builtins.print",
                        lambda *a, **k: lines.append(" ".join(map(str, a))))
    rc = lint.main()
    return rc, [ln[2:] for ln in lines if ln.startswith("  L")]


# --- F2. full build: the version-diffs ledger governs change notes and the feed ---

FULL_SRC = {"id": "prov/model", "provider": "Prov", "model": "Model", "status": "published",
            "targets": [{"kind": "provider-live", "url": "https://example.org/doc.pdf"}]}
V1, V2 = "20260801T060000Z", "20260815T060000Z"
# the fixture target must carry the slug the build derives from the registry URL,
# or every fixture row renders as a superseded target and the document table is
# never exercised
SLUG = cap.target_slug("provider-live", FULL_SRC["targets"][0]["url"])


def _build_site(tmp_path, monkeypatch, data_root, vdiffs=None, drift=None):
    root = tmp_path / "root"
    (root / "crawler").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "crawler" / "sources.json").write_text(
        json.dumps({"sources": [FULL_SRC]}), encoding="utf-8")
    if vdiffs is not None:
        (root / "reports" / "version-diffs.json").write_text(json.dumps(vdiffs), encoding="utf-8")
    if drift is not None:
        (root / "reports" / "drift-latest.json").write_text(json.dumps(drift), encoding="utf-8")
    for name, val in (("ROOT", root), ("DATA", data_root),
                      ("DIST", root / "site" / "dist"), ("STATIC", root / "site" / "static")):
        monkeypatch.setattr(build, name, val)
    assert build.main(generated="2026-08-22 00:00 UTC") == 0
    return root / "site" / "dist"


def _two_versions(corpus):
    # stored extracts differ (as two extractor eras would leave them)
    corpus.add_capture(ts=V1, raw=b"%PDF-1.4 v1", text="stable body text", tslug=SLUG)
    corpus.add_capture(ts=V2, raw=b"%PDF-1.4 v2", text="stable body text updated", tslug=SLUG)
    root = corpus.finish()
    key = f"prov/model::{SLUG}"
    st = json.loads((root / "state.json").read_text(encoding="utf-8"))[key]
    return root, st["versions"][0]["dir"], st["versions"][1]["dir"]


def _ledger(verdict, from_dir, to_dir, **extra):
    rec = {"verdict": verdict, "source": "prov/model", "target": SLUG,
           "from_dir": from_dir, "to_dir": to_dir, "same_tool": True,
           "compared_via": "re-extracted from the stored bytes with test"}
    rec.update(extra)
    return {f"prov/model::{SLUG}::{from_dir}>{to_dir}": rec}


def test_build_ledger_identical_text_suppresses_change_note_and_feed_entry(corpus, tmp_path, monkeypatch):
    data, d1, d2 = _two_versions(corpus)
    dist = _build_site(tmp_path, monkeypatch, data, vdiffs=_ledger("identical-text", d1, d2))
    model = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "extracted text is identical" in model and "content changed" not in model
    changes = (dist / "changes" / "index.html").read_text(encoding="utf-8")
    assert "content changed" not in changes


def test_build_ledger_changed_drives_change_note_feed_entry_and_banner(corpus, tmp_path, monkeypatch):
    data, d1, d2 = _two_versions(corpus)
    drift = [{"id": "prov/model", "model": "Model", "verdict": "identical-bytes",
              "self_history": {"verdict": "changed", "word_delta": 1, "from_dir": d1,
                               "to_dir": d2, "same_tool": True,
                               "compared_via": "re-extracted from the stored bytes with test"}}]
    dist = _build_site(tmp_path, monkeypatch, data,
                       vdiffs=_ledger("changed", d1, d2, word_delta=1), drift=drift)
    model = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "content changed vs the previous capture of this target (1 word(s) differ" in model
    assert "Latest version differs from the previous one:" in model
    assert "both extracted with the same tool" in model
    changes = (dist / "changes" / "index.html").read_text(encoding="utf-8")
    assert "content changed (1 word(s) differ in the extracted text) between" in changes


def test_build_without_ledger_record_falls_back_to_text_hashes(corpus, tmp_path, monkeypatch):
    data, _, _ = _two_versions(corpus)
    dist = _build_site(tmp_path, monkeypatch, data)
    model = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "content changed vs the previous capture of this target" in model


def test_build_near_identical_banner_states_observables_only(corpus, tmp_path, monkeypatch):
    data, d1, d2 = _two_versions(corpus)
    drift = [{"id": "prov/model", "model": "Model", "verdict": "near-identical",
              "similarity": 0.9983, "word_delta": 2, "same_tool": False,
              "compared_via": "re-extracted from the stored bytes: pypdf 6 (archive) vs utf-8 decode (live)"}]
    dist = _build_site(tmp_path, monkeypatch, data, drift=drift)
    model = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "Small difference observed:" in model and "2 word(s)" in model and "0.9983" in model
    assert "same tool" not in model        # two extractors: never claimed


def test_build_pure_move_is_reported_as_reordering_not_change(corpus, tmp_path, monkeypatch):
    data, d1, d2 = _two_versions(corpus)
    dist = _build_site(tmp_path, monkeypatch, data,
                       vdiffs=_ledger("changed", d1, d2, word_delta=0, moved_words=5))
    model = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "text re-ordered vs the previous capture of this target (5 word(s) moved, none changed)" in model
    assert "content changed" not in model
    assert "content changed" not in (dist / "changes" / "index.html").read_text(encoding="utf-8")


def test_build_retired_source_keeps_pages_with_a_note_and_leaves_the_count(corpus, tmp_path, monkeypatch):
    data, _, _ = _two_versions(corpus)
    retired = dict(FULL_SRC, retired="retired 2026-08-22: upstream eval removed")
    monkeypatch.setattr(sys.modules[__name__], "FULL_SRC", retired)
    dist = _build_site(tmp_path, monkeypatch, data)
    model = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "No longer tracked:" in model and "upstream eval removed" in model
    assert "permalinks stay valid" in model
    status = (dist / "status" / "index.html").read_text(encoding="utf-8")
    assert "(no longer tracked)" in status
    assert "0 models tracked" in (dist / "index.html").read_text(encoding="utf-8")


def test_lint_l2_l6_ignore_third_party_extract_but_police_generator_text(tmp_path, monkeypatch):
    dist = _mk_dist(tmp_path)
    page = dist / "methodology" / "index.html"
    page.parent.mkdir()
    page.write_text(_page("Methodology", "<pre class='extract'>provider text with "
                          "â€ mojibake and {placeholder}</pre>"), encoding="utf-8")
    rc, findings = run_lint(monkeypatch, dist)
    assert not [f for f in findings if f.startswith(("L2", "L6"))]
    page.write_text(_page("Methodology", "<p>{placeholder}</p>"), encoding="utf-8")
    rc, findings = run_lint(monkeypatch, dist)
    assert any(f.startswith("L6") for f in findings)


def test_lint_covers_the_404_page(tmp_path, monkeypatch):
    dist = _mk_dist(tmp_path)
    (dist / "404.html").write_text(_page("Not found", "<p>{placeholder}</p>"), encoding="utf-8")
    rc, findings = run_lint(monkeypatch, dist)
    assert any(f.startswith("L6") and "404.html" in f for f in findings)


def test_lint_l5_doc_blob_without_text_or_note(tmp_path, monkeypatch):
    dist = _mk_dist(tmp_path)
    (dist / "blob" / f"{SHA}.pdf").write_bytes(b"%PDF-1.4")
    _vpage(dist, f"<a href='../../../../../blob/{SHA}.pdf' download>"
                 f"{SHA}.pdf</a> (8 bytes)", SHA)
    rc, findings = run_lint(monkeypatch, dist)
    assert rc == 1
    assert len(findings) == 1 and findings[0].startswith("L5 document version")


def test_lint_l9_blob_exists_but_not_linked(tmp_path, monkeypatch):
    dist = _mk_dist(tmp_path)
    (dist / "blob" / f"{SHA}.pdf").write_bytes(b"%PDF-1.4")
    _vpage(dist, "raw.pdf (8 bytes)", SHA)
    rc, findings = run_lint(monkeypatch, dist)
    assert rc == 1
    assert len(findings) == 1 and findings[0].startswith("L9 blob exists")


def test_lint_l10_executable_html_in_blob(tmp_path, monkeypatch):
    dist = _mk_dist(tmp_path)
    (dist / "blob" / f"{SHA}.html").write_text("<html>x</html>", encoding="utf-8")
    rc, findings = run_lint(monkeypatch, dist)
    assert rc == 1
    assert findings == [f"L10 captured HTML served executable: blob/{SHA}.html"]


def test_lint_l11_orphan_state_key(tmp_path, monkeypatch):
    dist = _mk_dist(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text(json.dumps({
        "ghost/model::provider-live-x": {"versions": []},
        "prov/model::provider-live-y": {"versions": []}}), encoding="utf-8")
    (tmp_path / "crawler").mkdir()
    (tmp_path / "crawler" / "sources.json").write_text(
        json.dumps({"sources": [{"id": "prov/model"}]}), encoding="utf-8")
    rc, findings = run_lint(monkeypatch, dist)
    assert rc == 1
    assert findings == ["L11 state entry has no registry source "
                        "(renders nowhere): ghost/model::provider-live-x"]


def test_lint_l1_broken_internal_link(tmp_path, monkeypatch):
    dist = _mk_dist(tmp_path)
    (dist / "index.html").write_text(
        _page("GPAI Ledger", "<a href='ledger/missing/index.html'>gone</a>"),
        encoding="utf-8")
    rc, findings = run_lint(monkeypatch, dist)
    assert rc == 1
    assert findings == ["L1 broken link on index.html: ledger/missing/index.html"]


def test_lint_l7_duplicate_titles(tmp_path, monkeypatch):
    dist = _mk_dist(tmp_path)
    (dist / "about.html").write_text(_page("GPAI Ledger"), encoding="utf-8")
    rc, findings = run_lint(monkeypatch, dist)
    assert rc == 1
    assert findings == ["L7 duplicate title 'GPAI Ledger': index.html vs about.html"]


def test_lint_real_dist_is_clean(monkeypatch):
    real = ROOT / "site" / "dist"
    if not real.exists():
        pytest.skip("site/dist not built")
    rc, findings = run_lint(monkeypatch, real)
    assert rc == 0 and findings == []


def test_fixture_rows_render_in_the_document_table_not_as_superseded(corpus, tmp_path, monkeypatch):
    data_root, d1, d2 = _two_versions(corpus)
    dist = _build_site(tmp_path, monkeypatch, data_root, vdiffs=_ledger("changed", d1, d2, word_delta=1))
    page = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "<h2>Document versions</h2>" in page
    assert "Captures of superseded target URLs" not in page


def test_build_fails_closed_when_a_state_version_has_no_manifest(corpus, tmp_path, monkeypatch):
    data_root, _d1, d2 = _two_versions(corpus)
    (data_root / d2 / "manifest.json").unlink()
    root = tmp_path / "root"
    (root / "crawler").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "crawler" / "sources.json").write_text(
        json.dumps({"sources": [FULL_SRC]}), encoding="utf-8")
    for name, val in (("ROOT", root), ("DATA", data_root),
                      ("DIST", root / "site" / "dist"), ("STATIC", root / "site" / "static")):
        monkeypatch.setattr(build, name, val)
    assert build.main(generated="2026-08-22 00:00 UTC") == 1


def test_lint_reports_the_built_size_and_guards_the_pages_limit(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<title>x</title><h1>x</h1>", encoding="utf-8")
    (dist / "big.bin").write_bytes(b"0" * 2_000_000)
    monkeypatch.setattr(lint, "DIST", dist)
    monkeypatch.setattr(lint, "DIST_SIZE_LIMIT_MB", 1)
    monkeypatch.delenv("GPAI_SITE_URL", raising=False)
    printed = []
    monkeypatch.setattr(lint, "print", lambda *a, **k: printed.append(" ".join(map(str, a))),
                        raising=False)
    assert lint.main() == 1
    out = "\n".join(printed)
    assert "built site 2.0 MB" in out and "L16 built site is 2 MB" in out
