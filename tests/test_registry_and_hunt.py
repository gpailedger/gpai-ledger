import json
import types

import pytest

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


# --- site_hunt.probe_redirect: hops are followed only while they stay on-site ---

class _Head:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, url, **kw):
        self.calls.append(url)
        status, loc = self.responses[len(self.calls) - 1]
        return types.SimpleNamespace(status_code=status, url=url,
                                     headers={"Location": loc} if loc else {})


def test_probe_redirect_follows_same_site_hops_only(monkeypatch):
    head = _Head([(301, "https://docs.example.com/summary.pdf"), (200, None)])
    monkeypatch.setattr(site_hunt.requests, "head", head)
    assert site_hunt.probe_redirect("https://example.com/old.pdf", "example.com") \
        == "https://docs.example.com/summary.pdf"
    assert head.calls == ["https://example.com/old.pdf", "https://docs.example.com/summary.pdf"]


def test_probe_redirect_never_requests_an_off_site_location(monkeypatch):
    head = _Head([(302, "https://cdn.other.example/summary.pdf"), (200, None)])
    monkeypatch.setattr(site_hunt.requests, "head", head)
    assert site_hunt.probe_redirect("https://example.com/old.pdf", "example.com") is None
    assert head.calls == ["https://example.com/old.pdf"]


# --- build_registry.main: the refresh fails closed instead of dropping sources ---

def _fake_aial(tmp_path, names):
    repo = tmp_path / "aial"
    (repo / "evals").mkdir(parents=True, exist_ok=True)
    for n in names:
        (repo / "evals" / f"{n}.yaml").write_text(
            f"model_name: {n}\norganization: Testorg\n"
            f"public_summary_link: https://example.org/{n}.pdf\n"
            f"archive_file_name: {n}.pdf\n", encoding="utf-8")
    return repo


def test_build_registry_refuses_to_drop_a_tracked_source(tmp_path):
    out = tmp_path / "sources.json"
    br.main(str(_fake_aial(tmp_path, ["alpha", "beta"])), out_path=out)
    before = out.read_bytes()
    assert {"testorg/alpha", "testorg/beta"} <= {s["id"] for s in json.loads(before)["sources"]}
    (tmp_path / "aial" / "evals" / "beta.yaml").unlink()       # upstream rename/removal
    with pytest.raises(SystemExit, match="testorg/beta"):
        br.main(str(tmp_path / "aial"), out_path=out)
    assert out.read_bytes() == before                           # committed registry untouched
    assert not out.with_name(out.name + ".tmp").exists()


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


def test_unconfirmed_absence_does_not_feed_streak(tmp_path):
    # a 404 from one vantage point that the independent witness did not
    # corroborate must never trigger a relocation hunt
    p = _events(tmp_path, [
        {"source": "s", "target": "t", "outcome": "error", "url": "u", "kind": "provider-live",
         "ts": "1", "absence": "unconfirmed"},
        {"source": "s", "target": "t", "outcome": "error", "url": "u", "kind": "provider-live",
         "ts": "2", "absence": "unconfirmed"},
    ])
    assert ("s", "t") not in site_hunt.error_streaks(p)


def test_confirmed_absence_still_feeds_streak(tmp_path):
    p = _events(tmp_path, [
        {"source": "s", "target": "t", "outcome": "error", "url": "u", "kind": "provider-live",
         "ts": "1", "absence": "confirmed"},
        {"source": "s", "target": "t", "outcome": "error", "url": "u", "kind": "provider-live",
         "ts": "2", "absence": "confirmed"},
    ])
    assert site_hunt.error_streaks(p)[("s", "t")]["streak"] == 2


def test_recheck_recovered_breaks_streak(tmp_path):
    # a sibling's live fetch in the same run supersedes an earlier claim
    p = _events(tmp_path, [
        {"source": "s", "target": "t", "outcome": "error", "url": "u", "kind": "provider-live",
         "ts": "1", "absence": "confirmed"},
        {"source": "s", "target": "t", "outcome": "error", "url": "u", "kind": "provider-live",
         "ts": "2", "absence": "confirmed"},
        {"source": "s", "target": "t", "outcome": "recheck-recovered", "kind": "provider-live", "ts": "3"},
    ])
    assert ("s", "t") not in site_hunt.error_streaks(p)


def test_contradicted_absence_does_not_feed_streak(tmp_path):
    p = _events(tmp_path, [
        {"source": "s", "target": "t", "outcome": "error", "url": "u", "kind": "provider-live",
         "ts": "1", "absence": "contradicted"},
        {"source": "s", "target": "t", "outcome": "error", "url": "u", "kind": "provider-live",
         "ts": "2", "absence": "contradicted"},
    ])
    assert ("s", "t") not in site_hunt.error_streaks(p)
