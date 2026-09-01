import json
import types

import pytest

import build_registry as br
import capture as cap
import site_hunt
from pathlib import Path


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
    monkeypatch.setattr(cap, "guarded_request",
                        lambda method, url, **kw: head(url, **kw))
    assert site_hunt.probe_redirect("https://example.com/old.pdf", "example.com") \
        == "https://docs.example.com/summary.pdf"
    assert head.calls == ["https://example.com/old.pdf", "https://docs.example.com/summary.pdf"]


def test_probe_redirect_never_requests_an_off_site_location(monkeypatch):
    head = _Head([(302, "https://cdn.other.example/summary.pdf"), (200, None)])
    monkeypatch.setattr(cap, "guarded_request",
                        lambda method, url, **kw: head(url, **kw))
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


def test_build_registry_carries_a_retired_source_forward_flagged(tmp_path, monkeypatch):
    out = tmp_path / "sources.json"
    br.main(str(_fake_aial(tmp_path, ["alpha", "beta"])), out_path=out)
    (tmp_path / "aial" / "evals" / "beta.yaml").unlink()
    monkeypatch.setattr(br, "RETIRED_SOURCE_IDS",
                        {"testorg/beta": "retired 2026-08-22: upstream eval removed"})
    br.main(str(tmp_path / "aial"), out_path=out)
    by_id = {s["id"]: s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    assert by_id["testorg/beta"]["retired"] == "retired 2026-08-22: upstream eval removed"
    assert by_id["testorg/beta"]["targets"]                     # last committed targets kept
    assert "retired" not in by_id["testorg/alpha"]


def test_build_registry_merges_a_probed_document_and_publishes_the_source(tmp_path):
    # probe_missing.py fetched a document for a model the registry had as missing;
    # merging it adds a target and flips the status, so the next sweep captures it
    out = tmp_path / "sources.json"
    repo = _fake_aial(tmp_path, ["alpha"])
    (repo / "evals" / "beta.yaml").write_text(
        "model_name: beta\norganization: Testorg\n", encoding="utf-8")   # no summary => missing
    br.main(str(repo), out_path=out)
    by_id = {s["id"]: s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    assert by_id["testorg/beta"]["status"] == "missing"

    disc = Path(br.__file__).parent / "discovered.json"
    disc.write_text(json.dumps({"testorg/beta": [
        {"kind": "provider-live", "url": "https://example.org/beta-found.pdf",
         "note": "found by probe_missing"}]}), encoding="utf-8")
    try:
        br.main(str(repo), out_path=out)
    finally:
        disc.unlink()
    by_id = {s["id"]: s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    beta = by_id["testorg/beta"]
    assert beta["status"] == "published"
    assert "https://example.org/beta-found.pdf" in {t["url"] for t in beta["targets"]}
    assert "probe_missing" in [t.get("note", "") for t in beta["targets"]
                               if t["url"].endswith("beta-found.pdf")][0]


def test_build_registry_ignores_a_probed_document_for_an_unknown_source(tmp_path, capsys):
    out = tmp_path / "sources.json"
    repo = _fake_aial(tmp_path, ["alpha"])
    disc = Path(br.__file__).parent / "discovered.json"
    disc.write_text(json.dumps({"testorg/nope": [
        {"kind": "provider-live", "url": "https://example.org/x.pdf", "note": "n"}]}),
        encoding="utf-8")
    try:
        br.main(str(repo), out_path=out)
    finally:
        disc.unlink()
    assert "unknown source id" in capsys.readouterr().out
    assert all(s["id"] != "testorg/nope"
               for s in json.loads(out.read_text(encoding="utf-8"))["sources"])


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


def test_restricted_flag_applies_to_every_source_and_unknown_ids_fail_closed(tmp_path, monkeypatch):
    repo = _fake_aial(tmp_path, ["alpha-model"])
    out = tmp_path / "sources.json"
    monkeypatch.setattr(br, "RESTRICTED_SOURCES", {"anthropic/trust-center-bundle": "provider objection (test)"})
    br.main(str(repo), out_path=str(out))
    sources = {s["id"]: s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    assert sources["anthropic/trust-center-bundle"]["restricted"] == "provider objection (test)"
    monkeypatch.setattr(br, "RESTRICTED_SOURCES", {"nobody/nothing": "typo"})
    with pytest.raises(SystemExit):
        br.main(str(repo), out_path=str(out))


def test_persistent_absence_feeds_the_hunt_streak(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in [
        {"source": "s", "target": "t", "outcome": "error", "absence": "persistent",
         "url": "u", "kind": "provider-live", "ts": "1"},
        {"source": "s", "target": "t", "outcome": "error", "absence": "persistent",
         "url": "u", "kind": "provider-live", "ts": "2"},
    ]) + "\n", encoding="utf-8")
    assert ("s", "t") in site_hunt.error_streaks(p)


def test_no_archive_override_is_left_pinning_us_to_a_stale_upstream_file():
    # each override corrects an upstream metadata bug and must be dropped once
    # AIAL fixes it, or it silently pins the ledger to an older archived copy
    assert br.ARCHIVE_FILE_OVERRIDES == {}, (
        "an override is in force — confirm upstream is still wrong before keeping it")


def test_every_aial_tracked_model_gets_an_evaluation_target(tmp_path):
    out = tmp_path / "sources.json"
    br.main(str(_fake_aial(tmp_path, ["alpha", "beta"])), out_path=out)
    srcs = json.loads(out.read_text(encoding="utf-8"))["sources"]
    aial = [s for s in srcs if s.get("aial", {}).get("eval_yaml")]
    assert aial
    for s in aial:
        ev = [t for t in s["targets"] if t["kind"] == "aial-eval"]
        assert len(ev) == 1, f"{s['id']} has {len(ev)} evaluation targets"
        assert ev[0]["url"].startswith(br.AIAL_EVAL_RAW)
        assert ev[0]["url"].endswith(".yaml")
        assert "not a legal determination" in ev[0]["note"]


def test_an_evaluation_target_does_not_make_a_missing_model_published(tmp_path):
    out = tmp_path / "sources.json"
    repo = _fake_aial(tmp_path, ["alpha"])
    (repo / "evals" / "beta.yaml").write_text(
        "model_name: beta\norganization: Testorg\n", encoding="utf-8")
    br.main(str(repo), out_path=out)
    by = {s["id"]: s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    assert by["testorg/beta"]["status"] == "missing"
    assert any(t["kind"] == "aial-eval" for t in by["testorg/beta"]["targets"])


def test_every_evaluated_model_also_tracks_the_page_that_shows_its_grade(tmp_path):
    # the YAML carries the scores; only the rendered page carries the letter grade
    out = tmp_path / "sources.json"
    br.main(str(_fake_aial(tmp_path, ["alpha", "beta"])), out_path=out)
    srcs = json.loads(out.read_text(encoding="utf-8"))["sources"]
    for s in srcs:
        ev = [t for t in s["targets"] if t["kind"] == "aial-eval"]
        pg = [t for t in s["targets"] if t["kind"] == "aial-eval-page"]
        assert len(pg) == len(ev), f"{s['id']}: {len(ev)} evals but {len(pg)} pages"
        for t in pg:
            assert t["url"].startswith(br.AIAL_SITE + "evals/")
            assert t["url"].endswith("/"), "AIAL serves the page as a directory URL"
    # the page slug is the eval file's own stem, which is how AIAL builds it
    by = {s["id"]: s for s in srcs}
    a = next(s for s in srcs if s["targets"] and any(
        t["kind"] == "aial-eval" and t["url"].endswith("alpha.yaml") for t in s["targets"]))
    assert any(t["url"].endswith("/evals/alpha/") for t in a["targets"])


def test_the_scoring_framework_is_tracked_so_a_score_can_be_read_as_a_grade(tmp_path):
    out = tmp_path / "sources.json"
    br.main(str(_fake_aial(tmp_path, ["alpha"])), out_path=out)
    srcs = json.loads(out.read_text(encoding="utf-8"))["sources"]
    tracker = next(s for s in srcs if s["id"] == "aial/tracker")
    method = [t for t in tracker["targets"] if t["kind"] == "aial-method"]
    assert len(method) == len(br.AIAL_METHOD_PAGES) >= 3
    assert any(t["url"].endswith("/methodology") for t in method), (
        "without the methodology page a percentage cannot be read as a letter grade")



def test_two_eval_files_that_collapse_to_one_id_fail_the_build(tmp_path):
    # "claude-sonnet-4.5.yaml" and "claude-sonnet-4-5.yaml" both slugify to the
    # same source id; the second silently overwrote the first's history
    repo = _fake_aial(tmp_path, ["sonnet-4-5"])
    (repo / "evals" / "sonnet 4 5.yaml").write_text(
        'model_name: "Sonnet 4.5 again"\norganization: "Testorg"\n', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        br.main(str(repo), out_path=tmp_path / "sources.json")
    assert "same source id" in str(exc.value)


def test_a_model_with_a_tracked_document_is_not_reported_missing(tmp_path):
    # status came from AIAL's archive_file_name alone, so a page serving the
    # provider's captured, hashed PDF could render "Missing / none located"
    repo = _fake_aial(tmp_path, [])
    (repo / "evals" / "solo.yaml").write_text(
        'model_name: "Solo"\norganization: "Testorg"\n'
        'public_summary_link: "https://example.org/solo-summary.pdf"\n',
        encoding="utf-8")
    out = tmp_path / "sources.json"
    br.main(str(repo), out_path=out)
    src = [s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]
           if s["id"].endswith("/solo")][0]
    assert any(t["kind"] == "provider-live" for t in src["targets"])
    assert src["status"] == "published", "a tracked document rendered as missing"
