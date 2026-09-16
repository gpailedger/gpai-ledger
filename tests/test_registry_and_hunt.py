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


def test_a_hand_verified_kind_overrides_the_one_derived_from_metadata(tmp_path,
                                                                      monkeypatch):
    # EXTRA_TARGETS is a hand-verified classification; a URL-only dedupe kept the
    # derived kind, and kind drives the gone-wording and the hunt's suppression
    url = "https://example.org/nova-summary.pdf"
    repo = _fake_aial(tmp_path, [])
    (repo / "evals" / "nova.yaml").write_text(
        'model_name: "Nova"\norganization: "Testorg"\n'
        f'public_summary_link: "{url}"\n', encoding="utf-8")
    monkeypatch.setattr(br, "EXTRA_TARGETS", {"nova": [("provider-page", url)]})
    out = tmp_path / "sources.json"
    br.main(str(repo), out_path=out)
    src = [s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]
           if s["id"].endswith("/nova")][0]
    hits = [t for t in src["targets"] if t["url"] == url]
    assert len(hits) == 1, "the URL was added twice"
    assert hits[0]["kind"] == "provider-page", \
        "the hand-verified classification was discarded for the derived one"


# --- AIAL's newer metadata (the refresh failed on 12 and 13 Sep 2026) ---------

def test_unquoted_dates_and_placeholder_values_do_not_break_the_refresh(tmp_path):
    # one unquoted date in one upstream file parsed as a date object and crashed
    # the refresh two days running, leaving every sweep on a stale registry
    repo = _fake_aial(tmp_path, [])
    (repo / "evals" / "solo.yaml").write_text(
        "model_name: Solo\norganization: Testorg\n"
        "public_summary_link: https://example.org/solo.pdf\n"
        "archive_file_name: None\n"
        "public_summary_date: 2026-05-11\n"
        "model_publication_date: 2026-05-21 08:00:00\n"
        "evaluation_date: '2026-08-17 08:00:00'\n", encoding="utf-8")
    out = tmp_path / "sources.json"
    br.main(str(repo), out_path=out)
    solo = [s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]
            if s["id"] == "testorg/solo"][0]
    assert solo["aial"]["public_summary_date"] == "2026-05-11"
    assert solo["aial"]["model_publication_date"] == "2026-05-21"
    assert solo["aial"]["evaluation_date"] == "2026-08-17"
    assert solo["aial"]["archive_file_name"] == ""
    assert not any(t["kind"] == "aial-archive" for t in solo["targets"]), \
        "the string 'None' became an archive address that 404s every run"


def test_an_unreadable_upstream_file_does_not_hold_back_the_other_models(tmp_path):
    repo = _fake_aial(tmp_path, ["alpha"])
    (repo / "evals" / "broken.yaml").write_text("model_name: [unclosed\n", encoding="utf-8")
    out = tmp_path / "sources.json"
    br.main(str(repo), out_path=out)
    assert "testorg/alpha" in {s["id"] for s in json.loads(out.read_text(encoding="utf-8"))["sources"]}


def test_an_eval_file_renamed_with_an_organisation_prefix_keeps_its_model_id(tmp_path):
    # gpt-5-2.yaml became openai-gpt-5.2.yaml upstream; the id is the model's
    # permalink and every capture of it is filed under that id
    out = tmp_path / "sources.json"
    repo = _fake_aial(tmp_path, ["alpha-1-5"])
    br.main(str(repo), out_path=out)
    (repo / "evals" / "alpha-1-5.yaml").rename(repo / "evals" / "testorg-alpha-1.5.yaml")
    br.main(str(repo), out_path=out)              # used to refuse: it dropped testorg/alpha-1-5
    src = [s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]
           if s["id"] == "testorg/alpha-1-5"][0]
    assert src["aial"]["eval_yaml"] == "evals/testorg-alpha-1.5.yaml"


def test_the_organisation_prefix_stays_when_it_is_part_of_the_models_name(tmp_path):
    repo = _fake_aial(tmp_path, [])
    for stem, name in (("testorg-one", "Testorg One"), ("testorg-testorg-two", "Testorg Two")):
        (repo / "evals" / f"{stem}.yaml").write_text(
            f"model_name: {name}\norganization: Testorg\n", encoding="utf-8")
    out = tmp_path / "sources.json"
    br.main(str(repo), out_path=out)
    ids = {s["id"] for s in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    assert "testorg/testorg-one" in ids           # like Adobe Firefly, Minimax M3
    assert "testorg/testorg-two" in ids           # like deepseek-deepseek-v4


def test_one_organisation_spelled_two_ways_stays_one_provider(tmp_path):
    repo = _fake_aial(tmp_path, [])
    (repo / "evals" / "meta-glimmer.yaml").write_text(
        "model_name: Glimmer\norganization: Meta AI\n", encoding="utf-8")
    out = tmp_path / "sources.json"
    br.main(str(repo), out_path=out)
    src = [s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]
           if s["model"] == "Glimmer"][0]
    assert (src["id"], src["provider"]) == ("meta/glimmer", "Meta")


def test_a_declared_duplicate_is_skipped_only_while_its_model_is_built_elsewhere(
        tmp_path, monkeypatch):
    repo = _fake_aial(tmp_path, ["alpha"])
    (repo / "evals" / "alpha-copy.yaml").write_text(
        "model_name: Something Else\norganization: Otherorg\n"
        "public_summary_link: https://example.org/alpha.pdf\n", encoding="utf-8")
    monkeypatch.setattr(br, "DUPLICATE_EVAL_FILES",
                        {"alpha-copy.yaml": ("testorg/alpha", "a copy under a wrong header")})
    out = tmp_path / "sources.json"
    br.main(str(repo), out_path=out)
    srcs = json.loads(out.read_text(encoding="utf-8"))["sources"]
    assert [s["id"] for s in srcs if s.get("aial")] == ["testorg/alpha"]
    (repo / "evals" / "alpha.yaml").unlink()
    with pytest.raises(SystemExit, match="alpha-copy.yaml"):
        br.main(str(repo), out_path=out)


def test_an_evaluation_linking_another_models_document_is_held_out_while_the_link_stands(
        tmp_path, monkeypatch, capsys):
    # AIAL's "Nemotron 3 and 3.5 Family" links NVIDIA's Nemotron Nano v2 summary
    repo = _fake_aial(tmp_path, ["nano"])
    (repo / "evals" / "family.yaml").write_text(
        "model_name: Family\norganization: Testorg\n"
        "public_summary_link: https://example.org/nano.pdf\n", encoding="utf-8")
    monkeypatch.setattr(br, "MISATTRIBUTED_EVAL_FILES",
                        {"family.yaml": ("https://example.org/nano.pdf", "links nano's summary")})
    out = tmp_path / "sources.json"
    br.main(str(repo), out_path=out)                  # no longer refused as a shared document
    ids = {s["id"] for s in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    assert "testorg/nano" in ids and "testorg/family" not in ids
    (repo / "evals" / "family.yaml").write_text(
        "model_name: Family\norganization: Testorg\n"
        "public_summary_link: https://example.org/family.pdf\n", encoding="utf-8")
    br.main(str(repo), out_path=out)
    assert "testorg/family" in {s["id"] for s in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    assert "no longer links" in capsys.readouterr().out


def test_an_archive_name_upstream_does_not_hold_is_corrected_or_left_out(tmp_path, monkeypatch):
    # PLLuM 2512 Instruct's metadata names ..._instruct_... for a file archived as
    # ..._Instruct_...; Phi-4 Multimodal's names a .pdf archived as .pdf.pdf. Either
    # target would have failed on every sweep.
    repo = _fake_aial(tmp_path, [])
    for n, archive in (("one", "one_instruct_2026.pdf"), ("two", "Two_2026.pdf")):
        (repo / "evals" / f"{n}.yaml").write_text(
            f"model_name: {n}\norganization: Testorg\n"
            f"public_summary_link: https://example.org/{n}.pdf\n"
            f"archive_file_name: {archive}\n", encoding="utf-8")
    monkeypatch.setattr(br, "aial_archive_names",
                        lambda repo: {"One_Instruct_2026.pdf", "Two_2026.pdf.pdf"})
    out = tmp_path / "sources.json"
    br.main(str(repo), out_path=out)
    by = {s["id"]: s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]}

    def archives(sid):
        return [t["url"] for t in by[sid]["targets"] if t["kind"] == "aial-archive"]
    assert archives("testorg/one") == [br.AIAL_ARCHIVE_BASE + "One_Instruct_2026.pdf"]
    assert archives("testorg/two") == []
    assert by["testorg/two"]["status"] == "published"     # the provider's own copy stands


def test_an_archived_copy_already_tracked_is_kept_when_upstream_no_longer_holds_it(
        tmp_path, monkeypatch):
    repo = _fake_aial(tmp_path, ["solo"])                 # names solo.pdf
    out = tmp_path / "sources.json"
    monkeypatch.setattr(br, "aial_archive_names", lambda repo: {"solo.pdf"})
    br.main(str(repo), out_path=out)
    monkeypatch.setattr(br, "aial_archive_names", lambda repo: {"other.pdf"})
    br.main(str(repo), out_path=out)
    solo = [s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]
            if s["id"] == "testorg/solo"][0]
    assert br.AIAL_ARCHIVE_BASE + "solo.pdf" in [t["url"] for t in solo["targets"]], \
        "a tracked copy that vanishes is the sweep's absence to record, not the registry's to hide"


def test_two_models_claiming_one_document_fail_the_build(tmp_path):
    # an upstream file headed "Muse Glimmer / Meta AI" links xAI's Grok document
    repo = _fake_aial(tmp_path, ["grok"])
    (repo / "evals" / "glimmer.yaml").write_text(
        "model_name: Glimmer\norganization: Otherorg\n"
        "public_summary_link: https://example.org/grok.pdf\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="more than one model"):
        br.main(str(repo), out_path=tmp_path / "sources.json")


def test_a_hub_page_several_models_link_is_not_a_document_claim(tmp_path):
    repo = _fake_aial(tmp_path, [])
    for n in ("one", "two"):
        (repo / "evals" / f"{n}.yaml").write_text(
            f"model_name: {n}\norganization: Anthropic\n"
            "public_summary_link: https://trust.anthropic.com/resources\n",
            encoding="utf-8")
    br.main(str(repo), out_path=tmp_path / "sources.json")      # does not refuse


# --- AIAL renames files in waves; an id is a permanent address -----------------

NL = chr(10)


def test_a_renamed_eval_file_keeps_the_id_its_captures_live_under(tmp_path):
    # AIAL renamed 100 eval files on 14 Sep 2026. A filename is theirs to change;
    # the id is where this project's captures, permalinks and hashes live.
    out = tmp_path / "sources.json"
    repo = _fake_aial(tmp_path, ["alpha"])
    br.main(str(repo), out_path=out)
    (repo / "evals" / "alpha.yaml").rename(repo / "evals" / "testorg-alpha-v2.yaml")
    br.main(str(repo), out_path=out)
    by_id = {s["id"]: s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    assert "testorg/alpha" in by_id, "the rename moved the model to a new id"
    assert "testorg/alpha-v2" not in by_id
    assert by_id["testorg/alpha"]["aial"]["eval_yaml"] == "evals/testorg-alpha-v2.yaml"


def test_a_rename_that_also_renames_the_model_still_fails_closed(tmp_path):
    # the identity is what the file says; when that changes too, nothing proves the
    # two files are one model, and a human decides rather than the builder guessing
    out = tmp_path / "sources.json"
    repo = _fake_aial(tmp_path, ["alpha"])
    br.main(str(repo), out_path=out)
    before = out.read_bytes()
    (repo / "evals" / "alpha.yaml").unlink()
    (repo / "evals" / "testorg-alpha-v2.yaml").write_text(
        NL.join(["model_name: Alpha Two", "organization: Testorg",
                 "public_summary_link: https://example.org/alpha.pdf", ""]),
        encoding="utf-8")
    with pytest.raises(SystemExit, match="testorg/alpha"):
        br.main(str(repo), out_path=out)
    assert out.read_bytes() == before


def test_a_mislabelled_header_is_read_as_the_model_the_file_is_about(
        tmp_path, monkeypatch):
    # AIAL deleted grok-voice-think-fast-2.yaml on 14 Sep 2026 and kept its twin,
    # whose header reads Muse Glimmer / Meta AI while every link in it is xAI's
    out = tmp_path / "sources.json"
    repo = _fake_aial(tmp_path, ["alpha"])
    br.main(str(repo), out_path=out)
    (repo / "evals" / "alpha.yaml").unlink()
    (repo / "evals" / "testorg-alpha2.0.yaml").write_text(
        NL.join(["model_name: Wrong Model", "organization: Otherorg",
                 "public_summary_link: https://example.org/alpha.pdf", ""]),
        encoding="utf-8")
    monkeypatch.setattr(br, "MISLABELLED_EVAL_HEADERS", {
        "testorg-alpha2.0.yaml": (("Otherorg", "Wrong Model"), ("Testorg", "alpha"),
                                  "every link in it is alpha's")})
    br.main(str(repo), out_path=out)
    by_id = {s["id"]: s for s in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    assert "testorg/alpha" in by_id
    assert by_id["testorg/alpha"]["model"] == "alpha"
    assert not [s for s in by_id.values() if s["provider"] == "Otherorg"]


def test_a_header_aial_has_corrected_ends_its_own_override(tmp_path, monkeypatch, capsys):
    repo = _fake_aial(tmp_path, ["alpha"])
    monkeypatch.setattr(br, "MISLABELLED_EVAL_HEADERS", {
        "alpha.yaml": (("Otherorg", "Wrong Model"), ("Testorg", "alpha"), "why")})
    br.main(str(repo), out_path=tmp_path / "sources.json")
    out = capsys.readouterr().out
    assert "drop it from MISLABELLED_EVAL_HEADERS" in out
    assert "no longer reads Otherorg / Wrong Model" in out


def test_a_hand_pin_no_upstream_file_answers_to_is_reported(tmp_path, monkeypatch, capsys):
    # dead config is what turned AIAL's rename into three red sweeps: a duplicate
    # entry still named a file they had deleted
    monkeypatch.setattr(br, "EVAL_FILE_SLUGS", dict(br.EVAL_FILE_SLUGS, ghost="alpha"))
    br.main(str(_fake_aial(tmp_path, ["alpha"])), out_path=tmp_path / "sources.json")
    assert "no upstream file is named ghost.yaml" in capsys.readouterr().out
