"""Tests for site/build.py's pure render layer and site/lint.py's checks."""
import json
import sys
from pathlib import Path

import pytest

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
    assert "pruned as content-identical noise" in build.prior_cell(SHA, set())


def test_prior_cell_none_is_first_capture():
    assert "first capture of this target" in build.prior_cell(None, {SHA})


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
