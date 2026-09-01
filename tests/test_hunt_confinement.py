"""Relocation hunt: confinement to the provider's site, sibling-aware
confirmation, bounded scoring, and the recovered / no-site outcomes."""
import json
import types

import capture as cap
import site_hunt


def _resp(status, location=None, ctype="text/html", body=b"<html></html>", url=""):
    return types.SimpleNamespace(status_code=status, url=url, content=body, text=body.decode(),
                                 headers={k: v for k, v in (("Location", location),
                                                            ("Content-Type", ctype)) if v})


# --- redirects never leave the site ------------------------------------------

def test_fingerprint_check_never_fetches_an_off_site_redirect(monkeypatch):
    heads = []

    def head(url, **kw):
        heads.append(url)
        assert kw.get("allow_redirects") is False
        return _resp(302, location="https://cdn.other.example/doc.pdf")
    monkeypatch.setattr(cap, "guarded_request",
                        lambda method, url, **kw: (head)(url, **kw))
    fetched = []
    monkeypatch.setattr(cap, "fetch", lambda url, **k: fetched.append(url) or (b"x", {}))
    res, sim = site_hunt.fingerprint_check("https://example.com/doc.pdf", "text", "example.com")
    assert res is None and sim == 0.0 and fetched == []
    assert heads == ["https://example.com/doc.pdf"]


def test_fingerprint_check_follows_on_site_hops_then_fetches(monkeypatch):
    monkeypatch.setattr(cap, "guarded_request", cap.guarded_request)
    responses = iter([_resp(301, location="https://docs.example.com/doc.pdf"), _resp(200)])
    monkeypatch.setattr(cap, "guarded_request",
                        lambda method, url, **kw: (lambda url, **kw: next(responses))(url, **kw))
    monkeypatch.setattr(cap, "fetch", lambda url, **k: (
        b"%PDF", {"content_type": "application/pdf", "final_url": url}))
    monkeypatch.setattr(cap, "guess_ext", lambda *a: ".pdf")
    monkeypatch.setattr(cap, "extract_text", lambda raw, ext: ("the summary text", []))
    res, sim = site_hunt.fingerprint_check("https://example.com/doc.pdf", "the summary text",
                                           "example.com")
    assert res is not None and sim == 1.0


def test_fingerprint_check_discards_a_body_whose_final_url_left_the_site(monkeypatch):
    monkeypatch.setattr(cap, "guarded_request",
                        lambda method, url, **kw: (lambda url, **kw: _resp(200))(url, **kw))
    monkeypatch.setattr(cap, "fetch", lambda url, **k: (
        b"x", {"content_type": "text/html", "final_url": "https://evil.example/x"}))
    assert site_hunt.fingerprint_check("https://example.com/a", "t", "example.com") == (None, 0.0)


def test_crawl_domain_queues_on_site_redirects_and_drops_off_site_ones(monkeypatch):
    calls = []

    def get(url, **kw):
        calls.append(url)
        assert kw.get("allow_redirects") is False
        if url == "https://example.com/":
            return _resp(302, location="https://example.com/home")
        if url == "https://example.com/home":
            return _resp(200, body=b"<a href='https://example.com/legal/summary.pdf'>s</a>"
                                   b"<a href='https://example.com/away'>a</a>")
        if url == "https://example.com/away":
            return _resp(302, location="https://other.example/")
        return _resp(404)
    monkeypatch.setattr(cap, "guarded_request",
                        lambda method, url, **kw: (get)(url, **kw))
    pages = list(site_hunt.crawl_domain("example.com", ["https://example.com/"], 10, throttle=0))
    assert [p for p, _ in pages] == ["https://example.com/home"]
    assert pages[0][1] == ["https://example.com/legal/summary.pdf"]
    assert "https://other.example/" not in calls


def test_sitemap_is_read_without_following_redirects(monkeypatch):
    seen = {}

    def get(url, **kw):
        seen[url] = kw.get("allow_redirects")
        return _resp(200, ctype="application/xml",
                     body=b"<loc>https://example.com/legal/summary.pdf</loc>"
                          b"<loc>https://other.example/summary.pdf</loc>")
    monkeypatch.setattr(cap, "guarded_request",
                        lambda method, url, **kw: (get)(url, **kw))
    assert site_hunt.sitemap_urls("example.com") == ["https://example.com/legal/summary.pdf"]
    assert all(v is False for v in seen.values())


# --- which site may be hunted ------------------------------------------------

def _registry(tmp_path, monkeypatch, sources):
    p = tmp_path / "sources.json"
    p.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    monkeypatch.setattr(site_hunt, "REGISTRY", p)


def test_provider_site_never_falls_back_to_the_aial_archive(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, [{"id": "openai/m", "provider": "OpenAI", "targets": [
        {"kind": "provider-live", "url": "https://cdn.openai.com/doc.pdf"},
        {"kind": "aial-archive", "url": "https://aial.ie/archive/doc.pdf"},
        {"kind": "watch-page", "url": "https://aial.ie/tracker"}]}])
    assert site_hunt.provider_site("openai/m", "https://cdn.openai.com/doc.pdf") is None


def test_provider_site_uses_a_provider_owned_page(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, [{"id": "x/m", "provider": "X", "targets": [
        {"kind": "provider-live", "url": "https://drive.google.com/file/d/1/view"},
        {"kind": "provider-page", "url": "https://x.example/docs"}]}])
    assert site_hunt.provider_site("x/m", "https://drive.google.com/file/d/1/view") == "x.example"
    assert site_hunt.provider_site("x/m", "https://x.example/old.pdf") == "x.example"
    assert site_hunt.never_crawl("www.aial.ie") and site_hunt.never_crawl("web.archive.org")


# --- confirmation: only an unambiguous, attributable match is written ----------

SHA = "f" * 64


def test_judge_confirms_byte_identity_whatever_the_similarity():
    assert site_hunt.judge([(0.5, "u", SHA, 0.0)], {SHA}) == (
        True, "byte-identical to an archived version of this target")


def test_judge_refuses_two_strong_candidates():
    ok, why = site_hunt.judge([(0.999, "a", "1" * 64, 0.0), (0.985, "b", "2" * 64, 0.0)], set())
    assert ok is False and "ambiguous" in why


def test_judge_refuses_a_candidate_a_sibling_explains_as_well():
    # sibling summaries of one provider score 0.993-0.998 against each other
    ok, why = site_hunt.judge([(0.9990, "a", "1" * 64, 0.9981)], set())
    assert ok is False and "not attributable" in why


def test_judge_confirms_a_unique_attributable_match():
    ok, why = site_hunt.judge([(0.9995, "a", "1" * 64, 0.9900), (0.90, "b", "2" * 64, 0.0)], set())
    assert ok is True and "best sibling 0.9900" in why


def test_judge_reports_similarity_below_confirmation():
    assert site_hunt.judge([(0.90, "a", "1" * 64, 0.0)], set()) == (False, "similarity 0.9000")


def test_similarity_is_cheap_for_unrelated_texts():
    a = "alpha " * 4000
    b = "omega " * 4000
    assert site_hunt.similarity(a, b) < site_hunt.CANDIDATE_SIM
    assert site_hunt.similarity("the same text", "the same text") == 1.0


# --- main(): recovered location and no-site outcomes, nothing written ---------

def _hunt_env(tmp_path, monkeypatch, dead_url, sources):
    data = tmp_path / "data"
    data.mkdir()
    cap_dir = "captures/prov__m/provider-live-aaaa1111/20260811T100000Z"
    (data / cap_dir).mkdir(parents=True)
    (data / cap_dir / "extracted.txt").write_text("the summary text", encoding="utf-8")
    (data / "state.json").write_text(json.dumps({"prov/m::provider-live-aaaa1111": {
        "versions": [{"sha256": cap.sha256_hex(b"bytes"), "dir": cap_dir}],
        "last_sha256": cap.sha256_hex(b"bytes"), "last_capture": cap_dir}}), encoding="utf-8")
    (data / "events.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"source": "prov/m", "target": "provider-live-aaaa1111", "outcome": "error",
         "absence": "persistent", "url": dead_url, "kind": "provider-live", "ts": "1"},
        {"source": "prov/m", "target": "provider-live-aaaa1111", "outcome": "error",
         "absence": "persistent", "url": dead_url, "kind": "provider-live", "ts": "2"},
    ]) + "\n", encoding="utf-8")
    (tmp_path / "reports").mkdir()
    monkeypatch.setattr(site_hunt, "DATA", data)
    monkeypatch.setattr(site_hunt, "ROOT", tmp_path)
    monkeypatch.setattr(site_hunt, "RELOCATIONS", tmp_path / "relocations.json")
    _registry(tmp_path, monkeypatch, sources)
    monkeypatch.setattr("sys.argv", ["site_hunt.py"])
    return tmp_path


def test_recovered_location_is_reported_and_nothing_relocated(tmp_path, monkeypatch):
    root = _hunt_env(tmp_path, monkeypatch, "https://prov.example/doc.pdf",
                     [{"id": "prov/m", "provider": "Prov", "targets": [
                         {"kind": "provider-live", "url": "https://prov.example/doc.pdf"}]}])
    monkeypatch.setattr(cap, "fetch", lambda url, **k: (b"bytes", {"status_code": 200}))
    assert site_hunt.main() == 0
    report = (root / "reports" / "hunt-latest.md").read_text(encoding="utf-8")
    assert "recovered-at-recorded-location" in report and "identical to an archived version" in report
    assert json.loads((root / "relocations.json").read_text(encoding="utf-8")) == {}


def test_third_party_only_source_is_not_hunted(tmp_path, monkeypatch):
    root = _hunt_env(tmp_path, monkeypatch, "https://cdn.openai.com/doc.pdf",
                     [{"id": "prov/m", "provider": "Prov", "targets": [
                         {"kind": "provider-live", "url": "https://cdn.openai.com/doc.pdf"},
                         {"kind": "aial-archive", "url": "https://aial.ie/a.pdf"}]}])

    def dead(url, **k):
        raise cap.PermanentFetchError("HTTP 404", status_code=404)
    monkeypatch.setattr(cap, "fetch", dead)
    called = []
    monkeypatch.setattr(site_hunt, "crawl_domain", lambda *a, **k: called.append(a) or iter(()))
    assert site_hunt.main() == 0
    report = (root / "reports" / "hunt-latest.md").read_text(encoding="utf-8")
    assert "no provider site to hunt" in report and called == []


def test_the_hunt_cannot_reach_a_private_address(monkeypatch):
    # discovery URLs come out of third-party DOMs and third-party metadata; the
    # four raw requests here used to bypass the guard fetch() applies
    import pytest as _pytest
    for url in ("http://127.0.0.1/doc.pdf", "http://169.254.169.254/latest/meta-data",
                "file:///etc/passwd"):
        with _pytest.raises(Exception):
            cap.guarded_request("GET", url)


def test_a_guarded_request_bounds_the_body(monkeypatch):
    class _R:
        status_code = 200
        headers = {}
        history = []
        url = "https://example.org/x"

        def iter_content(self, n):
            while True:
                yield b"x" * n

        def close(self):
            pass

    monkeypatch.setattr(cap, "_assert_public_http", lambda u: None)
    monkeypatch.setattr(cap.requests, "request", lambda *a, **k: _R())
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="exceeded"):
        cap.guarded_request("GET", "https://example.org/x", max_bytes=1024)
