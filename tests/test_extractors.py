"""meta_hub.py hub parsing/accounting + derived_targets.py mined-URL capture
and miner regexes (fully offline)."""
import io
import json
import zipfile
from pathlib import Path

import pytest

import capture as cap
from conftest import load_module, sha

ROOT = Path(__file__).resolve().parent.parent
mh = load_module(str(ROOT / "crawler" / "meta_hub.py"), "meta_hub_mod")
dt = load_module(str(ROOT / "crawler" / "derived_targets.py"), "derived_targets_mod")

SPARK_SIGNED = "https://scontent.xx.fbcdn.net/v/muse-spark.pdf?sig=AAA"
# the stable identity is the hub EDITION label: Meta content-addresses every
# upload, so both the fbcdn hostname (rotating shard) and the path (new per
# re-issue) change while the document stays the same logical edition
SPARK_STABLE = "meta-hub:2026 - Muse Spark"
SPARK_PATH = "/v/muse-spark.pdf"
GLIMMER_SIGNED = "https://scontent.xx.fbcdn.net/v/muse-glimmer.pdf?sig=BBB"
GLIMMER_STABLE = "meta-hub:2026 - Muse Glimmer"

# edition objects exactly as they sit in the hub's JSON payload: \/ escapes in
# URLs, key order varying per object, dash as the literal – escape sequence
# in one and as the real en-dash character in the other
ED_SPARK = (r'{"cdn_url":"https:\/\/scontent.xx.fbcdn.net\/v\/muse-spark.pdf?sig=AAA",'
            r'"time_period":"2026 – Muse Spark"}')
ED_GLIMMER = ('{"time_period":"2026 – Muse Glimmer",'
              r'"cdn_url":"https:\/\/scontent.xx.fbcdn.net\/v\/muse-glimmer.pdf?sig=BBB"}')


def _hub_dom(*edition_objs, include_series=True):
    # neighbouring series before AND after ours, each with its own editions,
    # so segment scoping is exercised by every parse
    parts = ['<script>{"static_report_series_title":"Other Regulatory Reports","editions":['
             r'{"cdn_url":"https:\/\/cdn.example\/other.pdf","time_period":"2025"}]},']
    if include_series:
        parts.append('{"static_report_series_title":"EU AI Act Transparency Reports",'
                     '"editions":[' + ",".join(edition_objs) + "]},")
    parts.append('{"static_report_series_title":"Community Reports","editions":['
                 r'{"cdn_url":"https:\/\/cdn.example\/after.pdf","time_period":"2027"}]}</script>')
    return "".join(parts).encode("utf-8")


def _meta(url, ctype):
    return {"url": url, "final_url": url, "status_code": 200, "content_type": ctype,
            "etag": None, "last_modified": None, "content_length": None,
            "fetched_at": "2026-08-20T00:00:00Z"}


def _mock_provenance(monkeypatch):
    monkeypatch.setattr(cap, "wayback_save",
                        lambda url, **k: {"ok": True, "snapshot": None, "at": "t"})
    monkeypatch.setattr(cap, "ots_stamp",
                        lambda d: (b"OTS", {"ok": True, "calendars": ["c"], "at": "t"}))


def _events(data_root):
    return [json.loads(line) for line in
            (Path(data_root) / "events.jsonl").read_text(encoding="utf-8").splitlines()]


# --- 0. hostile-input bounds: parser deadline, zip caps in the scope filter ---

import time as _time


def _stall(data):
    _time.sleep(30)
    return "never"


def test_pdf_text_bounded_kills_a_stalled_parser_and_the_bytes_survive():
    t0 = _time.time()
    with pytest.raises(RuntimeError, match="exceeded"):
        cap._pdf_text_bounded(b"%PDF-1.4", timeout=2, fn=_stall)
    assert _time.time() - t0 < 20
    # the normal failure contract still holds through the worker: damaged
    # bytes -> no text, a note, no exception
    text, notes = cap.extract_text(b"%PDF-1.4 not really a pdf", ".pdf")
    assert text is None and notes and "extraction failed" in notes[-1]


def _bomb_zip(member_size):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Training-Data-Summary.pdf", b"\0" * member_size)
    return buf.getvalue()


def test_filter_zip_art53_rejects_bundles_over_the_zip_caps(monkeypatch):
    monkeypatch.setattr(cap, "MAX_ZIP_MEMBER_BYTES", 1024)
    monkeypatch.setattr(cap, "MAX_ZIP_TOTAL_BYTES", 1024)
    with pytest.raises(RuntimeError, match="cap"):
        cap.filter_zip_art53(_bomb_zip(4096))


def test_store_document_rejected_bundle_is_an_error_event_not_a_version(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "MAX_ZIP_MEMBER_BYTES", 1024)
    monkeypatch.setattr(cap, "MAX_ZIP_TOTAL_BYTES", 1024)
    store = cap.Store(tmp_path / "data")
    bomb = _bomb_zip(4096)
    monkeypatch.setattr(cap, "fetch", lambda url, **k: (bomb, _meta(url, "application/zip")))
    assert dt.store_document(store, "prov/bundle", "Prov", "Bundle",
                             "https://ex.org/bundle.zip?r=1", "https://ex.org/bundle.zip",
                             "note") == "error"
    (e,) = _events(tmp_path / "data")
    assert e["outcome"] == "error" and "bundle rejected" in e["error"]
    assert not list((tmp_path / "data" / "captures").rglob("manifest.json"))


def test_cohere_miner_ignores_look_alike_hosts(monkeypatch):
    evil = ('<a href="https://fdr-prod-docs-files-public.s3.attacker.example/'
            'eu-ai-public-summary.pdf?x=1">x</a>')
    monkeypatch.setattr(cap, "fetch_rendered", lambda url, **k: (evil.encode(), {}))
    calls = []
    monkeypatch.setattr(dt, "store_document", lambda *a, **k: calls.append(a) or "new")
    assert dt.cohere(None) is False and calls == []


def _manifests(data_root):
    return sorted(Path(data_root).rglob("manifest.json"))


# --- A. meta_hub.hub_editions parsing ---

def test_hub_editions_unescapes_urls_and_parses_any_key_order(monkeypatch):
    monkeypatch.setattr(cap, "fetch_rendered",
                        lambda url, **k: (_hub_dom(ED_SPARK, ED_GLIMMER), {}))
    assert [e["url"] for e in mh.hub_editions()] == [SPARK_SIGNED, GLIMMER_SIGNED]

def test_hub_editions_normalizes_unicode_dashes_in_periods(monkeypatch):
    monkeypatch.setattr(cap, "fetch_rendered",
                        lambda url, **k: (_hub_dom(ED_SPARK, ED_GLIMMER), {}))
    assert [e["period"] for e in mh.hub_editions()] == \
        ["2026 - Muse Spark", "2026 - Muse Glimmer"]

def test_hub_editions_exits_when_series_absent(monkeypatch):
    monkeypatch.setattr(cap, "fetch_rendered",
                        lambda url, **k: (_hub_dom(include_series=False), {}))
    with pytest.raises(SystemExit, match="not found"):
        mh.hub_editions()

def test_hub_editions_exits_when_series_has_zero_editions(monkeypatch):
    monkeypatch.setattr(cap, "fetch_rendered", lambda url, **k: (_hub_dom(), {}))
    with pytest.raises(SystemExit, match="zero editions"):
        mh.hub_editions()


# --- B. meta_hub.main error accounting ---

def test_meta_main_counts_error_and_still_stores_good_edition(tmp_path, monkeypatch):
    _mock_provenance(monkeypatch)
    monkeypatch.setattr(mh, "DATA", tmp_path / "data")
    monkeypatch.setattr(cap, "fetch_rendered",
                        lambda url, **k: (_hub_dom(ED_GLIMMER, ED_SPARK), {}))
    def fake_fetch(url, **k):
        if "glimmer" in url:
            raise RuntimeError("boom")
        return b"%PDF-1.4 spark", _meta(url, "application/pdf")
    monkeypatch.setattr(cap, "fetch", fake_fetch)
    assert mh.main() == 1
    events = _events(tmp_path / "data")
    # exactly ["error", "new"]: no registry-gap fired (both ids are registered)
    assert [e["outcome"] for e in events] == ["error", "new"]
    assert "boom" in events[0]["error"] and events[0]["via"] == "meta-hub"
    # events key on the unsigned CDN path, not the rotating signed URL
    assert events[0]["url"] == GLIMMER_STABLE
    assert events[0]["target"] == cap.target_slug("provider-live", GLIMMER_STABLE)
    mans = _manifests(tmp_path / "data" / "captures" / "meta__muse-spark")
    assert len(mans) == 1
    m = json.loads(mans[0].read_text(encoding="utf-8"))
    assert m["sha256"] == sha(b"%PDF-1.4 spark")
    assert not (tmp_path / "data" / "captures" / "meta__muse-glimmer").exists()


# --- C. meta_hub slug/model derivation ---

def test_meta_main_derives_source_id_and_model_from_period(tmp_path, monkeypatch):
    _mock_provenance(monkeypatch)
    monkeypatch.setattr(mh, "DATA", tmp_path / "data")
    monkeypatch.setattr(cap, "fetch_rendered", lambda url, **k: (_hub_dom(ED_SPARK), {}))
    monkeypatch.setattr(cap, "fetch",
                        lambda url, **k: (b"%PDF-1.4 spark", _meta(url, "application/pdf")))
    assert mh.main() == 0
    mans = _manifests(tmp_path / "data" / "captures" / "meta__muse-spark")
    assert len(mans) == 1
    m = json.loads(mans[0].read_text(encoding="utf-8"))
    assert m["source_id"] == "meta/muse-spark"
    assert m["provider"] == "Meta" and m["model"] == "Muse Spark"
    assert {"stable_base": SPARK_PATH, "edition_key": SPARK_STABLE,
            "hub_period": "2026 - Muse Spark"} in m["extraction_notes"]


# --- D. derived_targets.store_document statuses ---

D_SIGNED = "https://ex.org/files/summary.pdf?sig=rotating"
D_STABLE = "https://ex.org/files/summary.pdf"


def _store_doc(store):
    return dt.store_document(store, "prov/model", "Prov", "Model",
                             D_SIGNED, D_STABLE, "mined note")


ART53_MEMBER = "Model Training Data Summary.pdf"
SOC2_BYTES = b"%PDF-1.4 soc2"


def _bundle(reverse=False):
    members = [(ART53_MEMBER, b"%PDF-1.4 art53"), ("SOC 2 Report.pdf", SOC2_BYTES)]
    if reverse:
        members.reverse()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()

def test_store_document_fetch_error_logs_event(tmp_path, monkeypatch):
    def _raise(url, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(cap, "fetch", _raise)
    store = cap.Store(tmp_path / "data")
    assert _store_doc(store) == "error"
    (e,) = _events(store.root)
    assert e["outcome"] == "error" and "boom" in e["error"]
    assert e["via"] == "derived-targets" and e["url"] == D_STABLE
    assert _manifests(store.root) == []

def test_store_document_pdf_unchanged_bytes_dedupe(tmp_path, monkeypatch):
    _mock_provenance(monkeypatch)
    monkeypatch.setattr(cap, "fetch",
                        lambda url, **k: (b"%PDF-1.4 body", _meta(url, "application/pdf")))
    store = cap.Store(tmp_path / "data")
    assert _store_doc(store) == "new"
    assert _store_doc(store) == "unchanged"
    assert [e["outcome"] for e in _events(store.root)] == ["new", "unchanged"]
    assert len(_manifests(store.root)) == 1

def test_store_document_new_pdf_manifest_and_event(tmp_path, monkeypatch):
    waycalls = []
    monkeypatch.setattr(cap, "wayback_save",
                        lambda url, **k: waycalls.append(url) or {"ok": True, "at": "t"})
    monkeypatch.setattr(cap, "ots_stamp",
                        lambda d: (b"OTS", {"ok": True, "calendars": ["c"], "at": "t"}))
    monkeypatch.setattr(cap, "fetch",
                        lambda url, **k: (b"%PDF-1.4 body", _meta(url, "application/pdf")))
    store = cap.Store(tmp_path / "data")
    assert _store_doc(store) == "new"
    (mp,) = _manifests(store.root)
    m = json.loads(mp.read_text(encoding="utf-8"))
    assert "mined note" in m["extraction_notes"]
    assert {"stable_key": D_STABLE} in m["extraction_notes"]
    (e,) = _events(store.root)
    assert e["outcome"] == "new" and e["via"] == "derived-targets"
    assert e["url"] == D_STABLE
    assert e["target"] == cap.target_slug("provider-live", D_STABLE)
    # wayback targets the fetchable signed URL, not the (unfetchable) stable key
    assert waycalls == [D_SIGNED]

def test_store_document_zip_stores_only_art53_members(tmp_path, monkeypatch):
    _mock_provenance(monkeypatch)
    monkeypatch.setattr(cap, "fetch",
                        lambda url, **k: (_bundle(), _meta(url, "application/zip")))
    store = cap.Store(tmp_path / "data")
    assert _store_doc(store) == "new"
    (raw_zip,) = sorted(store.root.rglob("raw.zip"))
    with zipfile.ZipFile(raw_zip) as zf:
        assert zf.namelist() == [ART53_MEMBER]
    (mp,) = _manifests(store.root)
    m = json.loads(mp.read_text(encoding="utf-8"))
    excl = [n for n in m["extraction_notes"]
            if isinstance(n, dict) and "members_not_stored" in n]
    assert excl == [{"members_not_stored": [
        {"inner_file": "SOC 2 Report.pdf", "inner_sha256": sha(SOC2_BYTES)}]}]
    assert m["text_sha256"] == cap.zip_content_key(m["extraction_notes"])

def test_store_document_rezipped_bundle_dedupes_on_content_key(tmp_path, monkeypatch):
    _mock_provenance(monkeypatch)
    zips = [_bundle(), _bundle(reverse=True)]
    assert zips[0] != zips[1]  # same members, different archive bytes
    monkeypatch.setattr(cap, "fetch",
                        lambda url, **k: (zips.pop(0), _meta(url, "application/zip")))
    store = cap.Store(tmp_path / "data")
    assert _store_doc(store) == "new"
    assert _store_doc(store) == "unchanged"
    assert [e["outcome"] for e in _events(store.root)] == ["new", "unchanged-content"]
    assert len(_manifests(store.root)) == 1

def test_store_document_html_byte_churn_dedupes_on_text(tmp_path, monkeypatch):
    _mock_provenance(monkeypatch)
    bodies = [b"<html><body><p>Doc text here</p></body></html>",
              b"<html>\n<body>\n  <p>Doc   text here</p>\n</body>\n</html>"]
    monkeypatch.setattr(cap, "fetch",
                        lambda url, **k: (bodies.pop(0), _meta(url, "text/html")))
    store = cap.Store(tmp_path / "data")
    assert _store_doc(store) == "new"
    assert _store_doc(store) == "unchanged"
    assert [e["outcome"] for e in _events(store.root)] == ["new", "unchanged-content"]
    assert len(_manifests(store.root)) == 1


# --- E. miner regexes + status-to-ok mapping ---

COHERE_BASE = ("https://fdr-prod-docs-files-public.s3.eu-west-1.amazonaws.com/"
               "cohere/eu-ai-public-summary-command-a-plus.pdf")
COHERE_HTML = (f'<a href="{COHERE_BASE}?X-Amz-Algorithm=AWS4-HMAC-SHA256'
               '&amp;X-Amz-Expires=604800&amp;X-Amz-Signature=abc123">summary</a>')
ANTHROPIC_HTML = '<a href="/doc/trust-zip?r=Tok3n99">Bulk download</a>'


def _recorder(calls, status="new"):
    def rec(store, source_id, provider, model, fetch_url, stable_key, note):
        calls.append((source_id, fetch_url, stable_key))
        return status
    return rec

def test_cohere_mines_signed_url_and_unescapes_entities(monkeypatch):
    monkeypatch.setattr(dt, "render_watch_page", lambda url: COHERE_HTML)
    calls = []
    monkeypatch.setattr(dt, "store_document", _recorder(calls))
    assert dt.cohere(object()) is True
    assert calls == [(
        "cohere/command-a-plus",
        COHERE_BASE + "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
                      "&X-Amz-Expires=604800&X-Amz-Signature=abc123",
        COHERE_BASE)]

def test_anthropic_mines_trust_zip_token(monkeypatch):
    monkeypatch.setattr(dt, "render_watch_page", lambda url: ANTHROPIC_HTML)
    calls = []
    monkeypatch.setattr(dt, "store_document", _recorder(calls))
    assert dt.anthropic_bundle(object()) is True
    assert calls == [(
        "anthropic/trust-center-bundle",
        "https://trust.anthropic.com/doc/trust-zip?r=Tok3n99",
        "https://trust.anthropic.com/doc/trust-zip")]

@pytest.mark.parametrize("status,expected", [("error", False), ("unchanged", True)])
def test_cohere_ok_reflects_store_status(monkeypatch, status, expected):
    monkeypatch.setattr(dt, "render_watch_page", lambda url: COHERE_HTML)
    monkeypatch.setattr(dt, "store_document", _recorder([], status))
    assert dt.cohere(object()) is expected

@pytest.mark.parametrize("status,expected", [("error", False), ("unchanged", True)])
def test_anthropic_ok_reflects_store_status(monkeypatch, status, expected):
    monkeypatch.setattr(dt, "render_watch_page", lambda url: ANTHROPIC_HTML)
    monkeypatch.setattr(dt, "store_document", _recorder([], status))
    assert dt.anthropic_bundle(object()) is expected


def _render_stall(url, timeout_ms, max_expand_clicks):
    _time.sleep(30)
    return b"never", {}


def test_fetch_rendered_is_killed_at_its_deadline():
    t0 = _time.time()
    with pytest.raises(RuntimeError, match="exceeded"):
        cap.fetch_rendered("https://example.org/", deadline=2, fn=_render_stall)
    assert _time.time() - t0 < 20


def test_permanent_fetch_error_survives_the_worker_boundary():
    import pickle
    exc = cap.PermanentFetchError("HTTP 404 for u", status_code=404, headers={"Server": "x"})
    back = pickle.loads(pickle.dumps(exc))
    assert (str(back), back.status_code, back.headers) == ("HTTP 404 for u", 404, {"Server": "x"})


def test_cohere_miner_regex_is_linear_on_hyphen_rich_hosts():
    import derived_targets as dt
    t0 = _time.time()
    assert dt.COHERE_SUMMARY_RE.search("https://fdr-prod-docs-files-public.s3" + "-a" * 60 + "x") is None
    assert _time.time() - t0 < 1
    real = ("https://fdr-prod-docs-files-public.s3.us-east-1.amazonaws.com/cohere.docs/"
            "assets/documents/eu-ai-public-summary_command-a-plus_2607031.pdf?X-Amz-Signature=1")
    assert dt.COHERE_SUMMARY_RE.search(real).group(0).startswith("https://fdr-prod-docs-files-public.s3.us-east-1")


def test_yaml_is_read_as_text_but_is_not_a_document_format():
    # AIAL's scored evaluations are YAML; we need their text (a grade change must
    # be visible) but they must never be classed as the provider's document
    assert cap.guess_ext("text/plain; charset=utf-8",
                         "https://raw.githubusercontent.com/o/r/main/evals/a.yaml",
                         b"model_name: x") == ".yaml"
    text, _notes = cap.extract_text(b'model_name: "X"\nS1:\n  D1:\n    score: 9\n', ".yaml")
    assert "score: 9" in text
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "site"))
    from conftest import load_module
    build = load_module(str(_P(__file__).resolve().parent.parent / "site" / "build.py"),
                        "build_for_yaml_check")
    assert ".yaml" not in build.DOC_SUFFIXES


# --- the Commission's template is a .docx, and it is the form providers fill in -

def _docx(paragraphs, extra_names=()):
    """A minimal but real OOXML .docx carrying the given paragraphs."""
    import io
    import zipfile
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    ps = "".join(
        "<w:p>" + "".join(f"<w:r><w:t>{run}</w:t></w:r>" for run in runs) + "</w:p>"
        for runs in paragraphs)
    doc = (f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>'
           f"{ps}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", doc)
        for n in extra_names:
            zf.writestr(n, "x")
    return buf.getvalue()


def test_a_docx_is_read_paragraph_by_paragraph():
    raw = _docx([["Template for the Public Summary"],
                 ["1.1. Provider identification"],
                 ["Replace this with your response"]])
    text, notes = cap.extract_text(raw, ".docx")
    assert "Template for the Public Summary" in text
    assert "1.1. Provider identification" in text
    assert text.splitlines()[0] == "Template for the Public Summary"
    assert not [n for n in notes if isinstance(n, str) and "no extractor" in n]


def test_a_word_split_across_runs_is_not_split_in_the_text():
    # Word breaks a run wherever formatting changes, often mid-word; joining runs
    # with a separator would invent spaces that are not in the document
    raw = _docx([["Provi", "der", " identification"]])
    text, _ = cap.extract_text(raw, ".docx")
    assert "Provider identification" in text


def test_an_empty_paragraph_does_not_become_a_blank_line():
    raw = _docx([["real text"], [], ["  "]])
    text, _ = cap.extract_text(raw, ".docx")
    assert text.splitlines() == ["real text"]


def test_a_docx_without_a_document_part_fails_loudly_not_silently():
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/other.xml", "<a/>")
    text, notes = cap.extract_text(buf.getvalue(), ".docx")
    assert text is None
    assert any("extraction failed" in str(n) for n in notes)


def test_docx_is_a_document_format_on_the_site():
    # unlike an evaluation, the template IS a document the ledger holds
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "site"))
    from conftest import load_module
    build = load_module(str(_P(__file__).resolve().parent.parent / "site" / "build.py"),
                        "build_for_docx_check")
    assert ".docx" in build.DOC_SUFFIXES


def test_an_ooxml_file_is_not_sniffed_as_a_plain_zip():
    # the Commission serves the Article 53(1)(d) template from an extensionless
    # URL with Content-Type "/"; sniffing it as .zip published "no text extracted"
    # for the one document every filing in this archive is measured against
    raw = _docx([["Template for the Public Summary"]])
    assert cap.guess_ext("/", "https://ec.europa.eu/newsroom/dae/document/11857", raw) == ".docx"
    assert cap.guess_ext("application/zip", "https://x/bundle.zip", raw) == ".zip"


def test_a_docx_footnote_is_not_silently_dropped():
    # the template's normative footnote ("the Commission understands the modality
    # of 'audio' to include 'speech'") lives in footnotes.xml, not the body
    import io
    import zipfile
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = (f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>'
            "<w:p><w:r><w:t>Body sentence</w:t></w:r></w:p></w:body></w:document>")
    notes = (f'<?xml version="1.0"?><w:footnotes xmlns:w="{W}">'
             "<w:footnote><w:p><w:r><w:t>NORMATIVE-FOOTNOTE</w:t></w:r></w:p>"
             "</w:footnote></w:footnotes>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", body)
        zf.writestr("word/footnotes.xml", notes)
    text, _ = cap.extract_text(buf.getvalue(), ".docx")
    assert "Body sentence" in text
    assert "NORMATIVE-FOOTNOTE" in text


def test_a_break_separates_words_instead_of_fusing_them():
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    import io
    import zipfile
    doc = (f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body><w:p>'
           "<w:r><w:t>Common Crawl</w:t><w:br/><w:t>Wikipedia</w:t></w:r>"
           "</w:p></w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", doc)
    text, _ = cap.extract_text(buf.getvalue(), ".docx")
    assert "CrawlWikipedia" not in text
    assert "Wikipedia" in text



def test_a_docx_entity_bomb_is_refused_not_expanded():
    # ElementTree refuses EXTERNAL entities but expands INTERNAL ones, and the
    # member cap bounds the bytes read, not what they expand to — a few KB of
    # .docx fetched from a third party could exhaust an unattended runner
    import io
    import zipfile
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
            '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
            f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&lol2;</w:t>'
            "</w:r></w:p></w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", bomb)
    text, notes = cap.extract_text(buf.getvalue(), ".docx")
    assert text is None
    assert any("DTD or entity" in str(n) for n in notes), notes


def test_an_external_entity_cannot_reach_the_filesystem():
    import io
    import zipfile
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xxe = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
           f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&x;</w:t>'
           "</w:r></w:p></w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xxe)
    text, _notes = cap.extract_text(buf.getvalue(), ".docx")
    assert text is None


def test_an_ordinary_docx_is_unaffected_by_the_dtd_guard():
    raw = _docx([["Template for the Public Summary"], ["1.1. Provider identification"]])
    text, notes = cap.extract_text(raw, ".docx")
    assert "1.1. Provider identification" in text
    assert not [n for n in notes if "DTD" in str(n)]
