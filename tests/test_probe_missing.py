"""crawler/probe_missing.py — the Tier-1 prober that can promote a document into
the registry without a human. Its promotion rules decide what a public ledger
asserts about a named company, so they are tested harder than anything else here.
"""
import json
import sys
from pathlib import Path

import pytest

import capture as cap
from conftest import load_module

ROOT = Path(__file__).resolve().parent.parent
PM = load_module(str(ROOT / "crawler" / "probe_missing.py"), "probe_missing_mod")
DEC = load_module(str(ROOT / "crawler" / "decisions.py"), "decisions_for_probe")


@pytest.fixture(autouse=True)
def _queue_in_tmp(tmp_path, monkeypatch):
    """probe_missing hands candidates it will not promote to crawler/decisions.py,
    which writes crawler/pending.json. Without this every test that produces a
    candidate would write into the real repository — one did."""
    monkeypatch.setattr(DEC, "PENDING", tmp_path / "queue" / "pending.json")
    monkeypatch.setattr(DEC, "DECISIONS", tmp_path / "queue" / "decisions.json")
    (tmp_path / "queue").mkdir(exist_ok=True)
    monkeypatch.setitem(sys.modules, "decisions", DEC)

SUMMARY = ("Public Summary of Training Content\n{model}\n"
           "Article 53(1)(d) of Regulation (EU) 2024/1689\n"
           "1.3 Modalities, overall training data size\n"
           "Size of dataset per modality: text\n")


def _src(sid, model, provider, status, urls=()):
    return {"id": sid, "provider": provider, "model": model, "status": status,
            "targets": [{"kind": "provider-live", "url": u, "note": ""} for u in urls]}


# --- learning a provider's own URL shape ---

def test_patterns_are_learned_only_from_the_providers_own_published_urls():
    sources = [
        _src("openai/gpt-5-2", "GPT-5.2", "OpenAI", "published",
             ["https://cdn.openai.com/pdf/gpt-5-2-eu-ai-act-public-summary.pdf"]),
        # a third party's archived copy is not the provider's pattern
        _src("x/y", "Why 1", "Other", "published",
             ["https://aial.ie/research/gpai-training-transparency/archive/Why_1.pdf"]),
        # a missing model contributes no pattern
        _src("openai/sora-2", "Sora 2", "OpenAI", "missing",
             ["https://cdn.openai.com/pdf/sora-2-something.pdf"]),
    ]
    pat = PM.learn_patterns(sources)
    assert set(pat) == {"OpenAI"}
    assert pat["OpenAI"][0][1] == "gpt-5-2"


def test_candidates_substitute_this_models_slug_into_that_shape():
    sources = [_src("openai/gpt-5-2", "GPT-5.2", "OpenAI", "published",
                    ["https://cdn.openai.com/pdf/gpt-5-2-eu-ai-act-public-summary.pdf"])]
    pat = PM.learn_patterns(sources)
    got = PM.candidates_for(_src("openai/gpt-5-6-sol", "GPT-5.6 Sol", "OpenAI", "missing"), pat)
    assert "https://cdn.openai.com/pdf/gpt-5-6-sol-eu-ai-act-public-summary.pdf" in got


def test_a_provider_with_no_published_document_yields_no_guesses():
    assert PM.candidates_for(_src("a/b", "B", "Nobody", "missing"), {}) == []


# --- the promotion rules ---

@pytest.mark.parametrize("text,model,expected", [
    ("Data Summary for MAI-Image-2.5 Version 1", "MAI-Image-2", False),
    ("Data Summary for MAI-Image-2 Version 1", "MAI-Image-2", True),
    ("Public Summary of Training Content Claude Opus 4.8", "Claude Opus 4", False),
    ("summary for GPT-5.6 Luna", "GPT-5.6 Sol", False),
    ("summary for GPT-5.6 Sol", "GPT-5.6 Sol", True),
])
def test_a_sibling_models_document_is_never_read_as_this_models(text, model, expected):
    # sibling summaries run 95-99% identical; the model name is what separates them
    assert PM.names_the_model(text, model) is expected


def test_a_page_that_is_not_a_summary_scores_too_few_template_markers():
    assert PM.looks_like_summary("Our usage policy prohibits abuse.") < PM.MIN_MARKERS
    assert PM.looks_like_summary(SUMMARY.format(model="X")) >= PM.MIN_MARKERS


def _run(monkeypatch, tmp_path, sources, fetched, state=None):
    """Drive main() with a fake web: {url: (bytes, text)} and a tmp corpus."""
    crawler = tmp_path / "crawler"
    crawler.mkdir()
    (crawler / "sources.json").write_text(json.dumps({"sources": sources}), encoding="utf-8")
    (tmp_path / "reports").mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (data / "state.json").write_text(json.dumps(state or {}), encoding="utf-8")
    monkeypatch.setattr(PM, "SOURCES", crawler / "sources.json")
    monkeypatch.setattr(PM, "DISCOVERED", crawler / "discovered.json")
    monkeypatch.setattr(PM, "REPORT", tmp_path / "reports" / "probe-missing-latest.md")
    monkeypatch.setattr(PM, "DATA", data)
    monkeypatch.setattr(PM, "THROTTLE_S", 0)

    def fake_fetch(url, **kw):
        if url not in fetched:
            raise cap.PermanentFetchError(f"HTTP 404 for {url}", status_code=404)
        return fetched[url][0], {"url": url, "final_url": url, "status_code": 200,
                                 "content_type": "application/pdf"}
    monkeypatch.setattr(PM.cap, "fetch", fake_fetch)
    monkeypatch.setattr(PM.cap, "extract_text",
                        lambda raw, ext: (fetched.get_text(raw) if hasattr(fetched, "get_text")
                                          else {v[0]: v[1] for v in fetched.values()}.get(raw), []))
    monkeypatch.setattr(sys, "argv", ["probe_missing.py"])
    assert PM.main([]) == 0
    disc = crawler / "discovered.json"
    return (json.loads(disc.read_text(encoding="utf-8")) if disc.exists() else {},
            (tmp_path / "reports" / "probe-missing-latest.md").read_text(encoding="utf-8"))


PUBLISHED = _src("prov/one", "Model One", "Prov", "published",
                 ["https://prov.example/pdf/model-one-summary.pdf"])


def test_a_real_hit_is_promoted_and_becomes_a_registry_target(tmp_path, monkeypatch):
    missing = _src("prov/two", "Model Two", "Prov", "missing")
    body = b"%PDF-1.4 two"
    disc, report = _run(monkeypatch, tmp_path, [PUBLISHED, missing],
                        {"https://prov.example/pdf/model-two-summary.pdf":
                         (body, SUMMARY.format(model="Model Two"))})
    assert disc["prov/two"][0]["url"] == "https://prov.example/pdf/model-two-summary.pdf"
    assert disc["prov/two"][0]["kind"] == "provider-live"
    assert "Model Two" in disc["prov/two"][0]["note"]
    assert "Promoted to the registry: 1" in report


def test_a_siblings_document_served_at_this_models_url_is_not_promoted(tmp_path, monkeypatch):
    # the provider's URL pattern resolves, but the document is another model's
    missing = _src("prov/two", "Model Two", "Prov", "missing")
    disc, report = _run(monkeypatch, tmp_path, [PUBLISHED, missing],
                        {"https://prov.example/pdf/model-two-summary.pdf":
                         (b"%PDF x", SUMMARY.format(model="Model One"))})
    assert disc == {}
    assert "does not name this model" not in report      # it is in the table, not prose
    assert "| prov/two |" in report and "| no |" in report


def test_a_document_already_held_for_another_model_is_not_promoted(tmp_path, monkeypatch):
    missing = _src("prov/two", "Model Two", "Prov", "missing")
    text = SUMMARY.format(model="Model Two")
    cap_dir = "captures/prov__one/provider-live-a/20260811T100000Z"
    d = tmp_path / "data" / cap_dir
    state = {"prov/one::provider-live-a": {"versions": [{"sha256": "a" * 64, "dir": cap_dir}]}}

    def prep():
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps(
            {"text_sha256": cap.canonical_text_sha(text)}), encoding="utf-8")
    (tmp_path / "data").mkdir()
    prep()
    crawler = tmp_path / "crawler"
    crawler.mkdir()
    (crawler / "sources.json").write_text(
        json.dumps({"sources": [PUBLISHED, missing]}), encoding="utf-8")
    (tmp_path / "reports").mkdir()
    (tmp_path / "data" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    for name, val in (("SOURCES", crawler / "sources.json"),
                      ("DISCOVERED", crawler / "discovered.json"),
                      ("REPORT", tmp_path / "reports" / "probe-missing-latest.md"),
                      ("DATA", tmp_path / "data"), ("THROTTLE_S", 0)):
        monkeypatch.setattr(PM, name, val)
    url = "https://prov.example/pdf/model-two-summary.pdf"
    monkeypatch.setattr(PM.cap, "fetch", lambda u, **kw: (b"%PDF", {
        "url": u, "final_url": u, "status_code": 200, "content_type": "application/pdf"})
        if u == url else (_ for _ in ()).throw(
            cap.PermanentFetchError("HTTP 404", status_code=404)))
    monkeypatch.setattr(PM.cap, "extract_text", lambda raw, ext: (text, []))
    assert PM.main([]) == 0
    assert not (crawler / "discovered.json").exists()
    report = (tmp_path / "reports" / "probe-missing-latest.md").read_text(encoding="utf-8")
    assert "prov/one" in report          # the clash names the model that already holds it


def test_nothing_found_is_reported_plainly(tmp_path, monkeypatch):
    missing = _src("prov/two", "Model Two", "Prov", "missing")
    disc, report = _run(monkeypatch, tmp_path, [PUBLISHED, missing], {})
    assert disc == {}
    assert "Promoted to the registry: 0" in report


def test_a_url_already_tracked_is_never_re_probed(tmp_path, monkeypatch):
    url = "https://prov.example/pdf/model-two-summary.pdf"
    missing = _src("prov/two", "Model Two", "Prov", "missing", [url])
    calls = []
    crawler = tmp_path / "crawler"
    crawler.mkdir()
    (crawler / "sources.json").write_text(
        json.dumps({"sources": [PUBLISHED, missing]}), encoding="utf-8")
    (tmp_path / "reports").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text("{}", encoding="utf-8")
    for name, val in (("SOURCES", crawler / "sources.json"),
                      ("DISCOVERED", crawler / "discovered.json"),
                      ("REPORT", tmp_path / "reports" / "probe-missing-latest.md"),
                      ("DATA", tmp_path / "data"), ("THROTTLE_S", 0)):
        monkeypatch.setattr(PM, name, val)
    monkeypatch.setattr(PM.cap, "fetch",
                        lambda u, **kw: calls.append(u) or (_ for _ in ()).throw(
                            cap.PermanentFetchError("HTTP 404", status_code=404)))
    PM.main([])
    assert url not in calls


def test_a_candidate_it_will_not_promote_reaches_the_decision_queue(tmp_path, monkeypatch):
    # the whole point of the queue: a document we fetched but would not attribute
    # must become a question for the operator, not a line in an unread report
    missing = _src("prov/two", "Model Two", "Prov", "missing")
    # fetches, looks like a summary, but names the sibling model
    disc, _report = _run(monkeypatch, tmp_path, [PUBLISHED, missing],
                         {"https://prov.example/pdf/model-two-summary.pdf":
                          (b"%PDF x", SUMMARY.format(model="Model One"))})
    assert disc == {}
    queued = json.loads(DEC.PENDING.read_text(encoding="utf-8"))
    assert len(queued) == 1
    entry = next(iter(queued.values()))
    assert entry["source_id"] == "prov/two"
    assert entry["url"] == "https://prov.example/pdf/model-two-summary.pdf"
    assert "does not name this model" in entry["why"]
    assert entry["issue"] is None            # an issue is opened by the hunt step


def test_template_markers_survive_the_line_breaks_pdf_extraction_leaves():
    # extraction breaks phrases across lines; scoring raw text made most real
    # provider PDFs score zero and silently stopped them being promoted
    broken = ("Public Summary of\ntraining content\n\nArticle\n53(1)(d) of "
              "Regulation (EU) 2024/1689\n1.3 Modalities,   overall\ntraining data size")
    assert PM.looks_like_summary(broken) >= PM.MIN_MARKERS


# --- the gates an adversarial review found unconstrained ---

FLUX_SIBS = ["FLUX.2 Klein", "FLUX.2 [max]"]


def test_a_document_for_a_longer_named_sibling_is_not_this_models():
    # live in the registry today: FLUX.2, FLUX.2 Klein and FLUX.2 [max] are all
    # missing, so the probe will request the FLUX.2 slug once any of them lands
    klein = "Public Summary of Training Content for FLUX.2 Klein — Article 53(1)(d)"
    assert PM.names_the_model(klein, "FLUX.2", FLUX_SIBS) is False
    assert PM.names_the_model(klein, "FLUX.2 Klein", FLUX_SIBS) is True
    plain = "Public Summary of Training Content for FLUX.2 — Article 53(1)(d)"
    assert PM.names_the_model(plain, "FLUX.2", FLUX_SIBS) is True


def test_a_mention_buried_in_the_body_does_not_name_the_document():
    buried = "x" * (PM.NAME_REGION_CHARS + 50) + " FLUX.2 is compared here "
    assert PM.names_the_model(buried, "FLUX.2", FLUX_SIBS) is False


def test_an_ordinary_model_card_carries_no_distinctive_phrase():
    card = ("GPT-5.6 Sol is a general-purpose AI model. Training data: publicly "
            "available data and licensed corpora.")
    assert PM.looks_like_summary(card) >= PM.MIN_MARKERS      # weak markers alone
    assert PM.has_distinctive_marker(card) is False
    assert PM.has_distinctive_marker(SUMMARY.format(model="X")) is True


def test_a_slug_in_the_hostname_is_never_substituted():
    # rewriting a host would send the probe to whatever third party registered it
    src = {"provider": "P", "model": "Model Two", "id": "p/2"}
    pat = {"P": [("https://model-one.example/model-one/summary.pdf", "model-one")]}
    got = PM.candidates_for(src, pat)
    assert got, "the path should still be substituted"
    assert all(u.startswith("https://model-one.example/") for u in got)


def test_the_budget_env_var_cannot_crash_the_weekly_pass(monkeypatch):
    monkeypatch.setenv("GPAI_PROBE_BUDGET", "15m")
    assert PM.budget_from_env() == float(PM.BUDGET_S)
    monkeypatch.setenv("GPAI_PROBE_BUDGET", "5")
    assert PM.budget_from_env() == 60.0
    monkeypatch.setenv("GPAI_PROBE_BUDGET", "99999")
    assert PM.budget_from_env() == 3600.0


def test_a_document_without_a_distinctive_phrase_is_queued_not_promoted(tmp_path, monkeypatch):
    # end to end: the marker gate was previously unconstrained by any test
    missing = _src("prov/two", "Model Two", "Prov", "missing")
    card = ("Model Two is a general-purpose AI model.\nTraining data: publicly "
            "available sources.")
    disc, report = _run(monkeypatch, tmp_path, [PUBLISHED, missing],
                        {"https://prov.example/pdf/model-two-summary.pdf": (b"%PDF", card)})
    assert disc == {}, "a model card was promoted as a summary"
    assert "| prov/two |" in report                      # listed for a human
    queued = json.loads(DEC.PENDING.read_text(encoding="utf-8"))
    assert any("distinctive" in v["why"] for v in queued.values())


def test_a_bundles_text_hash_still_counts_as_already_held(tmp_path):
    # bundle captures record the served text under extracted_text_sha256
    data = tmp_path / "data"
    cap_dir = "captures/prov__one/provider-live-a/20260811T100000Z"
    (data / cap_dir).mkdir(parents=True)
    text = SUMMARY.format(model="Model One")
    (data / cap_dir / "manifest.json").write_text(json.dumps(
        {"extracted_text_sha256": cap.canonical_text_sha(text)}), encoding="utf-8")
    (data / "state.json").write_text(json.dumps(
        {"prov/one::provider-live-a": {"versions": [{"sha256": "a" * 64, "dir": cap_dir}]}}),
        encoding="utf-8")
    import unittest.mock as mock
    with mock.patch.object(PM, "DATA", data):
        assert cap.canonical_text_sha(text) in PM.text_shas_by_model([])

