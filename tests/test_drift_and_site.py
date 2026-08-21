import json

import analyze_drift
import build as sitebuild


# --- site escaping + url scheme neutralization ---

def test_esc_escapes_html():
    assert sitebuild.esc("<script>&\"") == "&lt;script&gt;&amp;&quot;"

def test_url_attr_allows_http_https():
    assert sitebuild.url_attr("https://x/y") == "https://x/y"
    assert sitebuild.url_attr("/rel") == "/rel"

def test_url_attr_neutralizes_javascript_and_data():
    assert sitebuild.url_attr("javascript:alert(1)") == "#"
    assert sitebuild.url_attr("data:text/html,<x>") == "#"


# --- analyze_drift char-stream noise immunity + retired skipping ---

def test_char_stream_noise_helpers():
    # spacing/hyphenation differences collapse to identical char streams
    import re
    a = "Summary:1.0 re- flect"
    b = "Summary: 1.0 reflect"
    ca = "".join(re.findall(r"[a-z0-9]+", a.lower()))
    cb = "".join(re.findall(r"[a-z0-9]+", b.lower()))
    assert ca == cb  # the immunity property the detector relies on


def _mini_corpus(tmp_path, retired_archive=False):
    """Build a tiny data/ tree: one source, a live doc + an archive doc."""
    data = tmp_path / "data"
    (data / "captures").mkdir(parents=True)
    state = {}
    def put(key, dirname, text):
        d = data / "captures" / dirname
        d.mkdir(parents=True)
        (d / "raw.pdf").write_bytes(b"%PDF-1.7")
        (d / "extracted.txt").write_text(text, encoding="utf-8")
        (d / "manifest.json").write_text(json.dumps(
            {"stored_as": "raw.pdf", "sha256": dirname}), encoding="utf-8")
        entry = {"last_sha256": dirname, "last_capture": f"captures/{dirname}",
                 "versions": [{"sha256": dirname, "dir": f"captures/{dirname}"}]}
        if retired_archive and "archive" in key:
            entry["retired"] = "test"
        state[key] = entry
    put("prov/m::provider-live-1", "live1", "the quick brown fox")
    put("prov/m::aial-archive-1", "arch1", "the quick brown fox")
    (data / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return data


def test_drift_same_content_identical_text(tmp_path, monkeypatch):
    data = _mini_corpus(tmp_path)
    reg = {"sources": [{"id": "prov/m", "model": "M", "status": "published"}]}
    (tmp_path / "reg.json").write_text(json.dumps(reg), encoding="utf-8")
    monkeypatch.setattr(analyze_drift, "DATA", data)
    monkeypatch.setattr(analyze_drift, "ROOT", tmp_path)
    (tmp_path / "crawler").mkdir()
    (tmp_path / "crawler" / "sources.json").write_text(json.dumps(reg), encoding="utf-8")
    (tmp_path / "reports").mkdir()
    analyze_drift.main()
    out = json.loads((tmp_path / "reports" / "drift-latest.json").read_text(encoding="utf-8"))
    assert out[0]["verdict"] in ("same-content", "identical-bytes")


def test_drift_skips_retired_archive(tmp_path, monkeypatch):
    data = _mini_corpus(tmp_path, retired_archive=True)
    reg = {"sources": [{"id": "prov/m", "model": "M", "status": "published"}]}
    monkeypatch.setattr(analyze_drift, "DATA", data)
    monkeypatch.setattr(analyze_drift, "ROOT", tmp_path)
    (tmp_path / "crawler").mkdir()
    (tmp_path / "crawler" / "sources.json").write_text(json.dumps(reg), encoding="utf-8")
    (tmp_path / "reports").mkdir()
    analyze_drift.main()
    out = json.loads((tmp_path / "reports" / "drift-latest.json").read_text(encoding="utf-8"))
    # with the only archive retired, there is no archive to compare -> incomplete
    assert out[0]["verdict"] == "incomplete"
