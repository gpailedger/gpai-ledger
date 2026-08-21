"""Tests for crawler/verify_corpus.py — the corpus safety net.

verify() appends into module-level FAILS/WARNS/STATS, so every test resets them
(autouse fixture + a mid-test reset after proving the corpus clean), then breaks
exactly one invariant and asserts the right check code fires.
"""
import hashlib
import json
from pathlib import Path

import pytest

from conftest import canon_sha, load_module, make_ots, sha

VC = load_module(
    str(Path(__file__).resolve().parent.parent / "crawler" / "verify_corpus.py"),
    "verify_corpus_mod")

# state key produced by add_capture() defaults
K = "prov/model::provider-live-aaaa1111"
GHOST = "captures/ghost/provider-live-0000/20260101T000000Z"


def _reset():
    VC.FAILS.clear()
    VC.WARNS.clear()
    VC.STATS.clear()


@pytest.fixture(autouse=True)
def _fresh_verifier():
    _reset()
    yield


def _codes():
    return sorted(c for c, _, _ in VC.FAILS)


def _msgs():
    return [m for _, _, m in VC.FAILS]


def assert_clean(root):
    assert VC.verify(root) == 0, VC.FAILS
    _reset()


def edit_json(path, mutate):
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    mutate(obj)
    Path(path).write_text(json.dumps(obj, indent=2), encoding="utf-8")


def edit_state_entry(root, key=K, **changes):
    edit_json(root / "state.json", lambda st: st[key].update(changes))


def append_events(root, *events):
    with (root / "events.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


# --- clean corpus baseline ---

def test_clean_two_source_corpus_passes(corpus):
    corpus.add_capture()
    corpus.add_capture(source_id="other/model", tslug="aial-archive-bbbb2222",
                       raw=b"%PDF-1.4 other", text="other text body")
    root = corpus.finish()
    assert VC.verify(root) == 0
    assert VC.FAILS == []
    assert VC.STATS["versions"] == 2


# --- C1: raw bytes vs manifest ---

def test_c1_flipped_raw_byte_fails_sha(corpus):
    d, _ = corpus.add_capture(with_ots=False)  # no ots: isolate the C1 finding
    root = corpus.finish()
    assert_clean(root)
    raw = (d / "raw.pdf").read_bytes()
    (d / "raw.pdf").write_bytes(b"X" + raw[1:])  # same length: only sha trips
    assert VC.verify(root) == 1
    assert _codes() == ["C1"] and "sha256 mismatch" in _msgs()[0]


def test_c1_wrong_size_bytes_in_manifest(corpus):
    d, m = corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    edit_json(d / "manifest.json", lambda mm: mm.update(size_bytes=m["size_bytes"] + 7))
    assert VC.verify(root) == 1
    assert _codes() == ["C1"] and "size mismatch" in _msgs()[0]


def test_c1_raw_named_by_manifest_absent(corpus):
    d, _ = corpus.add_capture(with_ots=False)
    root = corpus.finish()
    assert_clean(root)
    (d / "raw.pdf").unlink()
    assert VC.verify(root) == 1
    assert _codes() == ["C1"] and "absent" in _msgs()[0]


# --- C2: OTS proofs ---

def test_c2_corrupt_ots_bytes_unparseable(corpus):
    d, _ = corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    (d / "raw.pdf.ots").write_bytes(b"\x00 not an ots stream")
    assert VC.verify(root) == 1
    assert _codes() == ["C2"] and "unparseable" in _msgs()[0]


def test_c2_ots_digest_for_different_document(corpus):
    d, _ = corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    # a real, parseable proof — stamping some other document's sha
    (d / "raw.pdf.ots").write_bytes(make_ots(sha(b"a different document")))
    assert VC.verify(root) == 1
    assert _codes() == ["C2"] and "digest does not match" in _msgs()[0]


def test_c2_orphan_ots_beside_deleted_raw(corpus):
    d, _ = corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    (d / "raw.pdf").unlink()  # raw.pdf.ots stays behind
    assert VC.verify(root) == 1
    assert _codes() == ["C1", "C2"]
    assert any(c == "C2" and "orphan" in m for c, _, m in VC.FAILS)


def test_c2_ots_in_dir_unreferenced_by_state(corpus):
    corpus.add_capture()
    corpus.add_capture(source_id="other/model", tslug="provider-live-cccc3333",
                       raw=b"%PDF-1.4 other", text="other text body")
    root = corpus.finish()
    assert_clean(root)
    edit_json(root / "state.json",
              lambda st: st.pop("other/model::provider-live-cccc3333"))
    assert VC.verify(root) == 1
    assert any(c == "C2" and "not referenced" in m for c, _, m in VC.FAILS)


# --- C3: state <-> disk ---

def test_c3_partial_write_raw_without_manifest(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    d = root / "captures" / "prov__model" / "provider-live-aaaa1111" / "20260816T060000Z"
    d.mkdir(parents=True)
    (d / "raw.pdf").write_bytes(b"%PDF-1.4 interrupted sweep")
    assert VC.verify(root) == 1
    assert _codes() == ["C3"] and "partial" in _msgs()[0]


def test_c3_orphan_formed_dir_not_referenced(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    d = root / "captures" / "prov__model" / "provider-live-aaaa1111" / "20260817T060000Z"
    d.mkdir(parents=True)
    (d / "raw.pdf").write_bytes(b"%PDF-1.4 stray")
    (d / "manifest.json").write_text(json.dumps({"stored_as": "raw.pdf"}),
                                     encoding="utf-8")
    assert VC.verify(root) == 1
    assert _codes() == ["C3"] and "orphan" in _msgs()[0]


# --- C4: events log ---

def test_c4_new_event_dir_absent_fails(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    append_events(root, {"outcome": "new", "dir": GHOST, "sha256": sha(b"ghost")})
    assert VC.verify(root) == 1
    assert _codes() == ["C4"] and "absent and not pruned" in _msgs()[0]


def test_c4_new_event_covered_by_pruned_noise(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    append_events(root,
                  {"outcome": "new", "dir": GHOST, "sha256": sha(b"ghost")},
                  {"outcome": "pruned-noise", "dir": GHOST})
    assert VC.verify(root) == 0
    assert VC.FAILS == []


def test_c4_new_event_covered_by_scope_repack(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    gsha = sha(b"ghost")
    append_events(root,
                  {"outcome": "new", "dir": GHOST, "sha256": gsha},
                  {"outcome": "scope-repack", "prior_sha256": gsha})
    assert VC.verify(root) == 0
    assert VC.FAILS == []


def test_c4_bare_prune_after_log_correction_fails(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    append_events(root,
                  {"ts": "2026-08-20T00:00:00Z", "outcome": "log-correction"},
                  {"outcome": "pruned-noise", "dir": GHOST})
    assert VC.verify(root) == 1
    assert _codes() == ["C4"] and "missing ts/sha256/reason" in _msgs()[0]


def test_c4_bare_prune_before_log_correction_grandfathered(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    append_events(root,
                  {"outcome": "pruned-noise", "dir": GHOST},
                  {"ts": "2026-08-20T00:00:00Z", "outcome": "log-correction"})
    assert VC.verify(root) == 0
    assert VC.FAILS == []


def test_c4_unparseable_event_line_fails(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    with (root / "events.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("{not json\n")
    assert VC.verify(root) == 1
    assert _codes() == ["C4"] and "unparseable event lines" in _msgs()[0]


# --- C5: extracted text ---

def test_c5_null_text_sha_beside_extracted_txt(corpus):
    d, _ = corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    # null both sides so the C6 last_text cross-check stays quiet
    edit_json(d / "manifest.json", lambda m: m.update(text_sha256=None))
    edit_state_entry(root, last_text_sha256=None)
    assert VC.verify(root) == 1
    assert _codes() == ["C5"] and "unverifiable" in _msgs()[0]


def test_c5_text_sha_mismatch_after_txt_rewrite(corpus):
    d, _ = corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    (d / "extracted.txt").write_text("silently rewritten body", encoding="utf-8")
    assert VC.verify(root) == 1
    assert _codes() == ["C5"] and "canonical text sha mismatch" in _msgs()[0]


ZIP_NOTES = [
    {"inner_file": "training_summary.txt", "inner_sha256": sha(b"inner-a")},
    {"inner_file": "annex.txt", "inner_sha256": sha(b"inner-b")},
    {"note": "digest-less entries are ignored by the verifier"},
]


def zip_content_key(notes):
    # mirror the verifier: sorted (inner_file, inner_sha256) pairs, json-dumped
    pairs = sorted((n["inner_file"], n["inner_sha256"]) for n in notes
                   if isinstance(n, dict) and "inner_sha256" in n)
    return hashlib.sha256(
        json.dumps(pairs, ensure_ascii=False).encode("utf-8")).hexdigest()


def add_zip_capture(corpus):
    return corpus.add_capture(
        source_id="zip/model", tslug="aial-archive-dddd4444",
        raw=b"PK\x03\x04 zip fixture", ext=".zip", text=None,
        extra_manifest={"text_sha256": zip_content_key(ZIP_NOTES),
                        "extraction_notes": ZIP_NOTES})


def test_c5_zip_content_key_clean_passes(corpus):
    add_zip_capture(corpus)
    root = corpus.finish()
    assert VC.verify(root) == 0
    assert VC.FAILS == []


def test_c5_zip_content_key_mismatch(corpus):
    d, _ = add_zip_capture(corpus)
    root = corpus.finish()
    assert_clean(root)
    # tamper an inner digest: recomputed key drifts from manifest text_sha256
    edit_json(d / "manifest.json",
              lambda m: m["extraction_notes"][0].update(inner_sha256=sha(b"tampered")))
    assert VC.verify(root) == 1
    assert _codes() == ["C5"] and "zip content key mismatch" in _msgs()[0]


# --- C6: state summary fields ---

def test_c6_last_sha256_mismatch(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    edit_state_entry(root, last_sha256=sha(b"other"))
    assert VC.verify(root) == 1
    assert _codes() == ["C6"] and "last_sha256 != versions[-1].sha256" in _msgs()[0]


def test_c6_last_capture_mismatch(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    edit_state_entry(root, last_capture="captures/prov__model/x/19700101T000000Z")
    assert VC.verify(root) == 1
    assert _codes() == ["C6"] and "last_capture != versions[-1].dir" in _msgs()[0]


def test_c6_last_text_sha256_disagrees_with_manifest(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    edit_state_entry(root, last_text_sha256=canon_sha("some other body"))
    assert VC.verify(root) == 1
    assert _codes() == ["C6"] and "last_text_sha256 disagrees" in _msgs()[0]


def test_c6_state_version_sha_differs_from_manifest(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    wrong = sha(b"drifted")

    def mutate(st):
        st[K]["versions"][-1]["sha256"] = wrong
        st[K]["last_sha256"] = wrong  # keep the last_sha256 check quiet: isolate it

    edit_json(root / "state.json", mutate)
    assert VC.verify(root) == 1
    assert _codes() == ["C6"] and "state version sha != manifest sha" in _msgs()[0]


# --- C7: retired entries ---

def test_c7_retired_flag_not_a_string_fails(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    edit_state_entry(root, retired=True)
    assert VC.verify(root) == 1
    assert _codes() == ["C7"] and "not a string" in _msgs()[0]


def test_c7_retired_with_history_passes(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    edit_state_entry(root, retired="superseded by relocation")
    assert VC.verify(root) == 0
    assert VC.FAILS == []


def test_c7_nonretired_entry_without_versions_warns_not_fails(corpus):
    corpus.add_capture()
    root = corpus.finish()
    assert_clean(root)
    edit_json(root / "state.json",
              lambda st: st.__setitem__("ghost/model::empty", {"versions": []}))
    assert VC.verify(root) == 0
    assert VC.FAILS == []
    assert any(c == "C7" and "no versions" in m for c, _, m in VC.WARNS)
