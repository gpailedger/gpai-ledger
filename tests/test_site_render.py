"""Tests for site/build.py's pure render layer and site/lint.py's checks."""
import json
import sys
from pathlib import Path

import pytest
import capture as cap

from conftest import load_module

NL = chr(10)

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
    # source-neutral: a withheld capture is not always the provider's own file
    assert "obtain the file from the target address above" in out


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


def _build_site(tmp_path, monkeypatch, data_root, vdiffs=None, drift=None,
                src=None):
    root = tmp_path / "root"
    (root / "crawler").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "crawler" / "sources.json").write_text(
        json.dumps({"sources": [src or FULL_SRC]}), encoding="utf-8")
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


def _version_page(sha, linked_sha, ots_name=None):
    link = f"<a href='../../../../../blob/{linked_sha}.pdf' download>{linked_sha}.pdf</a>"
    ots = (f"<a href='../../../../../blob/{ots_name}' download>{ots_name}</a>"
           if ots_name else "not stamped")
    return _page("Model version", f"<table><tr><th>SHA-256</th><td><code>{sha}</code></td></tr>"
                                  f"<tr><th>Stored file</th><td>{link}</td></tr>"
                                  f"<tr><th>OpenTimestamps proof</th><td>{ots}</td></tr></table>"
                                  f"<h2>Extracted text</h2><pre class='extract'>t</pre>")


def _with_version(dist, slug, sha, linked_sha, ots_name=None):
    d = dist / "ledger" / "prov" / "model" / "v" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(_version_page(sha, linked_sha, ots_name), encoding="utf-8")
    (dist / "blob").mkdir(exist_ok=True)
    (dist / "blob" / f"{linked_sha}.pdf").write_bytes(b"%PDF")
    return d


def test_lint_l17_version_page_must_link_its_own_proof(tmp_path, monkeypatch):
    dist = _mk_dist(tmp_path)
    sha, slug = "a" * 64, "20260811T100000Z"
    _with_version(dist, slug, sha, sha)
    (dist / "blob" / f"{sha}.pdf.{slug}.ots").write_bytes(b"\x00ots")
    rc, findings = run_lint(monkeypatch, dist)
    assert any(f.startswith("L17") for f in findings)
    _with_version(dist, slug, sha, sha, ots_name=f"{sha}.pdf.{slug}.ots")
    rc, findings = run_lint(monkeypatch, dist)
    assert not [f for f in findings if f.startswith("L17")]


def test_lint_l18_hash_cell_must_match_the_linked_blob(tmp_path, monkeypatch):
    dist = _mk_dist(tmp_path)
    _with_version(dist, "20260811T100000Z", "a" * 64, "b" * 64)
    rc, findings = run_lint(monkeypatch, dist)
    assert any(f.startswith("L18") for f in findings)


def test_lint_l19_real_build_must_have_a_page_per_version(tmp_path, monkeypatch):
    dist = _mk_dist(tmp_path)
    _with_version(dist, "20260811T100000Z", "a" * 64, "a" * 64)
    (dist / "ledger.json").write_text("[]", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "state.json").write_text(json.dumps({"prov/model::provider-live-x": {
        "versions": [{"sha256": "a" * 64, "dir": "d1"}, {"sha256": "b" * 64, "dir": "d2"}]}}),
        encoding="utf-8")
    monkeypatch.setattr(lint, "DATA", data)
    rc, findings = run_lint(monkeypatch, dist)
    assert any(f.startswith("L19") and "1 version pages built for 2 versions" in f for f in findings)
    (dist / "ledger.json").unlink()            # not a real build: L19 does not apply
    rc, findings = run_lint(monkeypatch, dist)
    assert not [f for f in findings if f.startswith("L19")]


def test_dataset_export_carries_blob_proof_and_snapshot_urls(corpus, tmp_path, monkeypatch):
    data, d1, d2 = _two_versions(corpus)
    dist = _build_site(tmp_path, monkeypatch, data)
    rows = json.loads((dist / "ledger.json").read_text(encoding="utf-8"))["records"]
    assert len(rows) == 2
    for r in rows:
        assert r["blob_url"].endswith(f"blob/{r['sha256']}.pdf")
        assert r["ots_url"].endswith(".ots") and r["sha256"] in r["ots_url"]
        assert "wayback_snapshot" in r


def _absence_event(data_root, ts, absence="confirmed", outcome="error", by=("operator",)):
    ev = {"ts": ts, "source": "prov/model", "target": SLUG, "outcome": outcome,
          "url": "https://ex.org/doc.pdf", "kind": "provider-live"}
    if outcome == "error":
        ev["error"] = "HTTP 404 for https://ex.org/doc.pdf"
        ev["absence"] = absence
        ev["confirmed_by"] = list(by)
    with (Path(data_root) / "events.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(ev) + "\n")


def test_site_says_when_a_provider_copy_is_confirmed_gone(corpus, tmp_path, monkeypatch):
    data, _d1, _d2 = _two_versions(corpus)
    _absence_event(data, "2026-08-28T08:52:51Z")
    dist = _build_site(tmp_path, monkeypatch, data)
    model = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "The provider's copy of this document no longer resolves." in model
    assert "28 Aug 2026" in model
    assert "second, unrelated network" in model
    assert "https://ex.org/doc.pdf" in model
    # the archived evidence is still presented, and no version was withdrawn
    assert "Document versions" in model
    status = (dist / "status" / "index.html").read_text(encoding="utf-8")
    assert "(a tracked address no longer resolves)" in status


def test_no_gone_banner_when_the_absence_was_never_confirmed(corpus, tmp_path, monkeypatch):
    data, _d1, _d2 = _two_versions(corpus)
    _absence_event(data, "2026-08-28T08:52:51Z", absence="persistent", by=())
    dist = _build_site(tmp_path, monkeypatch, data)
    model = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "no longer resolves" not in model


def test_gone_banner_disappears_once_the_document_is_seen_again(corpus, tmp_path, monkeypatch):
    data, _d1, _d2 = _two_versions(corpus)
    _absence_event(data, "2026-08-26T08:00:00Z")
    _absence_event(data, "2026-08-28T09:00:00Z", outcome="live-attested")
    dist = _build_site(tmp_path, monkeypatch, data)
    model = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "no longer resolves" not in model
    assert "(a tracked address no longer resolves)" not in (
        dist / "status" / "index.html").read_text(encoding="utf-8")


def test_gone_banner_names_the_witness_when_the_archive_confirmed_it(corpus, tmp_path, monkeypatch):
    data, _d1, _d2 = _two_versions(corpus)
    _absence_event(data, "2026-08-28T08:52:51Z", by=("witness",))
    dist = _build_site(tmp_path, monkeypatch, data)
    model = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "independent Internet Archive capture" in model
    assert "second, unrelated network" not in model


# --- does the recorded snapshot witness THIS capture? ---

def test_wayback_cell_flags_a_snapshot_of_a_redirect_target():
    m = _wb(snap="https://web.archive.org/web/20260820120000/https://cdn.x/d.pdf")
    m["wayback"]["same_url"] = False
    out = build.wayback_cell(m)
    assert "followed a redirect" in out and "not the tracked URL" in out


def test_wayback_cell_says_nothing_about_redirects_when_the_url_matches():
    m = _wb(snap="https://web.archive.org/web/20260820120000/https://x/d.pdf")
    m["wayback"]["same_url"] = True
    assert "followed a redirect" not in build.wayback_cell(m)


def test_only_a_snapshot_taken_around_the_capture_witnesses_it():
    # a save is triggered right after the fetch, so a real witness is minutes old
    concurrent = _wb(snap="https://web.archive.org/web/20260815060312/https://x/d.pdf", ok=True)
    assert build.wayback_witnesses(concurrent) is True


def test_a_snapshot_from_either_side_of_the_capture_does_not_witness_it():
    # earlier: the Archive returned a pre-existing capture instead of crawling.
    # later: a backlog drain archived the URL as it is now, not what we stored.
    older = _wb(snap="https://web.archive.org/web/20260810120000/https://x/d.pdf", ok=True)
    newer = _wb(snap="https://web.archive.org/web/20260820120000/https://x/d.pdf", ok=True)
    none = {"wayback": {"ok": False}, "http": {"fetched_at": "2026-08-15T06:00:00Z"}}
    assert build.wayback_witnesses(older) is False
    assert build.wayback_witnesses(newer) is False
    assert build.wayback_witnesses(none) is None


def test_a_recorded_fresh_flag_does_not_override_the_timestamps():
    # `fresh` answers "did SPN crawl anew?"; witnessing answers "was it taken
    # when we fetched?" — the published claim is the second one
    m = _wb(snap="https://web.archive.org/web/20260810120000/https://x/d.pdf", ok=True)
    m["wayback"]["fresh"] = True
    assert build.wayback_witnesses(m) is False


def test_dataset_export_reports_snapshots_that_do_not_witness_the_capture(
        corpus, tmp_path, monkeypatch):
    corpus.add_capture(tslug=SLUG, ts=V1, raw=b"%PDF-1.4 a", text="a",
                       wayback={"ok": True, "snapshot":
                                "https://web.archive.org/web/20260101000000/https://example.org/doc.pdf"})
    data = corpus.finish()
    dist = _build_site(tmp_path, monkeypatch, data)
    rows = json.loads((dist / "ledger.json").read_text(encoding="utf-8"))["records"]
    assert [r["wayback_witnesses_capture"] for r in rows] == [False]


def test_a_mirror_going_dark_is_not_reported_as_the_providers_copy(corpus, tmp_path, monkeypatch):
    # a confirmed absence on AIAL's mirror says nothing about the provider
    data, _d1, _d2 = _two_versions(corpus)
    ev = {"ts": "2026-08-28T08:52:51Z", "source": "prov/model", "target": SLUG,
          "outcome": "error", "url": "https://aial.ie/archive/x.pdf",
          "kind": "aial-archive", "error": "HTTP 404", "absence": "confirmed",
          "confirmed_by": ["witness"]}
    with (Path(data) / "events.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(ev) + "\n")
    dist = _build_site(tmp_path, monkeypatch, data)
    model = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "third-party archived copy" in model
    # the wording may mention the provider's copy only to say this is NOT it
    assert "The provider's copy of this document no longer resolves." not in model


def test_the_corroboration_sentence_names_only_the_vantages_that_gave_it():
    assert build.gone_corroboration(["witness"]) == (
        "corroborated by an independent Internet Archive capture")
    assert "second, unrelated network" in build.gone_corroboration(["operator"])
    both = build.gone_corroboration(["witness", "operator"])
    assert "Internet Archive" in both and "second, unrelated network" in both
    # an empty or unrecognised value must not claim both
    for odd in ([], ["martian"], None):
        assert build.gone_corroboration(odd) == "corroborated by a second, independent check"


def test_a_host_being_down_does_not_advance_last_checked(corpus, tmp_path, monkeypatch):
    data, _d1, _d2 = _two_versions(corpus)
    with (Path(data) / "events.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"ts": "2026-09-30T06:00:00Z", "source": "prov/model",
                             "target": SLUG, "outcome": "host-unreachable",
                             "host": "dead.example", "after_failures": 3}) + "\n")
    dist = _build_site(tmp_path, monkeypatch, data)
    model = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    assert "2026-09-30" not in model, "a skipped target was published as checked"


def test_a_third_party_evaluation_is_never_counted_as_the_providers_document():
    ev = mk_row(kind="aial-eval", stored_as="raw.yaml",
                url="https://raw.githubusercontent.com/o/r/main/evals/x.yaml")
    doc = mk_row(kind="provider-live", stored_as="raw.pdf")
    assert build.is_document(ev, set()) is False
    assert build.is_document(doc, set()) is True
    # and it must not inflate the document count shown next to the model
    assert build.distinct_documents([ev], set()) == 0
    assert build.distinct_documents([ev, doc], set()) == 1


def test_an_evaluation_is_shown_as_the_assessment_it_is():
    ev = mk_row(kind="aial-eval", stored_as="raw.yaml",
                url="https://raw.githubusercontent.com/o/r/main/evals/x.yaml")
    html = "".join(build.render_version_sections([ev], set()))
    assert "Third-party evaluation" in html
    assert "not a legal determination" in html
    assert "not the provider's document" in html
    assert "Document versions" not in html
    assert "Watch-surface captures" not in html


# --- F5. a third party's own research is archived, not republished -----------

EVAL_URL = ("https://raw.githubusercontent.com/AIAccountabilityLab/"
            "gpai-training-transparency/main/evals/model.yaml")
EVAL_SLUG = cap.target_slug("aial-eval", EVAL_URL)
# a line that exists ONLY in AIAL's file: if it reaches the built site anywhere,
# the ledger has republished their work
EVAL_TEXT = ("model_name: Model" + chr(10) + "scores:" + chr(10) +
             "  clarity: 9" + chr(10) +
             "  note: AIALS-OWN-ASSESSMENT-PROSE" + chr(10))
SRC_WITH_EVAL = dict(FULL_SRC, targets=[
    {"kind": "provider-live", "url": "https://example.org/doc.pdf"},
    {"kind": "aial-eval", "url": EVAL_URL},
])


def _corpus_with_eval(corpus):
    corpus.add_capture(ts=V1, raw=b"%PDF-1.4 the provider summary",
                       text="PROVIDERS-OWN-SUMMARY-PROSE", tslug=SLUG)
    corpus.add_capture(ts=V1, raw=EVAL_TEXT.encode("utf-8"), ext=".yaml",
                       text=EVAL_TEXT, kind="aial-eval", url=EVAL_URL,
                       tslug=EVAL_SLUG)
    corpus.finish()


def test_an_evaluations_bytes_and_words_never_reach_the_built_site(corpus, tmp_path,
                                                                   monkeypatch):
    # AIAL's scored rubric is their research output, published with no licence
    # granting redistribution: the ledger proves what it holds without serving it
    _corpus_with_eval(corpus)
    dist = _build_site(tmp_path, monkeypatch, corpus.root, src=SRC_WITH_EVAL)

    served = sorted(p.name for p in (dist / "blob").glob("*"))
    assert not [n for n in served if n.endswith((".yaml", ".yml"))],         f"AIAL's file was copied into the site: {served}"
    for page in dist.rglob("*.html"):
        assert "AIALS-OWN-ASSESSMENT-PROSE" not in page.read_text(encoding="utf-8"),             f"AIAL's assessment was republished on {page.name}"
    assert "AIALS-OWN-ASSESSMENT-PROSE" not in (dist / "ledger.json").read_text(
        encoding="utf-8")


def test_withholding_one_capture_does_not_withhold_the_providers_document(
        corpus, tmp_path, monkeypatch):
    # the restriction is per capture: the same source serves the provider's own
    # document in full while withholding a third party's assessment of it
    _corpus_with_eval(corpus)
    dist = _build_site(tmp_path, monkeypatch, corpus.root, src=SRC_WITH_EVAL)
    served = sorted(p.name for p in (dist / "blob").glob("*"))
    assert [n for n in served if n.endswith(".pdf")],         f"the provider's document stopped being served: {served}"
    pages = {p: p.read_text(encoding="utf-8") for p in dist.rglob("index.html")}
    assert any("PROVIDERS-OWN-SUMMARY-PROSE" in h for h in pages.values()),         "the provider's own document stopped being shown in full"


def test_a_withheld_capture_still_proves_what_the_ledger_holds(corpus, tmp_path,
                                                               monkeypatch):
    _corpus_with_eval(corpus)
    dist = _build_site(tmp_path, monkeypatch, corpus.root, src=SRC_WITH_EVAL)
    ev = [h for h in (p.read_text(encoding="utf-8") for p in dist.rglob("index.html"))
          if "not served (third-party research" in h]
    assert len(ev) == 1, "expected exactly one withheld version page"
    html = ev[0]
    assert "<h2>Structured facts</h2>" in html
    assert "Canonical text SHA-256" in html
    # the verify instruction must not tell a reader to get AIAL's file "from the
    # provider" — it never came from the provider
    assert "obtain the document from the provider" not in html
    assert "obtain the file from the target address above" in html


def test_the_dataset_never_offers_a_download_for_a_withheld_capture(corpus, tmp_path,
                                                                    monkeypatch):
    _corpus_with_eval(corpus)
    dist = _build_site(tmp_path, monkeypatch, corpus.root, src=SRC_WITH_EVAL)
    recs = json.loads((dist / "ledger.json").read_text(encoding="utf-8"))["records"]
    by_kind = {r["kind"]: r for r in recs}
    assert by_kind["aial-eval"]["blob_url"] is None
    # the facts that make the record verifiable are still there
    assert by_kind["aial-eval"]["sha256"] and by_kind["aial-eval"]["text_sha256"]
    assert by_kind["provider-live"]["blob_url"], "the provider's blob went missing"


def test_restriction_is_per_capture_not_per_source():
    src, ev = {}, {"target_kind": "aial-eval"}
    doc = {"target_kind": "provider-live"}
    assert build.restriction_of(src, ev)
    assert build.restriction_of(src, doc) is None
    # an objecting provider still restricts everything under that source
    objecting = {"restricted": "provider objection"}
    assert build.restriction_of(objecting, doc) == "provider objection"


def test_a_kind_is_only_unrestricted_deliberately():
    # dropping aial-eval from this map republishes AIAL's research: it may only
    # happen once they have granted permission
    assert "aial-eval" in build.RESTRICTED_KINDS


def test_a_removed_evaluation_does_not_read_as_the_providers_document_vanishing():
    note = build.GONE_WORDING["aial-eval"]
    assert "not the" in note and "provider's document" in note


def test_a_withheld_page_says_the_true_reason_for_withholding():
    # AIAL never asked us to withhold anything: claiming they did would be a false
    # statement about a real organisation on a page that exists to be accurate
    ev = build.structured_facts({"size_bytes": 10, "text_sha256": "a" * 64,
                                 "target_kind": "aial-eval"}, "one two")
    assert "At the provider's request" not in ev
    assert "does not redistribute it" in ev
    doc = build.structured_facts({"size_bytes": 10, "text_sha256": "a" * 64,
                                  "target_kind": "provider-live"}, "one two")
    assert "At the provider's request" in doc


def test_lint_knows_every_section_the_builder_can_emit():
    # a model page whose captures render under a heading lint does not know is
    # reported as having no semantic section — which blocked a real deploy
    import re
    rows = [mk_row(),                                             # document
            mk_row(kind="watch-page", stored_as="raw.html",
                   url="https://example.org/hub"),                # watch surface
            mk_row(kind="aial-eval", stored_as="raw.yaml",
                   url="https://raw.githubusercontent.com/o/r/main/evals/x.yaml"),
            mk_row(ts="20260101T000000Z", active=False)]          # superseded
    html = "".join(build.render_version_sections(rows, set()))
    headings = set(re.findall(r"<h2>([^<]+)</h2>", html))
    assert headings, "no sections rendered — the fixture stopped exercising this"
    unknown = headings - set(lint.SEMANTIC_SECTIONS)
    assert not unknown, f"lint would report these as no-section: {sorted(unknown)}"


def test_a_header_parameter_does_not_decide_what_a_capture_is_stored_as():
    # "text/plain" and "text/plain; charset=utf-8" describe the same file; the
    # ledger stored them differently, so an AIAL evaluation landed as .txt —
    # which site/build.py counts as a document format
    y = "https://raw.githubusercontent.com/o/r/main/evals/a.yaml"
    assert cap.guess_ext("text/plain", y, b"model_name: x") == ".yaml"
    assert cap.guess_ext("text/plain; charset=utf-8", y, b"model_name: x") == ".yaml"
    # a generic type defers to a specific suffix, a specific type does not
    assert cap.guess_ext("text/plain", "https://x/notes.txt", b"hi") == ".txt"
    assert cap.guess_ext("text/plain", "https://x/readme", b"hi") == ".txt"
    assert cap.guess_ext("application/pdf", "https://x/doc.html", b"%PDF-1.4") == ".pdf"
    assert cap.guess_ext("application/pdf; charset=binary", "https://x/d.pdf",
                         b"%PDF-1.4") == ".pdf"
    # and the stored extension decides document-hood, so it must stay out of it
    assert ".yaml" not in build.DOC_SUFFIXES and ".txt" in build.DOC_SUFFIXES


def test_no_form_of_a_third_partys_assessment_is_ever_a_document_version():
    for kind in ("aial-eval", "aial-eval-history", "aial-eval-page", "aial-method"):
        row = mk_row(kind=kind, stored_as="raw.yaml", url="https://x/e")
        assert build.is_document(row, set()) is False, kind
        assert build.distinct_documents([row], set()) == 0, kind


def test_the_history_of_a_grade_renders_with_the_evaluation_not_as_a_document():
    rows = [mk_row(kind="aial-eval", stored_as="raw.yaml", url="https://x/e.yaml"),
            mk_row(ts="20260701T000000Z", kind="aial-eval-history",
                   stored_as="raw.yaml", url="https://x/c1/e.yaml"),
            mk_row(ts="20260702T000000Z", kind="aial-eval-page",
                   stored_as="raw.html", url="https://aial.ie/evals/x/")]
    html = "".join(build.render_version_sections(rows, set()))
    assert "Third-party evaluation" in html
    assert "Document versions" not in html and "Watch-surface captures" not in html


def test_the_scoring_framework_gets_its_own_section():
    rows = [mk_row(kind="aial-method", stored_as="raw.html",
                   url="https://aial.ie/research/gpai-training-transparency/methodology")]
    html = "".join(build.render_version_sections(rows, set()))
    assert "The framework these evaluations use" in html
    assert "not republished" in html
    assert "Watch-surface captures" not in html


def test_every_restricted_kind_is_a_kind_the_site_can_name_and_place():
    # a kind withheld but unlabelled renders its raw key to a reader, and a kind
    # the section logic does not know renders under no heading at all
    known = set(build.THIRD_PARTY_EVAL_KINDS) | set(build.THIRD_PARTY_METHOD_KINDS)
    for kind in cap.RESTRICTED_KINDS:
        assert kind in build.KIND_LABELS, f"{kind} has no reader-facing label"
        assert kind in known, f"{kind} would render under no section"
        assert kind in build.GONE_WORDING, f"{kind} has no wording for going missing"


def test_a_harvested_state_shows_when_it_stood_without_claiming_we_saw_it_then():
    src = dict(SRC, restricted=None)
    m = dict(mk_manifest(), target_kind="aial-eval-history",
             git_commit="f0e434e12a91c0781460a6322c1b0be1236c8728",
             git_commit_date="2026-03-20T18:07:05Z")
    out = build.render_version_page(src, m, "20260829T142050Z", set(), "text",
                                    True, False)
    assert "Upstream commit" in out
    assert "20 Mar 2026" in out, "the date the grade actually stood is not shown"
    assert "f0e434e12a91" in out
    assert "this archive fetched it at the time above, not then" in out
    # and a capture with no upstream provenance must not grow an empty row
    plain = build.render_version_page(src, mk_manifest(), "20260829T142050Z", set(),
                                      "text", True, False)
    assert "Upstream commit" not in plain


def test_the_tracker_page_that_publishes_every_grade_is_not_served_either():
    # AIAL's tracker root carries a full Model/Provider/Transparency/Usefulness
    # table: serving this project's copy would republish exactly what the
    # evaluation pages withhold
    url = "https://aial.ie/research/gpai-training-transparency/"
    m = dict(mk_manifest(), target_kind="watch-page",
             http=dict(mk_manifest()["http"], url=url, final_url=url))
    assert build.restriction_of({}, m)
    out = build.render_version_page({"provider": "AIAL", "model": "tracker"}, m,
                                    "20260829T000000Z", set(), "grades table",
                                    True, False)
    assert "<h2>Structured facts</h2>" in out
    assert "grades table" not in out, "their grade table was republished"
    # ...and the stated reason must be the true one
    assert "At the provider's request" not in out
    assert "does not redistribute it" in out


def test_aials_mirror_of_a_providers_own_document_is_still_served():
    # aial-archive is the PROVIDER's mandated disclosure, mirrored by AIAL, and is
    # sometimes the only surviving copy: it must not be swept up by the policy
    m = dict(mk_manifest(), target_kind="aial-archive")
    assert build.restriction_of({}, m) is None
    assert "https://aial.ie/research/gpai-training-transparency/archive/x.pdf"         not in cap.RESTRICTED_URLS


def test_a_row_of_harvested_states_reads_as_history_not_as_one_days_captures():
    # all 318 states were fetched on one day; without the upstream date a reader
    # sees five identical-looking captures and cannot order the grades
    r = mk_row(kind="aial-eval-history", stored_as="raw.yaml",
               ts="20260829T142050Z", upstream_date="2026-03-20T18:07:05Z")
    html = build.version_row_html(r, set(), _first(r))
    assert "the state that stood upstream from 20 Mar 2026" in html
    assert "fetched here on 29 Aug 2026" in html
    # an ordinary capture gains no such note
    assert "stood upstream" not in build.version_row_html(mk_row(), set(),
                                                          _first(mk_row()))


def test_a_harvested_capture_is_never_presented_as_a_superseded_target(corpus,
                                                                       tmp_path,
                                                                       monkeypatch):
    # a harvested state has no registry target by design (its address is a pinned
    # commit). Falling into "Captures of superseded target URLs" would tell a
    # reader the ledger once tracked that address and stopped — untrue of all 318
    corpus.add_capture(ts=V1, raw=b"%PDF-1.4 doc", text="doc text", tslug=SLUG)
    corpus.add_capture(ts="20260829T142050Z",
                       raw=("model_name: x" + NL + "score: 9" + NL).encode(),
                       ext=".yaml", text="model_name: x", kind="aial-eval-history",
                       url="https://raw.githubusercontent.com/o/r/abc123/evals/x.yaml",
                       tslug="aial-eval-history-deadbeef",
                       extra_manifest={"harvested_from": "upstream git history",
                                       "git_commit": "abc123",
                                       "git_commit_date": "2026-03-20T18:07:05Z"})
    corpus.finish()
    dist = _build_site(tmp_path, monkeypatch, corpus.root)
    html = (dist / "ledger" / "prov" / "model" / "index.html").read_text(encoding="utf-8")
    i = html.find("Captures of superseded target URLs")
    seg = html[i:] if i >= 0 else ""
    assert "AIAL evaluation" not in seg, "a harvested state was called superseded"
    assert "Third-party evaluation" in html
    assert "stood upstream from 20 Mar 2026" in html

