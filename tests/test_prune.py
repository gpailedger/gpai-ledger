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
