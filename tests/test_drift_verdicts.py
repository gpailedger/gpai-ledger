"""Verdict-branch coverage for crawler/analyze_drift.py using CorpusBuilder
fixtures: every verdict main() can emit, newest-first live selection, the
near-identical verdict, the common-extractor re-extraction rule, and the
version-diffs ledger."""
import json

import pytest

import analyze_drift
from conftest import sha


INPAGE_URL = "https://example.org/model/summary"

ARCH_TEXT = "the model was trained on publicly available web data collected during 2025"
DRIFT_TEXT = "the model was trained on privately licensed proprietary corpora acquired in 2026"
# a long document: one substituted word scores well above 0.995
LONG = " ".join(f"word{i}" for i in range(300))
LONG_EDIT = LONG.replace("word150", "changed")


@pytest.fixture(autouse=True)
def _reextraction_reproduces_stored_text(monkeypatch, corpus):
    """Fixture corpora carry fake document bytes; by default re-extracting them
    yields the stored text again (the normal case: one extractor, same result),
    so a differing pair is `changed`, never `changed-unverified`. Tests of the
    re-extraction rule patch cap.extract_text themselves."""
    texts = {}
    real_add = corpus.add_capture

    def add_capture(*args, **kw):
        if "raw" in kw and "text" in kw:
            texts[kw["raw"]] = kw["text"]
        return real_add(*args, **kw)

    monkeypatch.setattr(corpus, "add_capture", add_capture)
    monkeypatch.setattr(analyze_drift.cap, "extract_text",
                        lambda raw, ext: (texts.get(raw), ["fixture extractor"]))


def _extractor(mapping):
    return lambda raw, ext: (mapping.get(raw), [])


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


def test_near_identical_one_word_edit_in_a_long_document(corpus, tmp_path, monkeypatch):
    # similarity >= 0.995 must never hide a real edit behind "same-content"
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       raw=b"%PDF-1.4 v-live", text=LONG_EDIT)
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive", text=LONG)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    r = out[0]
    assert r["verdict"] == "near-identical" and r["similarity"] >= 0.995
    assert r["word_delta"] == 1
    (c,) = r["changes"]
    assert c["op"] == "replace" and "word150" in c["old"] and "changed" in c["new"]
    assert r["compared_via"].startswith("re-extracted from the stored bytes with pypdf")
    assert r["same_tool"] is True


def test_word_delta_counts_only_real_differences_not_split_artifacts(corpus, tmp_path, monkeypatch):
    # "Summary:1.0" vs "Summary: 1.0" and "re- flect" vs "reflect" carry the same
    # letters: they must neither count nor be listed; the one real edit must
    longer = " ".join(f"token{i}" for i in range(1000))
    arch = "Data Summary:1.0 re- flect " + longer
    live = "Data Summary: 1.0 reflect " + longer.replace("token500", "edited")
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       raw=b"%PDF-1.4 v-live", text=live)
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive", text=arch)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    r = out[0]
    assert r["verdict"] == "near-identical"
    assert r["word_delta"] == 1
    (c,) = r["changes"]
    assert c["op"] == "replace" and "token500" in c["old"] and "edited" in c["new"]


@pytest.mark.parametrize("old,new,delta", [
    ("Web crawling: ☒ Yes ☐ No", "Web crawling: ☐ Yes ☒ No", 2),     # tick box moved
    ("trained on 1.5 trillion tokens", "trained on 15 trillion tokens", 1),  # decimal point
    ("share of web data: 2.5%", "share of web data: 25%", 1),
    ("Klein Flux Tools API", "Klein FLUX Tools API", 1),                 # case only
    ("Απάντηση: Ναι", "Απάντηση: Όχι", 1),                               # non-Latin word
])
def test_identity_rule_sees_meaning_bearing_differences(old, new, delta):
    res = analyze_drift.compare_words(analyze_drift.words_of(old), analyze_drift.words_of(new))
    assert res["identical"] is False and res["word_delta"] == delta


@pytest.mark.parametrize("old,new", [
    ("Data Summary:1.0 tokens", "Data Summary: 1.0 tokens"),          # word split
    ("high- quality re- flect", "high-quality reflect"),                # hyphenation
    ("Modalità ☒ Testo ☐ Immagine", "Modalità  ☒  Testo\n☐ Immagine"),  # layout only
    ("circa 1,5 miliardi", "circa 1.5 miliardi"),                       # locale separator
])
def test_identity_rule_still_ignores_split_and_layout_artifacts(old, new):
    res = analyze_drift.compare_words(analyze_drift.words_of(old), analyze_drift.words_of(new))
    assert res["identical"] is True and res["word_delta"] == 0


@pytest.mark.parametrize("old,new", [
    ("a causal language model", "a casual language model"),   # transposition
    ("collected from the web", "collected form the web"),
    ("data collected in 2025", "data collected in 2052"),      # digit transposition
    ("opt-out on request", "opt-out no request"),
    ("circa 1,5 miliardi di token", "circa 15 miliardi di token"),   # comma decimal
    ("the models are listed and the dataset is", "the model are listed and the datasets is"),
])
def test_an_edit_is_never_booked_as_a_move(old, new):
    res = analyze_drift.compare_words(analyze_drift.words_of(old), analyze_drift.words_of(new))
    assert res["identical"] is False
    assert res["word_delta"] >= 1 and res["moved_words"] == 0
    assert all(c["op"] != "moved" for c in res["changes"])


def test_two_independent_edits_count_twice():
    res = analyze_drift.compare_words(
        analyze_drift.words_of("alpha beta gamma delta epsilon zeta eta"),
        analyze_drift.words_of("beta gamma delta epsilon zeta eta theta"))
    assert res["word_delta"] == 2


def test_swapped_blocks_are_a_move():
    a = analyze_drift.words_of("intro one two three four middle five six seven eight end")
    b = analyze_drift.words_of("intro five six seven eight middle one two three four end")
    res = analyze_drift.compare_words(a, b)
    # one block is booked as moved; the pivot word "middle" is at worst counted
    # conservatively (a deletion and an insertion), never the swapped words
    assert res["moved_words"] >= 4 and res["word_delta"] <= 2
    assert any(c["op"] == "moved" and c["old"] in ("one two three four", "five six seven eight")
               for c in res["changes"])


def test_a_phrase_shared_by_two_rewritten_paragraphs_is_not_a_move():
    # "di dati" occurs in a deleted paragraph and in an inserted one: changed,
    # not moved (a 2-word block is a move only as a whole deleted/inserted run)
    old = analyze_drift.words_of("P Q di dati R S T U V W")
    new = analyze_drift.words_of("T U V W X Y di dati Z")
    res = analyze_drift.compare_words(old, new)
    assert res["moved_words"] == 0 and res["word_delta"] >= 6
    assert all(c["op"] != "moved" for c in res["changes"])


def test_a_whole_run_header_of_two_words_is_a_move():
    old = analyze_drift.words_of("Transparency Report one two three four five six")
    new = analyze_drift.words_of("one two three four five six Transparency Report")
    res = analyze_drift.compare_words(old, new)
    assert res["moved_words"] == 2 and res["word_delta"] == 0
    assert res["changes"] == [{"op": "moved", "old": "Transparency Report",
                               "new": "Transparency Report"}]


def test_a_bare_dash_does_not_pad_a_move_block():
    # "A -" is one identity-bearing word plus a dash: never a two-word move
    old = analyze_drift.words_of("start A - mid one two three four five end")
    new = analyze_drift.words_of("start mid one two three four five A - end")
    res = analyze_drift.compare_words(old, new)
    assert res["moved_words"] == 0 and res["word_delta"] == 2
    assert all(c["op"] != "moved" for c in res["changes"])


def test_change_listing_shows_the_words_around_the_difference():
    # a PDF page number sits next to a section number: the listing must show
    # the region so a reader sees what was actually absent
    res = analyze_drift.compare_words(
        analyze_drift.words_of("requested. 2 2.3 Personal Information"),
        analyze_drift.words_of("requested. 2.3 Personal Information"))
    assert res["word_delta"] == 1
    (c,) = res["changes"]
    assert c["op"] == "delete" and "2 2.3" in c["old"] and "2.3" in c["new"]


def test_changed_unverified_when_a_side_cannot_be_reextracted(corpus, tmp_path, monkeypatch):
    corpus.add_capture(ts="20260801T060000Z", raw=b"%PDF-1.4 v1", text=LONG)
    corpus.add_capture(ts="20260815T060000Z", raw=b"%PDF-1.4 v2", text=LONG_EDIT)
    monkeypatch.setattr(analyze_drift.cap, "extract_text",
                        _extractor({b"%PDF-1.4 v1": LONG}))        # v2 yields no text
    run_drift(monkeypatch, tmp_path, corpus.finish(), [])
    ledger = json.loads((tmp_path / "root" / "reports" / "version-diffs.json")
                        .read_text(encoding="utf-8"))
    (rec,) = ledger.values()
    assert rec["verdict"] == "changed-unverified" and rec["word_delta"] == 1
    assert "re-extraction unavailable for the newest capture" in rec["compared_via"]
    assert rec["same_tool"] is False


def test_artifact_merged_into_the_same_opcode_as_a_real_edit_is_not_counted():
    old = analyze_drift.words_of("Summary:1.0 word150 follows")
    new = analyze_drift.words_of("Summary: 1.0 changed follows")
    res = analyze_drift.compare_words(old, new)
    assert res["word_delta"] == 1
    (c,) = res["changes"]
    assert c["op"] == "replace" and "word150" in c["old"] and "changed" in c["new"]


def test_moved_running_header_counts_as_moved_not_changed():
    header = "Public Summary of Training Content"
    body = " ".join(f"w{i}" for i in range(60))
    old = analyze_drift.words_of(f"{header} {body} tail")
    new = analyze_drift.words_of(f"{body} {header} tail")
    res = analyze_drift.compare_words(old, new)
    assert res["identical"] is False
    assert res["word_delta"] == 0 and res["moved_words"] == 5
    assert [c["op"] for c in res["changes"]] == ["moved"]
    assert res["changes"][0]["old"] == header


def test_compared_via_names_the_tool_on_each_side(corpus, tmp_path, monkeypatch):
    # live markdown vs AIAL's PDF print: two extractors, no "same tool" claim
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       raw=b"live markdown", ext=".txt", text=LONG_EDIT)
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive", text=LONG)
    monkeypatch.setattr(analyze_drift.cap, "extract_text",
                        _extractor({b"live markdown": LONG_EDIT, b"%PDF-1.4 v-arch": LONG}))
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    r = out[0]
    assert r["verdict"] == "near-identical" and r["same_tool"] is False
    assert r["compared_via"].startswith("re-extracted from the stored bytes: pypdf")
    assert "(archive) vs utf-8 decode (live)" in r["compared_via"]


def test_live_vs_archive_comparison_is_cached_in_the_ledger(corpus, tmp_path, monkeypatch):
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       raw=b"%PDF-1.4 v-live", text=LONG_EDIT)
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive", text=LONG)
    run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    ledger = json.loads((tmp_path / "root" / "reports" / "version-diffs.json")
                        .read_text(encoding="utf-8"))
    (key,) = ledger.keys()
    assert "::live-vs-archive::" in key and ledger[key]["rule"] == analyze_drift.RULE_VERSION
    calls = []
    monkeypatch.setattr(analyze_drift, "compare_captures",
                        lambda a, b, roles=None: calls.append((a, b)) or {"verdict": "no-text"})
    analyze_drift.main()
    assert calls == []


def test_record_under_an_older_rule_is_recomputed_and_the_old_verdict_kept(corpus, tmp_path, monkeypatch):
    corpus.add_capture(ts="20260801T060000Z", raw=b"%PDF-1.4 v1", text=LONG)
    corpus.add_capture(ts="20260815T060000Z", raw=b"%PDF-1.4 v2", text=LONG_EDIT)
    root = corpus.finish()
    run_drift(monkeypatch, tmp_path, root, [])
    p = tmp_path / "root" / "reports" / "version-diffs.json"
    ledger = json.loads(p.read_text(encoding="utf-8"))
    (key,) = ledger.keys()
    ledger[key].update({"verdict": "identical-text", "rule": "identity-v1"})
    p.write_text(json.dumps(ledger), encoding="utf-8")
    analyze_drift.main()
    rec = json.loads(p.read_text(encoding="utf-8"))[key]
    assert rec["verdict"] == "changed" and rec["rule"] == analyze_drift.RULE_VERSION
    assert rec["prior_verdicts"][0]["verdict"] == "identical-text"
    assert rec["prior_verdicts"][0]["rule"] == "identity-v1"


def test_reextraction_neutralizes_extractor_era_drift(corpus, tmp_path, monkeypatch):
    # the stored extracts differ only because two extractor versions produced
    # them; extracted again with ONE extractor they are identical -> same-content
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       raw=b"%PDF-1.4 v-live", text=LONG)
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive", text=LONG_EDIT)
    monkeypatch.setattr(analyze_drift.cap, "extract_text",
                        _extractor({b"%PDF-1.4 v-live": LONG, b"%PDF-1.4 v-arch": LONG}))
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    assert out[0]["verdict"] == "same-content" and out[0]["similarity"] == 1.0
    assert out[0]["compared_via"].startswith("re-extracted from the stored bytes with pypdf")
    assert out[0]["same_tool"] is True


def test_reextraction_confirms_a_real_edit(corpus, tmp_path, monkeypatch):
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       raw=b"%PDF-1.4 v-live", text=LONG_EDIT)
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive", text=LONG)
    monkeypatch.setattr(analyze_drift.cap, "extract_text",
                        _extractor({b"%PDF-1.4 v-live": LONG_EDIT, b"%PDF-1.4 v-arch": LONG}))
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    r = out[0]
    assert r["verdict"] == "near-identical" and r["word_delta"] == 1
    assert r["compared_via"].startswith("re-extracted")


def test_self_history_changed_is_recorded_once_in_the_ledger(corpus, tmp_path, monkeypatch):
    corpus.add_capture(ts="20260801T060000Z", raw=b"%PDF-1.4 v1", text=LONG)
    corpus.add_capture(ts="20260815T060000Z", raw=b"%PDF-1.4 v2", text=LONG_EDIT)
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v1", kind="aial-archive", text=LONG)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    sh = out[0]["self_history"]
    assert sh["verdict"] == "changed" and sh["word_delta"] == 1
    assert sh["from_dir"].endswith("20260801T060000Z") and sh["to_dir"].endswith("20260815T060000Z")
    ledger = json.loads((tmp_path / "root" / "reports" / "version-diffs.json")
                        .read_text(encoding="utf-8"))
    key = f"prov/model::provider-live-aaaa1111::{sh['from_dir']}>{sh['to_dir']}"
    rec = ledger[key]
    assert rec["from_sha256"] == sha(b"%PDF-1.4 v1") and rec["to_sha256"] == sha(b"%PDF-1.4 v2")
    (c,) = rec["changes"]
    assert c["op"] == "replace" and "word150" in c["old"] and "changed" in c["new"]
    assert rec["rule"] == analyze_drift.RULE_VERSION
    # a later run reuses the record: the pair is never compared again
    compared = []
    real = analyze_drift.compare_captures
    monkeypatch.setattr(analyze_drift, "compare_captures",
                        lambda a, b, roles=("previous", "newest"):
                        compared.append((a, b)) or real(a, b, roles))
    analyze_drift.main()
    assert (sh["from_dir"], sh["to_dir"]) not in compared
    ledger2 = json.loads((tmp_path / "root" / "reports" / "version-diffs.json")
                         .read_text(encoding="utf-8"))
    assert ledger2 == ledger


def test_self_history_identical_text_when_only_bytes_changed(corpus, tmp_path, monkeypatch):
    corpus.add_capture(ts="20260801T060000Z", raw=b"%PDF-1.4 v1", text="stable body text")
    corpus.add_capture(ts="20260815T060000Z", raw=b"%PDF-1.4 v2", text="stable  body\ntext")
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v2", kind="aial-archive", text="stable body text")
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    assert out[0]["self_history"]["verdict"] == "identical-text"
    assert out[0]["self_history"]["word_delta"] == 0


def test_self_history_single_version(corpus, tmp_path, monkeypatch):
    corpus.add_capture("prov/model", "provider-live-aaaa1111",
                       raw=b"%PDF-1.4 same", text=ARCH_TEXT)
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 same", kind="aial-archive", text=ARCH_TEXT)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [plain_source()])
    assert out[0]["verdict"] == "identical-bytes"
    assert out[0]["self_history"] == {"verdict": "single-version"}


def test_ledger_method_change_is_never_a_content_change(corpus, tmp_path, monkeypatch):
    # an app-shell capture followed by a browser-rendered one: the text differs
    # because the method did, and that must not read as a provider edit
    corpus.add_capture("prov/model", "provider-page-aaaa1111", ts="20260801T060000Z",
                       raw=b"<html>shell</html>", ext=".html", text="shell only",
                       url=INPAGE_URL, kind="provider-page")
    corpus.add_capture("prov/model", "provider-page-aaaa1111", ts="20260815T060000Z",
                       raw=b"<html>full</html>", ext=".html", text=ARCH_TEXT,
                       url=INPAGE_URL, kind="provider-page",
                       extra_manifest={"http": {"url": INPAGE_URL, "rendered": True}})
    corpus.add_capture("prov/model", "aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 v-arch", kind="aial-archive", text=ARCH_TEXT)
    out = run_drift(monkeypatch, tmp_path, corpus.finish(), [inpage_source()])
    assert out[0]["verdict"] == "capture-method-change"
    assert out[0]["self_history"]["verdict"] == "method-changed"
    ledger = json.loads((tmp_path / "root" / "reports" / "version-diffs.json")
                        .read_text(encoding="utf-8"))
    (rec,) = ledger.values()
    assert rec["verdict"] == "method-changed" and rec["word_delta"] > 0


def test_ledger_covers_every_target_pair_not_only_published_documents(corpus, tmp_path, monkeypatch):
    corpus.add_capture("other/watch", "watch-page-cccc3333", ts="20260801T060000Z",
                       raw=b"<html>a</html>", ext=".html", text="portal a", kind="watch-page",
                       url="https://example.org/portal")
    corpus.add_capture("other/watch", "watch-page-cccc3333", ts="20260815T060000Z",
                       raw=b"<html>b</html>", ext=".html", text="portal b", kind="watch-page",
                       url="https://example.org/portal")
    run_drift(monkeypatch, tmp_path, corpus.finish(), [])
    ledger = json.loads((tmp_path / "root" / "reports" / "version-diffs.json")
                        .read_text(encoding="utf-8"))
    (rec,) = ledger.values()
    assert rec["source"] == "other/watch" and rec["verdict"] == "changed"


def test_consent_state_is_compared_by_value(corpus, tmp_path, monkeypatch):
    # a banner that survived one rendered capture (1 node removed) and not the
    # next (2 removed) is a capture-method difference, never a content change
    for ts, removed, text in (("20260801T060000Z", 2, "page body"),
                              ("20260815T060000Z", 1, "We use cookies page body")):
        corpus.add_capture("prov/page", "provider-page-dddd4444", ts=ts,
                           raw=f"<html>{ts}</html>".encode(), ext=".html", text=text,
                           kind="provider-page", url="https://example.org/page",
                           extra_manifest={"http": {"url": "https://example.org/page",
                                                    "rendered": True,
                                                    "consent_nodes_removed": removed}})
    run_drift(monkeypatch, tmp_path, corpus.finish(), [])
    ledger = json.loads((tmp_path / "root" / "reports" / "version-diffs.json")
                        .read_text(encoding="utf-8"))
    (rec,) = ledger.values()
    assert rec["verdict"] == "method-changed"


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
