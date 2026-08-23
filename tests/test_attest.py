"""crawler/attest.py — second-vantage attestation of a target the sweep reports
absent: what it records, what it refuses to record, and how the sweep and the
hunt treat the event."""
import json

import attest
import capture as cap
import run_capture as rc_mod
import site_hunt


URL = "https://ex.org/summary.pdf"
BODY = b"%PDF-1.4 archived bytes"


def _store(tmp_path, retired=False):
    root = tmp_path / "data"
    cap_dir = "captures/prov__model/provider-live-aaaa1111/20260811T100000Z"
    d = root / cap_dir
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps(
        {"http": {"url": URL}, "target_kind": "provider-live", "stored_as": "raw.pdf"}),
        encoding="utf-8")
    entry = {"versions": [{"sha256": cap.sha256_hex(BODY), "dir": cap_dir}],
             "last_sha256": cap.sha256_hex(BODY), "last_capture": cap_dir}
    if retired:
        entry["retired"] = True
    (root / "state.json").write_text(json.dumps({
        "prov/model::provider-live-aaaa1111": entry,
        "prov/model::aial-archive-bbbb2222": dict(entry, last_capture=cap_dir)}), encoding="utf-8")
    (root / "events.jsonl").write_text(json.dumps(
        {"ts": "2026-08-22T07:00:00Z", "source": "prov/model", "target": "provider-live-aaaa1111",
         "outcome": "error", "absence": "persistent"}) + "\n", encoding="utf-8")
    return root


def _events(root):
    return [json.loads(l) for l in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def _ok(body):
    return lambda url, **k: (body, {"url": url, "final_url": url, "status_code": 200,
                                    "content_type": "application/pdf"})


def test_live_document_is_attested_with_hash_and_archive_comparison(tmp_path, monkeypatch):
    root = _store(tmp_path)
    monkeypatch.setattr(cap, "fetch", _ok(BODY))
    assert attest.main(["--source", "prov/model", "--data-root", str(root)]) == 0
    e = _events(root)[-1]
    assert e["outcome"] == "live-attested" and e["vantage"] == "operator"
    assert e["target"] == "provider-live-aaaa1111" and e["url"] == URL
    assert e["sha256"] == cap.sha256_hex(BODY) and e["same_as_archived"] is True
    assert e["size_bytes"] == len(BODY) and e["content_type"] == "application/pdf"
    assert e["supersedes_absence"] == "2026-08-22T07:00:00Z"
    # only the live target: the AIAL archive copy is not re-fetched
    assert sum(1 for x in _events(root) if x["outcome"] == "live-attested") == 1


def test_changed_document_is_attested_live_but_never_minted(tmp_path, monkeypatch):
    root = _store(tmp_path)
    monkeypatch.setattr(cap, "fetch", _ok(b"%PDF-1.4 new bytes"))
    assert attest.main(["--source", "prov/model", "--data-root", str(root)]) == 0
    e = _events(root)[-1]
    assert e["outcome"] == "live-attested" and e["same_as_archived"] is False
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert len(state["prov/model::provider-live-aaaa1111"]["versions"]) == 1
    assert not list((root / "captures").glob("*/*/*/raw.pdf"))


def test_second_vantage_404_confirms_the_absence(tmp_path, monkeypatch):
    root = _store(tmp_path)

    def gone(url, **k):
        raise cap.PermanentFetchError("HTTP 404 for u", status_code=404, headers={})
    monkeypatch.setattr(cap, "fetch", gone)
    assert attest.main(["--source", "prov/model", "--data-root", str(root)]) == 0
    e = _events(root)[-1]
    assert e["outcome"] == "error" and e["absence"] == "confirmed"
    assert e["confirmed_by"] == ["operator"] and e["vantage"] == "operator"
    assert e["observations"] == [{"status_code": 404}]


def test_non_observations_record_nothing(tmp_path, monkeypatch):
    root = _store(tmp_path)
    def raising(exc):
        def boom(url, **k):
            raise exc
        return boom
    for exc in (cap.PermanentFetchError("HTTP 403 for u", status_code=403, headers={}),
                TimeoutError("slow")):
        monkeypatch.setattr(cap, "fetch", raising(exc))
        assert attest.main(["--source", "prov/model", "--data-root", str(root)]) == 2
    monkeypatch.setattr(cap, "fetch", lambda url, **k: (None, {"status_code": 304}))
    assert attest.main(["--source", "prov/model", "--data-root", str(root)]) == 2
    assert len(_events(root)) == 1


def test_retired_and_unselected_targets_are_skipped(tmp_path, monkeypatch):
    root = _store(tmp_path, retired=True)
    monkeypatch.setattr(cap, "fetch", _ok(BODY))
    assert attest.main(["--source", "prov/model", "--data-root", str(root)]) == 2
    root = _store(tmp_path / "b")
    assert attest.main(["--source", "prov/model", "--target", "nope",
                        "--data-root", str(root)]) == 2
    assert attest.main(["--source", "prov/model", "--target", "aial-archive-bbbb2222",
                        "--data-root", str(root)]) == 0
    assert _events(root)[-1]["target"] == "aial-archive-bbbb2222"


def test_attestation_restarts_the_sweep_streak_like_a_live_witness(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in [
        {"source": "s", "target": "t", "outcome": "error", "absence": "persistent", "ts": "2026-08-22T06:00:00Z"},
        {"source": "s", "target": "t", "outcome": "live-attested", "ts": "2026-08-23T10:00:00Z"},
        {"source": "s", "target": "u", "outcome": "error", "absence": "persistent", "ts": "2026-08-23T06:00:00Z"},
    ]) + "\n", encoding="utf-8")
    s = rc_mod.absence_streaks(p)
    assert s[("s", "t")] == {"absent_on": set(), "contradicted_on": {"2026-08-23"}}
    assert s[("s", "u")] == {"absent_on": {"2026-08-23"}, "contradicted_on": set()}


def test_hunt_streak_feeds_on_persistent_and_breaks_on_attestation(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in [
        {"source": "s", "target": "t", "outcome": "error", "absence": "persistent", "url": "u", "kind": "provider-live", "ts": "1"},
        {"source": "s", "target": "t", "outcome": "error", "absence": "persistent", "url": "u", "kind": "provider-live", "ts": "2"},
    ]) + "\n", encoding="utf-8")
    assert ("s", "t") in site_hunt.error_streaks(p)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"source": "s", "target": "t", "outcome": "live-attested",
                             "kind": "provider-live", "ts": "3"}) + "\n")
    assert ("s", "t") not in site_hunt.error_streaks(p)


def test_operator_supplied_file_is_attested_without_any_fetch(tmp_path, monkeypatch):
    root = _store(tmp_path)
    fetched = []
    monkeypatch.setattr(cap, "fetch", lambda url, **k: fetched.append(url) or (b"x", {}))
    f = tmp_path / "download.pdf"
    f.write_bytes(BODY)
    assert attest.main(["--source", "prov/model", "--target", "provider-live-aaaa1111",
                        "--file", str(f), "--data-root", str(root)]) == 0
    e = _events(root)[-1]
    assert e["outcome"] == "live-attested" and e["observed_via"] == "operator-supplied file"
    assert e["sha256"] == cap.sha256_hex(BODY) and e["same_as_archived"] is True
    assert "status_code" not in e and fetched == []
    # the file mode needs exactly one selected target
    assert attest.main(["--source", "prov/model", "--target", "nope", "--file", str(f),
                        "--data-root", str(root)]) == 2
