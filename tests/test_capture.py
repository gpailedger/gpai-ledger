import io
import zipfile


import capture as cap


# --- guess_ext magic-byte sniffing ---

def test_guess_ext_by_content_type_case_insensitive():
    assert cap.guess_ext("application/pdf", "x") == ".pdf"
    assert cap.guess_ext("APPLICATION/PDF", "x") == ".pdf"

def test_guess_ext_by_url_suffix():
    assert cap.guess_ext("", "https://a/b.zip") == ".zip"
    assert cap.guess_ext("", "https://a/b.pdf?sig=1") == ".pdf"

def test_guess_ext_sniffs_octet_stream_pdf():
    assert cap.guess_ext("application/octet-stream", "https://a/download", b"%PDF-1.7...") == ".pdf"

def test_guess_ext_sniffs_zip_magic():
    assert cap.guess_ext("application/octet-stream", "https://a/x", b"PK\x03\x04rest") == ".zip"

def test_guess_ext_falls_back_to_bin():
    assert cap.guess_ext("application/weird", "https://a/x", b"\x00\x01") == ".bin"


# --- normalize ---

def test_normalize_crlf_and_blank_runs():
    out = cap.normalize("a\r\n\r\n\r\n\r\nb   \n")
    assert out == "a\n\nb\n"


# --- target_slug stability ---

def test_target_slug_stable_and_kind_prefixed():
    s1 = cap.target_slug("provider-live", "https://x/y")
    s2 = cap.target_slug("provider-live", "https://x/y")
    assert s1 == s2 and s1.startswith("provider-live-")
    assert cap.target_slug("provider-live", "https://x/z") != s1


# --- zip extraction: sorted order, inner notes, bomb guards ---

def _zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()

def _pdf_bytes(text="hello ledger"):
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()

def test_zip_no_pdf_returns_none_with_note():
    data = _zip([("a.txt", b"hi"), ("b.txt", b"yo")])
    text, notes = cap.extract_text(data, ".zip")
    assert text is None
    assert any(isinstance(n, dict) and n.get("inner_file") == "a.txt" for n in notes)

def test_zip_member_order_is_sorted_deterministic():
    d1 = _zip([("z.txt", b"1"), ("a.txt", b"2")])
    d2 = _zip([("a.txt", b"2"), ("z.txt", b"1")])
    _, n1 = cap.extract_text(d1, ".zip")
    _, n2 = cap.extract_text(d2, ".zip")
    names1 = [n["inner_file"] for n in n1 if isinstance(n, dict) and "inner_file" in n]
    names2 = [n["inner_file"] for n in n2 if isinstance(n, dict) and "inner_file" in n]
    assert names1 == names2 == ["a.txt", "z.txt"]

def test_zip_bomb_member_count_rejected():
    data = _zip([(f"f{i}.txt", b"x") for i in range(cap.MAX_ZIP_MEMBERS + 1)])
    text, notes = cap.extract_text(data, ".zip")
    assert text is None
    assert any("members" in str(n) for n in notes)

def test_zip_bomb_expansion_rejected():
    # single member advertising a huge decompressed size via a high-ratio payload
    data = _zip([("big.txt", b"\x00" * (cap.MAX_ZIP_MEMBER_BYTES + 1))])
    text, notes = cap.extract_text(data, ".zip")
    assert text is None
    assert any("cap" in str(n) or "exceeds" in str(n) for n in notes)


# --- html extraction strips scripts ---

def test_html_strips_script_and_style():
    html = b"<html><body>keep<script>evil()</script><style>x{}</style>more</body></html>"
    text, _ = cap.extract_text(html, ".html")
    assert "keep" in text and "more" in text and "evil" not in text


# --- Store round-trip on a tmp corpus ---

def test_store_record_and_dedupe(tmp_path):
    store = cap.Store(tmp_path)
    d = store.capture_dir("prov/model", "provider-live-abc")
    rel = d.relative_to(store.root).as_posix()
    store.record_version("prov/model", "provider-live-abc", "sha1", rel, text_sha="t1")
    assert store.last_sha("prov/model", "provider-live-abc") == "sha1"
    assert store.last_text_sha("prov/model", "provider-live-abc") == "t1"
    # a version with no text must reset the text hash, not inherit t1
    store.record_version("prov/model", "provider-live-abc", "sha2", rel, text_sha=None)
    assert store.last_text_sha("prov/model", "provider-live-abc") is None

def test_capture_dir_never_overwrites(tmp_path, monkeypatch):
    store = cap.Store(tmp_path)
    monkeypatch.setattr(cap, "ts_slug", lambda: "20260101T000000Z")  # force collision
    a = store.capture_dir("p/m", "t")
    b = store.capture_dir("p/m", "t")
    assert a != b and a.exists() and b.exists()

def test_state_written_with_posix_separators(tmp_path):
    store = cap.Store(tmp_path)
    d = store.capture_dir("prov/model", "provider-live-abc")
    store.record_version("prov/model", "provider-live-abc", "sha1",
                         d.relative_to(store.root).as_posix())
    store.save_state()
    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "\\\\" not in raw and chr(92) not in raw


# --- wayback_save snapshot parsing (mocked) ---

class _Resp:
    def __init__(self, status, url, headers=None):
        self.status_code, self.url, self.headers = status, url, (headers or {})
    def close(self): pass

def test_wayback_content_location_branch(monkeypatch):
    monkeypatch.setattr(cap.requests, "get",
                        lambda *a, **k: _Resp(200, "https://web.archive.org/save/x",
                                              {"Content-Location": "/web/2026/x"}))
    out = cap.wayback_save("https://x")
    assert out["ok"] and out["snapshot"] == "https://web.archive.org/web/2026/x"

def test_wayback_redirected_url_branch(monkeypatch):
    monkeypatch.setattr(cap.requests, "get",
                        lambda *a, **k: _Resp(200, "https://web.archive.org/web/2026/x", {}))
    out = cap.wayback_save("https://x")
    assert out["snapshot"] == "https://web.archive.org/web/2026/x"

def test_wayback_no_snapshot_branch(monkeypatch):
    monkeypatch.setattr(cap.requests, "get",
                        lambda *a, **k: _Resp(200, "https://x/web/thing", {}))
    out = cap.wayback_save("https://x/web/thing")
    assert out["snapshot"] is None


# --- consent-strip JS contract (logic assertions on the source, not a browser run) ---

def test_consent_strip_js_requires_dual_signal():
    js = cap.CONSENT_STRIP_JS
    # platform-named containers are removed on a phrase match; generically-named ones
    # additionally require being a floating overlay, so inline prose is never removed
    assert "kill(platform, false)" in js
    assert "kill(generic, true)" in js
    assert "position" in js and "fixed" in js and "sticky" in js

def test_consent_strip_js_never_removes_body_or_root():
    js = cap.CONSENT_STRIP_JS
    assert "n === document.body" in js and "n === document.documentElement" in js


# --- OTS calendar allowlist must accept real attestation hosts (regression: the
# --- allowlist once held only submission-pool aliases, silently no-opping upgrades) ---

def test_ots_allowlist_accepts_real_attestation_calendar_uris():
    import upgrade_ots
    for uri in ("https://alice.btc.calendar.opentimestamps.org",
                "https://bob.btc.calendar.opentimestamps.org",
                "https://finney.calendar.eternitywall.com",
                "https://btc.calendar.catallaxy.com"):
        assert upgrade_ots._allowed_calendar(uri), uri

def test_ots_allowlist_rejects_unknown_hosts():
    import upgrade_ots
    assert not upgrade_ots._allowed_calendar("https://evil.example.com")
    assert not upgrade_ots._allowed_calendar("http://alice.btc.calendar.opentimestamps.org")


def test_filter_zip_art53_keeps_only_summary_members_and_is_deterministic():
    import io, zipfile
    import capture as cap
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Claude 3 Training Data Summary.pdf", b"%PDF-summary")
        z.writestr("SOC 3 Report.pdf", b"%PDF-soc3")
        z.writestr("Architecture Overview.pdf", b"%PDF-arch")
        z.writestr("AB 2013 Training Data Disclosure.pdf", b"%PDF-ab2013")
    raw = buf.getvalue()
    filtered1, excluded1 = cap.filter_zip_art53(raw)
    filtered2, excluded2 = cap.filter_zip_art53(raw)
    # deterministic byte-for-byte (fixed timestamps, sorted names)
    assert filtered1 == filtered2
    with zipfile.ZipFile(io.BytesIO(filtered1)) as z:
        assert z.namelist() == ["Claude 3 Training Data Summary.pdf"]
    names = {e["inner_file"] for e in excluded1}
    # AB 2013 mentions "training data" but is a California statute doc, not Art. 53
    assert names == {"SOC 3 Report.pdf", "Architecture Overview.pdf",
                     "AB 2013 Training Data Disclosure.pdf"}
    # every excluded member is hash-recorded so its identity stays provable
    assert all(len(e["inner_sha256"]) == 64 for e in excluded1)
    assert excluded1 == excluded2
