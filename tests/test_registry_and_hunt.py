import json

import build_registry as br
import site_hunt


# --- build_registry.normalize_url ---

def test_normalize_url_adds_scheme():
    assert br.normalize_url("example.com/x").startswith("https://")

def test_normalize_url_blob_to_resolve():
    u = br.normalize_url("https://huggingface.co/o/r/blob/main/f.pdf")
    assert "/resolve/" in u and "/blob/" not in u

def test_normalize_url_keeps_https():
    assert br.normalize_url("https://x/y") == "https://x/y"


# --- site_hunt same-site logic (the lstrip('www.') bug regression) ---

def test_same_site_exact_and_subdomain():
    assert site_hunt.same_site("https://example.com/a", "example.com")
    assert site_hunt.same_site("https://docs.example.com/a", "example.com")
    assert site_hunt.same_site("https://example.com/a", "www.example.com")

def test_same_site_rejects_lookalike_domain():
    # the classic lstrip('www.') hole: wwwexample.com must NOT match example.com
    assert not site_hunt.same_site("https://wwwexample.com/evil", "example.com")

def test_same_site_rejects_non_https():
    assert not site_hunt.same_site("http://example.com/a", "example.com")
    assert not site_hunt.same_site("file:///etc/passwd", "example.com")

def test_same_site_rejects_other_domain():
    assert not site_hunt.same_site("https://evil.com/a", "example.com")


# --- site_hunt.error_streaks logic ---

def _events(tmp_path, rows):
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p

def test_error_streak_counts_consecutive(tmp_path):
    p = _events(tmp_path, [
        {"source": "s", "target": "t", "outcome": "error", "url": "u", "kind": "provider-live", "ts": "1"},
        {"source": "s", "target": "t", "outcome": "error", "url": "u", "kind": "provider-live", "ts": "2"},
    ])
    streaks = site_hunt.error_streaks(p)
    assert streaks[("s", "t")]["streak"] == 2

def test_success_breaks_streak(tmp_path):
    p = _events(tmp_path, [
        {"source": "s", "target": "t", "outcome": "error", "url": "u", "kind": "provider-live", "ts": "1"},
        {"source": "s", "target": "t", "outcome": "error", "url": "u", "kind": "provider-live", "ts": "2"},
        {"source": "s", "target": "t", "outcome": "new", "kind": "provider-live", "ts": "3"},
    ])
    assert ("s", "t") not in site_hunt.error_streaks(p)

def test_provider_page_success_does_not_suppress_dead_document(tmp_path):
    # a live-document target dies while a sibling provider-PAGE keeps succeeding:
    # the hunt must still fire (the page is a different document)
    p = _events(tmp_path, [
        {"source": "s", "target": "doc", "outcome": "error", "url": "u", "kind": "provider-live", "ts": "1"},
        {"source": "s", "target": "doc", "outcome": "error", "url": "u", "kind": "provider-live", "ts": "2"},
        {"source": "s", "target": "page", "outcome": "unchanged", "kind": "provider-page", "ts": "3"},
    ])
    assert ("s", "doc") in site_hunt.error_streaks(p)

def test_provider_live_success_suppresses_sibling(tmp_path):
    p = _events(tmp_path, [
        {"source": "s", "target": "doc", "outcome": "error", "url": "u", "kind": "provider-live", "ts": "1"},
        {"source": "s", "target": "doc", "outcome": "error", "url": "u", "kind": "provider-live", "ts": "2"},
        {"source": "s", "target": "doc2", "outcome": "new", "kind": "provider-live", "ts": "3"},
    ])
    assert ("s", "doc") not in site_hunt.error_streaks(p)
