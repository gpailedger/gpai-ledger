"""store_new_version invariants + run_capture.py sweep behavior (fully offline)."""
import hashlib
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import capture as cap
from conftest import canon_sha, load_module, sha

ROOT = Path(__file__).resolve().parent.parent
rc_mod = load_module(str(ROOT / "crawler" / "run_capture.py"), "run_capture_mod")

URL = "https://ex.org/doc.pdf"
TSLUG = "provider-live-aaaa1111"


def _meta(url, ctype="application/pdf", status=200, etag=None, last_modified=None):
    return {"url": url, "final_url": url, "status_code": status,
            "content_type": ctype, "etag": etag, "last_modified": last_modified,
            "content_length": None, "fetched_at": "2026-08-20T00:00:00Z"}


def _boom(*a, **k):
    raise AssertionError("must not be called")


def _mint(store, **overrides):
    text = overrides.pop("text", "alpha beta gamma")
    args = dict(source_id="prov/model", provider="Prov", model="Model",
                kind="provider-live", tslug=TSLUG, event_url=URL,
                raw=b"%PDF-1.4 v1", meta=_meta(URL), ext=".pdf", text=text,
                notes=["note-1"], text_sha=(canon_sha(text) if text else None),
                wayback_url=None, do_ots=False)
    args.update(overrides)
    return cap.store_new_version(store, **args)


def _events(data_root):
    return [json.loads(line) for line in
            (Path(data_root) / "events.jsonl").read_text(encoding="utf-8").splitlines()]


# --- A. store_new_version invariants ---

def test_store_new_version_writes_all_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "wayback_save",
                        lambda url, **k: {"ok": True, "snapshot": "s", "at": "t"})
    monkeypatch.setattr(cap, "ots_stamp",
                        lambda d: (b"OTS", {"ok": True, "calendars": ["c"], "at": "t"}))
    store = cap.Store(tmp_path / "data")
    rel, m = _mint(store, wayback_url=URL, do_ots=True)
    d = store.root / rel
    assert (d / "raw.pdf").read_bytes() == b"%PDF-1.4 v1"
    assert (d / "extracted.txt").read_text(encoding="utf-8") == "alpha beta gamma"
    assert (d / "raw.pdf.ots").read_bytes() == b"OTS"
    assert json.loads((d / "manifest.json").read_text(encoding="utf-8")) == m

def test_store_new_version_manifest_fields(tmp_path):
    store = cap.Store(tmp_path / "data")
    _, m = _mint(store)
    assert m["sha256"] == sha(b"%PDF-1.4 v1")
    assert m["size_bytes"] == len(b"%PDF-1.4 v1")
    assert m["stored_as"] == "raw.pdf"
    assert m["text_sha256"] == canon_sha("alpha beta gamma")
    assert m["prior_sha256"] is None
    assert m["http"] == _meta(URL)
    assert m["extraction_notes"] == ["note-1"]

def test_store_new_version_state_entry(tmp_path):
    store = cap.Store(tmp_path / "data")
    rel, m = _mint(store)
    state = json.loads((store.root / "state.json").read_text(encoding="utf-8"))
    entry = state[f"prov/model::{TSLUG}"]
    assert entry["last_sha256"] == m["sha256"]
    assert entry["last_text_sha256"] == m["text_sha256"]
    assert entry["last_capture"] == rel
    assert entry["versions"] == [{"sha256": m["sha256"], "dir": rel}]

def test_store_new_version_event_carries_extra(tmp_path):
    store = cap.Store(tmp_path / "data")
    rel, m = _mint(store, event_extra={"extracted": True, "via": "conditional-get"})
    (e,) = _events(store.root)
    assert e["source"] == "prov/model" and e["target"] == TSLUG
    assert e["url"] == URL and e["kind"] == "provider-live"
    assert e["outcome"] == "new" and e["sha256"] == m["sha256"] and e["dir"] == rel
    assert e["extracted"] is True and e["via"] == "conditional-get"

def test_store_new_version_prior_sha_chains(tmp_path):
    store = cap.Store(tmp_path / "data")
    _, m1 = _mint(store, raw=b"v1 bytes")
    _, m2 = _mint(store, raw=b"v2 bytes")
    assert m2["prior_sha256"] == m1["sha256"] == sha(b"v1 bytes")

def test_store_new_version_no_wayback_key_when_url_none(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "wayback_save", _boom)
    store = cap.Store(tmp_path / "data")
    _, m = _mint(store, wayback_url=None)
    assert "wayback" not in m

def test_store_new_version_no_ots_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "ots_stamp", _boom)
    store = cap.Store(tmp_path / "data")
    rel, m = _mint(store, do_ots=False)
    assert "ots" not in m
    assert not list((store.root / rel).glob("*.ots"))

def test_store_new_version_ots_meta_stored_without_proof_file(tmp_path, monkeypatch):
    # all calendars failed: outcome dict is evidence, but no proof file exists
    ots_meta = {"ok": False, "calendars": [], "errors": ["e"], "at": "t"}
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (None, ots_meta))
    store = cap.Store(tmp_path / "data")
    rel, m = _mint(store, do_ots=True)
    assert m["ots"] == ots_meta
    assert not list((store.root / rel).glob("*.ots"))

def test_store_new_version_text_none(tmp_path):
    store = cap.Store(tmp_path / "data")
    rel, m = _mint(store, text=None)
    assert not (store.root / rel / "extracted.txt").exists()
    assert m["text_sha256"] is None
    assert store.last_text_sha("prov/model", TSLUG) is None

def test_store_new_version_managed_passthrough(tmp_path):
    store = cap.Store(tmp_path / "data")
    _mint(store, managed="meta_hub")
    assert store.state[f"prov/model::{TSLUG}"]["managed"] == "meta_hub"


# --- B. validators_for ---

V_URL = "https://ex.org/summary.pdf"
HAPPY_HTTP = {"status_code": 200, "etag": 'W/"abc"',
              "last_modified": "Mon, 17 Aug 2026 00:00:00 GMT"}


def _now_with_weekday(wd):
    # 2026-08-17 is a Monday (weekday 0)
    return datetime(2026, 8, 17 + wd, tzinfo=timezone.utc)


def _forced_weekday(url):
    return int(hashlib.sha256(url.encode()).hexdigest(), 16) % 7


OFF_DAY = _now_with_weekday((_forced_weekday(V_URL) + 1) % 7)
ON_DAY = _now_with_weekday(_forced_weekday(V_URL))


def _prime_prior(store, http=None, garbage=False):
    d = store.root / "captures" / "prov__model" / TSLUG / "20260815T000000Z"
    d.mkdir(parents=True)
    body = "{not json" if garbage else json.dumps({"http": http})
    (d / "manifest.json").write_text(body, encoding="utf-8")
    store.state[store.key("prov/model", TSLUG)] = {
        "last_capture": d.relative_to(store.root).as_posix()}


def _validators(store, now):
    return rc_mod.validators_for(store, store.root, "prov/model", TSLUG,
                                 V_URL, now=now)

def test_validators_none_without_prior_capture(tmp_path):
    store = cap.Store(tmp_path / "data")
    assert _validators(store, OFF_DAY) is None

def test_validators_none_when_prior_status_not_200(tmp_path):
    store = cap.Store(tmp_path / "data")
    _prime_prior(store, {"status_code": 304, "etag": 'W/"abc"'})
    assert _validators(store, OFF_DAY) is None

def test_validators_none_without_etag_or_last_modified(tmp_path):
    store = cap.Store(tmp_path / "data")
    _prime_prior(store, {"status_code": 200, "etag": None, "last_modified": None})
    assert _validators(store, OFF_DAY) is None

def test_validators_happy_path_returns_both(tmp_path):
    store = cap.Store(tmp_path / "data")
    _prime_prior(store, HAPPY_HTTP)
    assert _validators(store, OFF_DAY) == {
        "etag": 'W/"abc"', "last_modified": "Mon, 17 Aug 2026 00:00:00 GMT"}

def test_validators_forced_full_fetch_day_overrides_valid_validators(tmp_path):
    store = cap.Store(tmp_path / "data")
    _prime_prior(store, HAPPY_HTTP)
    assert _validators(store, ON_DAY) is None

def test_validators_unreadable_manifest_forces_full_fetch(tmp_path):
    store = cap.Store(tmp_path / "data")
    _prime_prior(store, garbage=True)
    assert _validators(store, OFF_DAY) is None


# --- C. run_capture.main() end-to-end (tmp registry + data root, mocked fetch) ---

def _write_registry(tmp_path, sources):
    p = tmp_path / "sources.json"
    p.write_text(json.dumps({"sources": sources}), encoding="utf-8", newline="\n")
    return p


def _src(sid, url, kind="provider-live", render=False):
    t = {"url": url, "kind": kind}
    if render:
        t["render"] = True
    return {"id": sid, "provider": "Prov", "model": "Model", "targets": [t]}


def _run(monkeypatch, registry, data_root):
    monkeypatch.setattr(sys, "argv", [
        "run_capture.py", "--registry", str(registry), "--data-root",
        str(data_root), "--no-wayback", "--no-ots", "--throttle", "0"])
    return rc_mod.main()


def _capture_dirs(data_root):
    return sorted(p.parent for p in
                  (Path(data_root) / "captures").rglob("manifest.json"))

def test_main_mints_version_then_dedupes_identical_run(tmp_path, monkeypatch):
    reg = _write_registry(tmp_path, [_src("prov/model", "https://ex.org/doc.txt")])
    data_root = tmp_path / "data"
    monkeypatch.setattr(cap, "fetch",
                        lambda url, **k: (b"summary body v1\n", _meta(url, "text/plain")))
    assert _run(monkeypatch, reg, data_root) == 0
    assert len(_capture_dirs(data_root)) == 1
    assert _run(monkeypatch, reg, data_root) == 0
    assert len(_capture_dirs(data_root)) == 1
    events = _events(data_root)
    assert [e["outcome"] for e in events] == ["new", "unchanged"]
    assert events[0]["extracted"] is True
    assert events[1]["sha256"] == sha(b"summary body v1\n")

def test_main_rendered_same_text_is_unchanged_content(tmp_path, monkeypatch):
    reg = _write_registry(tmp_path,
                          [_src("prov/model", "https://ex.org/page", render=True)])
    data_root = tmp_path / "data"
    # byte-different DOMs (whitespace churn), same canonical text
    doms = [b"<html><body><p>Summary text here</p></body></html>",
            b"<html>\n<body>\n  <p>Summary   text here</p>\n</body>\n</html>"]
    monkeypatch.setattr(cap, "fetch_rendered",
                        lambda url, **k: (doms.pop(0), _meta(url, "text/html")))
    monkeypatch.setattr(cap, "fetch", _boom)
    assert _run(monkeypatch, reg, data_root) == 0
    assert _run(monkeypatch, reg, data_root) == 0
    assert [e["outcome"] for e in _events(data_root)] == ["new", "unchanged-content"]
    assert len(_capture_dirs(data_root)) == 1
    assert _events(data_root)[1]["text_sha256"] == canon_sha("Summary text here")

def test_main_rendered_changed_text_mints_new_version(tmp_path, monkeypatch):
    reg = _write_registry(tmp_path,
                          [_src("prov/model", "https://ex.org/page", render=True)])
    data_root = tmp_path / "data"
    doms = [b"<html><body><p>Summary text here</p></body></html>",
            b"<html><body><p>Summary text revised</p></body></html>"]
    monkeypatch.setattr(cap, "fetch_rendered",
                        lambda url, **k: (doms.pop(0), _meta(url, "text/html")))
    assert _run(monkeypatch, reg, data_root) == 0
    assert _run(monkeypatch, reg, data_root) == 0
    assert [e["outcome"] for e in _events(data_root)] == ["new", "new"]
    assert len(_capture_dirs(data_root)) == 2

def test_main_zip_text_sha_matches_c5_zip_content_key(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("z-member.txt", b"zzz")
        zf.writestr("a-member.txt", b"aaa")
    zip_bytes = buf.getvalue()
    reg = _write_registry(tmp_path, [_src("prov/model", "https://ex.org/bundle")])
    data_root = tmp_path / "data"
    monkeypatch.setattr(cap, "fetch",
                        lambda url, **k: (zip_bytes, _meta(url, "application/zip")))
    assert _run(monkeypatch, reg, data_root) == 0
    (d,) = _capture_dirs(data_root)
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    assert m["stored_as"] == "raw.zip"
    # regression: zips must be keyed by inner-member hashes, not canonical text
    assert m["text_sha256"] == cap.zip_content_key(m["extraction_notes"])
    # verify_corpus C5 recomputes this exact formula over the stored manifest
    pairs = sorted((n["inner_file"], n["inner_sha256"])
                   for n in m["extraction_notes"]
                   if isinstance(n, dict) and "inner_sha256" in n)
    assert m["text_sha256"] == hashlib.sha256(
        json.dumps(pairs, ensure_ascii=False).encode("utf-8")).hexdigest()

def test_main_fetch_error_logs_event_and_returns_1(tmp_path, monkeypatch):
    reg = _write_registry(tmp_path, [_src("prov/model", "https://ex.org/doc.txt")])
    data_root = tmp_path / "data"
    def _raise(url, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(cap, "fetch", _raise)
    assert _run(monkeypatch, reg, data_root) == 1
    (e,) = _events(data_root)
    assert e["outcome"] == "error" and "boom" in e["error"]
    assert _capture_dirs(data_root) == []

def test_main_304_asserted_unchanged_without_new_dir(tmp_path, monkeypatch):
    reg = _write_registry(tmp_path, [_src("prov/model", "https://ex.org/doc.txt")])
    data_root = tmp_path / "data"
    responses = [(b"summary body v1\n", _meta("https://ex.org/doc.txt", "text/plain")),
                 (None, _meta("https://ex.org/doc.txt", "text/plain", status=304))]
    monkeypatch.setattr(cap, "fetch", lambda url, **k: responses.pop(0))
    assert _run(monkeypatch, reg, data_root) == 0
    assert _run(monkeypatch, reg, data_root) == 0
    events = _events(data_root)
    assert [e["outcome"] for e in events] == ["new", "unchanged"]
    assert events[1]["not_modified"] is True
    assert events[1]["sha256"] == sha(b"summary body v1\n")
    assert len(_capture_dirs(data_root)) == 1

def test_main_fetch_cache_dedupes_shared_url(tmp_path, monkeypatch):
    shared = "https://ex.org/portal.txt"
    reg = _write_registry(tmp_path, [_src("prov/model", shared),
                                     _src("prov/model2", shared)])
    data_root = tmp_path / "data"
    calls = []
    def fake_fetch(url, **k):
        calls.append(url)
        return b"shared portal body\n", _meta(url, "text/plain")
    monkeypatch.setattr(cap, "fetch", fake_fetch)
    assert _run(monkeypatch, reg, data_root) == 0
    assert calls == [shared]
    assert len(_capture_dirs(data_root)) == 2
