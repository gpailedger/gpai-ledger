"""Tests for crawler/prune_capture.py — the only sanctioned evidence-deletion tool.

main() parses sys.argv and works against the module-level DATA root, so every
test patches both (DATA -> the fixture corpus, argv -> the prune request) and
asserts on the corpus files afterwards. A capture may only be pruned when its
canonical text matches a neighbouring version: bytes changed, content did not.
"""
import json
import sys
from pathlib import Path

from conftest import canon_sha, load_module, sha

ROOT = Path(__file__).resolve().parent.parent
PC = load_module(str(ROOT / "crawler" / "prune_capture.py"), "prune_capture_mod")
VC = load_module(str(ROOT / "crawler" / "verify_corpus.py"),
                 "verify_corpus_for_prune")

# state key produced by add_capture() defaults
K = "prov/model::provider-live-aaaa1111"
TS1, TS2, TS3 = "20260815T060000Z", "20260816T060000Z", "20260817T060000Z"


def run_prune(monkeypatch, root, capture_ts, source_id="prov/model",
              tslug="provider-live-aaaa1111", reason="banner-only re-render"):
    monkeypatch.setattr(PC, "DATA", root)
    monkeypatch.setattr(sys, "argv", ["prune_capture.py", source_id, tslug,
                                      capture_ts, "--reason", reason])
    return PC.main()


def three_versions(corpus, t1, t2, t3):
    v1 = corpus.add_capture(ts=TS1, raw=b"%PDF-1.4 v1", text=t1)
    v2 = corpus.add_capture(ts=TS2, raw=b"%PDF-1.4 v2", text=t2)
    v3 = corpus.add_capture(ts=TS3, raw=b"%PDF-1.4 v3", text=t3)
    return v1, v2, v3


def read_state(root):
    return json.loads((root / "state.json").read_text(encoding="utf-8"))


def read_events(root):
    return [json.loads(l) for l in
            (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]


# --- refusals ---

def test_refuses_content_bearing_capture_and_touches_nothing(corpus, monkeypatch):
    (_, _), (d2, _), (_, _) = three_versions(
        corpus, "first body", "second body", "third body")
    root = corpus.finish()
    state_before = (root / "state.json").read_bytes()
    events_before = (root / "events.jsonl").read_bytes()
    assert run_prune(monkeypatch, root, TS2) == 1
    assert d2.exists()
    assert (root / "state.json").read_bytes() == state_before
    assert (root / "events.jsonl").read_bytes() == events_before


def test_refuses_unknown_state_key(corpus, monkeypatch):
    corpus.add_capture()
    root = corpus.finish()
    assert run_prune(monkeypatch, root, TS1, source_id="ghost/model") == 1


def test_refuses_unknown_capture_ts(corpus, monkeypatch):
    corpus.add_capture(ts=TS1)
    root = corpus.finish()
    assert run_prune(monkeypatch, root, "19700101T000000Z") == 1


def test_refuses_sole_version_without_neighbours(corpus, monkeypatch):
    d, _ = corpus.add_capture(ts=TS1)
    root = corpus.finish()
    assert run_prune(monkeypatch, root, TS1) == 1
    assert d.exists()


def test_refuses_victim_with_null_text_sha256(corpus, monkeypatch):
    # no extracted.txt -> text_sha256 None: no text identity provable, even
    # though the neighbour's text_sha is a value None could compare against
    corpus.add_capture(ts=TS1, raw=b"%PDF-1.4 v1", text="shared body")
    d2, _ = corpus.add_capture(ts=TS2, raw=b"%PDF-1.4 v2", text=None)
    root = corpus.finish()
    assert run_prune(monkeypatch, root, TS2) == 1
    assert d2.exists()


def test_refuses_first_version_even_when_the_next_one_is_identical(corpus, monkeypatch):
    # v1 is the earliest dated sighting of its content: its OTS proof and fetch
    # time are the evidence of when it was first observed — never noise
    (d1, _), (_, _), (_, _) = three_versions(
        corpus, "stable body text", "stable  body\ntext", "a new third body")
    root = corpus.finish()
    assert run_prune(monkeypatch, root, TS1) == 1
    assert d1.exists()


# --- successful prunes ---

def test_prunes_middle_noise_version_and_repairs_state(corpus, monkeypatch):
    # v2 canonical text == v1's (whitespace churn only), raw bytes differ
    (_, m1), (d2, _), (_, m3) = three_versions(
        corpus, "stable body text", "stable  body\ntext", "a new third body")
    root = corpus.finish()
    assert run_prune(monkeypatch, root, TS2) == 0
    assert not d2.exists()
    entry = read_state(root)[K]
    assert [v["sha256"] for v in entry["versions"]] == [m1["sha256"], m3["sha256"]]
    assert entry["last_sha256"] == m3["sha256"]
    assert entry["last_capture"].endswith("/" + TS3)
    assert entry["last_text_sha256"] == canon_sha("a new third body")


def test_prune_appends_fully_attributed_event(corpus, monkeypatch):
    three_versions(corpus, "stable body text", "stable  body\ntext",
                   "a new third body")
    root = corpus.finish()
    v2_dir = read_state(root)[K]["versions"][1]["dir"]
    assert run_prune(monkeypatch, root, TS2, reason="banner churn") == 0
    e = read_events(root)[-1]
    assert e["outcome"] == "pruned-noise"
    assert e["dir"] == v2_dir
    assert e["sha256"] == sha(b"%PDF-1.4 v2")
    assert e["text_sha256"] == canon_sha("stable body text")
    assert e["reason"] == "banner churn"
    assert e["via"] == "prune_capture"
    assert e["ts"].endswith("Z") and len(e["ts"]) == 20


def test_prunes_last_version_repairs_tail_pointers(corpus, monkeypatch):
    # v3's canonical text == v2's: the tail itself is the noise
    (_, _), (_, m2), (d3, _) = three_versions(
        corpus, "first body", "final body text", "final  body\ntext")
    root = corpus.finish()
    assert run_prune(monkeypatch, root, TS3) == 0
    assert not d3.exists()
    entry = read_state(root)[K]
    assert len(entry["versions"]) == 2
    assert entry["last_sha256"] == m2["sha256"]
    assert entry["last_capture"] == entry["versions"][-1]["dir"]
    assert entry["last_capture"].endswith("/" + TS2)
    assert entry["last_text_sha256"] == canon_sha("final body text")


def test_pruned_corpus_still_passes_verify_corpus(corpus, monkeypatch):
    three_versions(corpus, "stable body text", "stable  body\ntext",
                   "a new third body")
    root = corpus.finish()
    assert run_prune(monkeypatch, root, TS2) == 0
    VC.FAILS.clear()
    VC.WARNS.clear()
    VC.STATS.clear()
    assert VC.verify(root) == 0, VC.FAILS


# --- the duplicate route: the same bytes are retained somewhere else -----------

NL = chr(10)
DUP_K = "prov/model::aial-eval-history-bbbb2222"
OTHER_K = "other/model::aial-eval-history-cccc3333"
SAME = b"model_name: Phi-4"


def run_batch(monkeypatch, root, lines, reason="duplicate of a capture kept elsewhere"):
    batch = root / "batch.tsv"
    batch.write_text(NL.join(lines) + NL, encoding="utf-8")
    monkeypatch.setattr(PC, "DATA", root)
    monkeypatch.setattr(sys, "argv", ["prune_capture.py", "--batch", str(batch),
                                      "--reason", reason])
    return PC.main()


def test_prunes_a_duplicate_of_a_capture_kept_elsewhere(corpus, monkeypatch):
    # AIAL renamed 100 eval files on 14 Sep 2026 and the harvest stored states it
    # already held under the new paths: the same bytes, filed twice
    corpus.add_capture(source_id="prov/model", tslug="aial-eval-history-aaaa9999",
                       ts=TS1, raw=SAME, ext=".yaml", text="Phi-4",
                       kind="aial-eval-history")
    dup, _ = corpus.add_capture(source_id="prov/model", tslug="aial-eval-history-bbbb2222",
                                ts=TS2, raw=SAME, ext=".yaml", text="Phi-4",
                                kind="aial-eval-history")
    root = corpus.finish()
    assert run_prune(monkeypatch, root, TS2, tslug="aial-eval-history-bbbb2222",
                     reason="second chain from an upstream rename") == 0
    assert not dup.exists()
    assert read_state(root)[DUP_K]["versions"] == []
    e = read_events(root)[-1]
    assert e["outcome"] == "pruned-duplicate"
    assert e["sha256"] == sha(SAME)
    assert e["survives_in"]["dir"].endswith("/" + TS1)
    assert e["survives_in"]["source"] == "prov/model"
    assert e["via"] == "prune_capture" and e["reason"]


def test_an_emptied_chain_keeps_no_pointer_into_a_removed_directory(corpus, monkeypatch):
    corpus.add_capture(source_id="prov/model", tslug="aial-eval-history-aaaa9999",
                       ts=TS1, raw=SAME, ext=".yaml", text="Phi-4",
                       kind="aial-eval-history")
    corpus.add_capture(source_id="prov/model", tslug="aial-eval-history-bbbb2222",
                       ts=TS2, raw=SAME, ext=".yaml", text="Phi-4",
                       kind="aial-eval-history")
    root = corpus.finish()
    assert run_prune(monkeypatch, root, TS2, tslug="aial-eval-history-bbbb2222") == 0
    entry = read_state(root)[DUP_K]
    assert "last_capture" not in entry and "last_sha256" not in entry
    VC.FAILS.clear(); VC.WARNS.clear(); VC.STATS.clear()
    assert VC.verify(root) == 0, VC.FAILS


def test_the_survivor_may_be_another_models_capture_and_is_named(corpus, monkeypatch):
    # AIAL's regenerated pages were filed under the tracker while the registry was
    # stale; the content now lives under the model it belongs to
    corpus.add_capture(source_id="other/model", tslug="aial-eval-history-cccc3333",
                       ts=TS1, raw=SAME, ext=".yaml", text="Phi-4",
                       kind="aial-eval-history", provider="Other", model="Model")
    corpus.add_capture(source_id="prov/model", tslug="aial-eval-history-bbbb2222",
                       ts=TS2, raw=SAME, ext=".yaml", text="Phi-4",
                       kind="aial-eval-history")
    root = corpus.finish()
    assert run_prune(monkeypatch, root, TS2, tslug="aial-eval-history-bbbb2222") == 0
    e = read_events(root)[-1]
    assert e["survives_in"]["source"] == "other/model"
    assert e["survives_in"]["target"] == "aial-eval-history-cccc3333"


def test_a_batch_never_removes_both_copies_of_the_same_bytes(corpus, monkeypatch):
    corpus.add_capture(source_id="prov/model", tslug="aial-eval-history-aaaa9999",
                       ts=TS1, raw=SAME, ext=".yaml", text="Phi-4",
                       kind="aial-eval-history")
    corpus.add_capture(source_id="prov/model", tslug="aial-eval-history-bbbb2222",
                       ts=TS2, raw=SAME, ext=".yaml", text="Phi-4",
                       kind="aial-eval-history")
    root = corpus.finish()
    rc = run_batch(monkeypatch, root, [
        "prov/model" + chr(9) + "aial-eval-history-aaaa9999" + chr(9) + TS1,
        "prov/model" + chr(9) + "aial-eval-history-bbbb2222" + chr(9) + TS2])
    assert rc == 1, "a batch that would empty the corpus of these bytes must not pass"
    left = [v for k in (K, DUP_K, "prov/model::aial-eval-history-aaaa9999")
            for v in read_state(root).get(k, {}).get("versions", [])]
    # neither is removed: with both listed, neither has a survivor, so the tool
    # refuses both rather than quietly picking one to keep
    assert len(left) == 2, "a capture was removed although its twin was also queued"


def test_refuses_a_duplicate_whose_only_twin_is_under_a_retired_entry(corpus, monkeypatch):
    corpus.add_capture(source_id="other/model", tslug="aial-eval-history-cccc3333",
                       ts=TS1, raw=SAME, ext=".yaml", text="Phi-4",
                       kind="aial-eval-history", provider="Other", model="Model")
    dup, _ = corpus.add_capture(source_id="prov/model", tslug="aial-eval-history-bbbb2222",
                                ts=TS2, raw=SAME, ext=".yaml", text="Phi-4",
                                kind="aial-eval-history")
    root = corpus.finish()
    state = read_state(root)
    state[OTHER_K]["retired"] = "superseded upstream"
    (root / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    assert run_prune(monkeypatch, root, TS2, tslug="aial-eval-history-bbbb2222") == 1
    assert dup.exists(), "the capture was removed although nothing retained holds its bytes"
