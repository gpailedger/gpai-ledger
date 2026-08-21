"""Verdict-branch coverage for crawler/analyze_drift.py using CorpusBuilder
fixtures: every verdict main() can emit, plus newest-first live selection."""
import json

import analyze_drift


INPAGE_URL = "https://example.org/model/summary"

ARCH_TEXT = "the model was trained on publicly available web data collected during 2025"
DRIFT_TEXT = "the model was trained on privately licensed proprietary corpora acquired in 2026"


def run_drift(monkeypatch, tmp_path, data_root, sources):
    """Point the module's ROOT at a tmp tree (registry + reports/) and DATA at
    the fixture corpus, run main(), return the drift-latest.json results."""
    root = tmp_path / "root"
    (root / "crawler").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "crawler" / "sources.json").write_text(
        json.dumps({"sources": sources}), encoding="utf-8")
    monkeypatch.setattr(analyze_drift, "ROOT", root)
    monkeypatch.setattr(analyze_drift, "DATA", data_root)
    analyze_drift.main()
    return json.loads(
        (root / "reports" / "drift-latest.json").read_text(encoding="utf-8"))


def plain_source(source_id="prov/model", model="Model"):
    return {"id": source_id, "model": model, "status": "published", "targets": [
        {"kind": "provider-live", "url": "https://example.org/doc.pdf"},
        {"kind": "aial-archive", "url": "https://aial.ie/a.pdf"}]}


def inpage_source():
    return {"id": "prov/model", "model": "Model", "status": "published",
            "targets": [
                {"kind": "provider-page", "url": INPAGE_URL, "inpage": True},
                {"kind": "aial-archive", "url": "https://aial.ie/a.pdf"}]}


def test_identical_bytes(corpus, tmp_path, monkeypatch):
    raw = b"%PDF-1.4 same bytes"
    corpus.add_capture("prov/model", "provider-live-aaaa1111", raw=raw,
                       text="alpha beta gamma")
    corpus.add_capture("prov/model", "aial-archive-bbbb2222", raw=raw,
                       text="alpha beta gamma", kind="aial-archive")
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    assert out[0]["verdict"] == "identical-bytes"


def test_same_content_whitespace_only_reserialization(corpus, tmp_path, monkeypatch):
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       raw=b"%PDF-1.4 v-live", text="alpha beta gamma delta")
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive",
                       text="alpha  beta\tgamma\n delta ")
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    assert out[0]["verdict"] == "same-content"
    assert out[0]["similarity"] == 1.0


def test_drift_candidate_reports_word_changes(corpus, tmp_path, monkeypatch):
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       raw=b"%PDF-1.4 v-live", text=DRIFT_TEXT)
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive",
                       text=ARCH_TEXT)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    r = out[0]
    assert r["verdict"] == "DRIFT-CANDIDATE"
    assert r["similarity"] < 0.995
    assert r["changes"]
    assert all(set(c) == {"op", "old", "new"} for c in r["changes"])
    assert any("publicly" in c["old"] and "privately" in c["new"]
               for c in r["changes"])


def test_char_stream_rescues_word_split_artifacts(corpus, tmp_path, monkeypatch):
    # "Summary: 1.0" vs "Summary:1.0" tokenizes differently (word similarity
    # well under 0.995) but the alnum char streams are identical -> not drift
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       raw=b"%PDF-1.4 v-live",
                       text="Data Summary:1.0 tokens counted")
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive",
                       text="Data Summary: 1.0 tokens counted")
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    assert out[0]["verdict"] == "same-content"
    assert out[0]["similarity"] == 1.0


def test_format_mismatch_when_only_portal_html(corpus, tmp_path, monkeypatch):
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       raw=b"<html>hub</html>", ext=".html", text="hub page",
                       url="https://example.org/hub")
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive",
                       text=ARCH_TEXT)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    assert out[0]["verdict"] == "format-mismatch"
    assert "hub/portal" in out[0]["note"]


def test_incomplete_when_archive_missing(corpus, tmp_path, monkeypatch):
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       raw=b"%PDF-1.4 v-live", text=ARCH_TEXT)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    assert out[0]["verdict"] == "incomplete"
    assert out[0]["note"] == "live=1 archive=n"


def test_bundle_covered_for_anthropic_claude_portal_only(corpus, tmp_path, monkeypatch):
    corpus.add_capture("anthropic/claude-4-2", "provider-live-aaaa1111",
                       raw=b"<html>portal</html>", ext=".html",
                       text="portal listing", url="https://example.org/portal")
    corpus.add_capture("anthropic/claude-4-2", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive",
                       text=ARCH_TEXT)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(),
                    [plain_source("anthropic/claude-4-2", "Claude 4.2")])
    assert out[0]["verdict"] == "bundle-covered"


def test_inpage_baseline_single_capture(corpus, tmp_path, monkeypatch):
    corpus.add_capture("prov/model", "provider-page-aaaa1111",
                       raw=b"<html>doc</html>", ext=".html", text=ARCH_TEXT,
                       url=INPAGE_URL, kind="provider-page")
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive",
                       text=ARCH_TEXT)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [inpage_source()])
    assert out[0]["verdict"] == "inpage-baseline"


def test_inpage_self_history_same_content(corpus, tmp_path, monkeypatch):
    corpus.add_capture("prov/model", "provider-page-aaaa1111",
                       ts="20260801T060000Z", raw=b"<html>v1</html>",
                       ext=".html", text=ARCH_TEXT, url=INPAGE_URL,
                       kind="provider-page")
    corpus.add_capture("prov/model", "provider-page-aaaa1111",
                       ts="20260815T060000Z", raw=b"<html>v2</html>",
                       ext=".html", text=ARCH_TEXT, url=INPAGE_URL,
                       kind="provider-page")
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive",
                       text=ARCH_TEXT)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [inpage_source()])
    assert out[0]["verdict"] == "same-content"
    assert "in-page document" in out[0]["note"]


def test_inpage_self_history_drift(corpus, tmp_path, monkeypatch):
    corpus.add_capture("prov/model", "provider-page-aaaa1111",
                       ts="20260801T060000Z", raw=b"<html>v1</html>",
                       ext=".html", text=ARCH_TEXT, url=INPAGE_URL,
                       kind="provider-page")
    corpus.add_capture("prov/model", "provider-page-aaaa1111",
                       ts="20260815T060000Z", raw=b"<html>v2</html>",
                       ext=".html", text=DRIFT_TEXT, url=INPAGE_URL,
                       kind="provider-page")
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive",
                       text=ARCH_TEXT)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [inpage_source()])
    assert out[0]["verdict"] == "DRIFT-CANDIDATE"
    assert out[0]["similarity"] < 0.995


def test_inpage_capture_method_change(corpus, tmp_path, monkeypatch):
    corpus.add_capture("prov/model", "provider-page-aaaa1111",
                       ts="20260801T060000Z", raw=b"<html>v1</html>",
                       ext=".html", text=ARCH_TEXT, url=INPAGE_URL,
                       kind="provider-page")
    # newer capture was rendered; text also differs -> method change, not drift
    corpus.add_capture("prov/model", "provider-page-aaaa1111",
                       ts="20260815T060000Z", raw=b"<html>v2</html>",
                       ext=".html", text=DRIFT_TEXT, url=INPAGE_URL,
                       kind="provider-page",
                       extra_manifest={"http": {"url": INPAGE_URL,
                                                "rendered": True}})
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive",
                       text=ARCH_TEXT)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [inpage_source()])
    assert out[0]["verdict"] == "capture-method-change"


def test_newest_live_capture_wins_over_stale_entry(corpus, tmp_path, monkeypatch):
    # stale superseded entry (inserted first, byte-identical to the archive)
    # must lose to the newer drifted document; insertion order once masked drift
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       ts="20260101T000000Z", raw=b"%PDF-1.4 v-arch",
                       text=ARCH_TEXT)
    corpus.add_capture("prov/model", "provider-live-bbbb2222",
                       ts="20260815T060000Z", raw=b"%PDF-1.4 v-live",
                       text=DRIFT_TEXT)
    corpus.add_capture("prov/model", "aial-archive-cccc3333",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive",
                       text=ARCH_TEXT)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    assert out[0]["verdict"] == "DRIFT-CANDIDATE"


def test_newest_wins_even_when_stale_slug_sorts_higher(corpus, tmp_path, monkeypatch):
    # regression: sorting by the full capture PATH let the target-slug hash
    # segment decide before the timestamp — a stale entry whose slug sorted
    # lexically higher ("ffff") beat a newer capture under a lower slug
    # ("0000") and re-masked drift
    corpus.add_capture("prov/model", "provider-live-ffff9999",
                       ts="20260101T000000Z", raw=b"%PDF-1.4 v-arch",
                       text=ARCH_TEXT)
    corpus.add_capture("prov/model", "provider-live-0000aaaa",
                       ts="20260815T060000Z", raw=b"%PDF-1.4 v-live",
                       text=DRIFT_TEXT)
    corpus.add_capture("prov/model", "aial-archive-cccc3333",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive",
                       text=ARCH_TEXT)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    assert out[0]["verdict"] == "DRIFT-CANDIDATE"
