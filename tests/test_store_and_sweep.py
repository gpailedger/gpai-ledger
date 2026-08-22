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




# --- D. absence claims: re-check, independent witness, confirmed / unconfirmed /
#        contradicted, consecutive days, shared URLs (docs/runbooks.md "Absence claims")

import pytest
from datetime import date, timedelta


def _run_wb(monkeypatch, registry, data_root):
    """Like _run but WITHOUT --no-wayback so the witness path is reachable;
    the Wayback save itself is stubbed."""
    monkeypatch.setattr(cap, "wayback_save",
                        lambda url, **k: {"ok": True, "snapshot": "s", "at": "t"})
    monkeypatch.setattr(sys, "argv", [
        "run_capture.py", "--registry", str(registry), "--data-root",
        str(data_root), "--no-ots", "--throttle", "0"])
    return rc_mod.main()


def _err(status=404, headers=None):
    return cap.PermanentFetchError(f"HTTP {status} for u", status_code=status,
                                   headers=headers if headers is not None
                                   else {"Server": "AzureFrontDoor"})


def _seq(responses):
    """fetch stub: pops (body, meta) tuples or raises queued exceptions."""
    def fake(url, **k):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    return fake


def _w(saw, status=None):
    return lambda url: {"witness": "wayback", "saw": saw, "status": status,
                        "snapshot": "s", "reason": None, "at": "t"}


BODY = (b"summary body v1\n", _meta("https://ex.org/doc.txt", "text/plain"))
TARGET = cap.target_slug("provider-live", "https://ex.org/doc.txt")


def _seed(data_root, day, absence="unconfirmed", outcome="error"):
    """Append a prior event for prov/model dated `day` (YYYY-MM-DD)."""
    ev = {"ts": f"{day}T06:00:00Z", "source": "prov/model", "target": TARGET,
          "url": "https://ex.org/doc.txt", "kind": "provider-live", "outcome": outcome}
    if outcome == "error":
        ev["error"] = "HTTP 404"
        if absence:
            ev["absence"] = absence
    with open(Path(data_root) / "events.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(ev) + "\n")


def _day1(tmp_path, monkeypatch, sources=None):
    reg = _write_registry(tmp_path, sources or [_src("prov/model", "https://ex.org/doc.txt")])
    data_root = tmp_path / "data"
    monkeypatch.setattr(rc_mod, "RECHECK_DELAY", 0)
    monkeypatch.setattr(cap, "fetch", _seq([BODY] * (len(sources) if sources else 1)))
    assert _run_wb(monkeypatch, reg, data_root) == 0
    return reg, data_root


def _today():
    return cap.utc_now()[:10]


def _days_ago(n):
    return (date.fromisoformat(_today()) - timedelta(days=n)).isoformat()


def test_recheck_recovers_transient_404(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    monkeypatch.setattr(cap, "fetch", _seq([_err(404), BODY]))
    monkeypatch.setattr(cap, "wayback_witness", _boom)
    assert _run_wb(monkeypatch, reg, data_root) == 0
    outcomes = [e["outcome"] for e in _events(data_root)]
    assert outcomes == ["new", "recheck-recovered", "unchanged"]
    rec = _events(data_root)[1]
    assert rec["observations"][0]["status_code"] == 404
    assert rec["observations"][0]["headers"] == {"Server": "AzureFrontDoor"}


@pytest.mark.parametrize("status", [404, 410])
def test_witness_absent_confirms_for_each_absence_status(tmp_path, monkeypatch, status):
    reg, data_root = _day1(tmp_path, monkeypatch)
    monkeypatch.setattr(cap, "fetch", _seq([_err(status), _err(status)]))
    monkeypatch.setattr(cap, "wayback_witness", _w("absent", status))
    assert _run_wb(monkeypatch, reg, data_root) == 1
    e = _events(data_root)[-1]
    assert e["absence"] == "confirmed" and e["confirmed_by"] == ["witness"]
    assert e["status_code"] == status and e["consecutive_absent_days"] == 1
    assert [o["status_code"] for o in e["observations"]] == [status, status]
    assert e["vantage"] == cap.VANTAGE


def test_witness_live_contradicts_and_stays_green(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("live", 200))
    assert _run_wb(monkeypatch, reg, data_root) == 0
    e = _events(data_root)[-1]
    assert e["absence"] == "contradicted" and e["confirmed_by"] == []


def test_live_witness_vetoes_the_day_route(tmp_path, monkeypatch):
    # the 22 Aug MAI pattern persisting into day 2: runner 404, everyone else 200
    reg, data_root = _day1(tmp_path, monkeypatch)
    _seed(data_root, _days_ago(1))
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("live", 200))
    assert _run_wb(monkeypatch, reg, data_root) == 0
    assert _events(data_root)[-1]["absence"] == "contradicted"


def test_persistent_contradiction_becomes_a_vantage_alert(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    _seed(data_root, _days_ago(2), absence="contradicted")
    _seed(data_root, _days_ago(1), absence="contradicted")
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("live", 200))
    assert _run_wb(monkeypatch, reg, data_root) == 1     # red, but NOT an absence claim
    e = _events(data_root)[-1]
    assert e["absence"] == "contradicted" and e["consecutive_contradicted_days"] == 3
    assert "absent_on" not in e and "consecutive_absent_days" not in e


def test_inconclusive_witness_first_day_is_unconfirmed(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("inconclusive"))
    assert _run_wb(monkeypatch, reg, data_root) == 0
    e = _events(data_root)[-1]
    assert e["absence"] == "unconfirmed" and e["absent_on"] == [_today()]


def test_second_day_within_window_confirms(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    _seed(data_root, _days_ago(1))
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("inconclusive"))
    assert _run_wb(monkeypatch, reg, data_root) == 1
    e = _events(data_root)[-1]
    assert e["absence"] == "confirmed" and e["confirmed_by"] == ["consecutive-days"]
    assert e["absent_on"] == [_days_ago(1), _today()]


def test_missed_sweep_does_not_break_the_day_route(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    _seed(data_root, _days_ago(3))      # one missed day in between is tolerated
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("inconclusive"))
    assert _run_wb(monkeypatch, reg, data_root) == 1


def test_old_absence_outside_window_does_not_confirm(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    _seed(data_root, "2000-01-01")      # no success since, but far too old
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("inconclusive"))
    assert _run_wb(monkeypatch, reg, data_root) == 0
    e = _events(data_root)[-1]
    assert e["absence"] == "unconfirmed" and e["consecutive_absent_days"] == 2


def test_same_day_rerun_counts_once(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    _seed(data_root, _today())
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("inconclusive"))
    assert _run_wb(monkeypatch, reg, data_root) == 0
    assert _events(data_root)[-1]["consecutive_absent_days"] == 1


def test_success_between_absences_resets_the_streak(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    _seed(data_root, _days_ago(1))
    monkeypatch.setattr(cap, "fetch", _seq([BODY]))
    assert _run_wb(monkeypatch, reg, data_root) == 0          # a success since
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("inconclusive"))
    assert _run_wb(monkeypatch, reg, data_root) == 0
    assert _events(data_root)[-1]["consecutive_absent_days"] == 1


def test_plain_error_between_absences_neither_resets_nor_counts(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    _seed(data_root, _days_ago(2))
    _seed(data_root, _days_ago(1), absence=None)              # plain 503-style error
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("inconclusive"))
    assert _run_wb(monkeypatch, reg, data_root) == 1
    assert _events(data_root)[-1]["absent_on"] == [_days_ago(2), _today()]


def test_recheck_failing_for_non_absence_reason_is_a_plain_error(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    monkeypatch.setattr(cap, "fetch",
                        _seq([_err(404, {"X-Azure-Ref": "first"}), RuntimeError("HTTP 503")]))
    monkeypatch.setattr(cap, "wayback_witness", _boom)       # never consulted
    assert _run_wb(monkeypatch, reg, data_root) == 1          # red, like any failure
    e = _events(data_root)[-1]
    assert "absence" not in e and "HTTP 503" in e["error"]
    assert e["status_code"] is None and e["headers"] == {}   # never backfilled
    assert e["observations"][0]["headers"] == {"X-Azure-Ref": "first"}
    assert e["observations"][1]["status_code"] is None


def test_both_observations_keep_their_own_headers(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    monkeypatch.setattr(cap, "fetch", _seq([_err(404, {"X-Azure-Ref": "first"}),
                                            _err(404, {"X-Azure-Ref": "second"})]))
    monkeypatch.setattr(cap, "wayback_witness", _w("inconclusive"))
    _run_wb(monkeypatch, reg, data_root)
    e = _events(data_root)[-1]
    assert [o["headers"]["X-Azure-Ref"] for o in e["observations"]] == ["first", "second"]
    assert e["headers"] == {"X-Azure-Ref": "second"}


def test_never_captured_target_is_a_plain_error_without_witness(tmp_path, monkeypatch):
    reg = _write_registry(tmp_path, [_src("prov/model", "https://ex.org/doc.txt")])
    data_root = tmp_path / "data"
    monkeypatch.setattr(rc_mod, "RECHECK_DELAY", 0)
    monkeypatch.setattr(cap, "wayback_witness", _boom)
    monkeypatch.setattr(cap, "fetch", _seq([_err()]))
    assert _run_wb(monkeypatch, reg, data_root) == 1
    (e,) = _events(data_root)
    assert "absence" not in e and e["status_code"] == 404


def test_no_wayback_skips_witness_with_reason(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _boom)
    assert _run(monkeypatch, reg, data_root) == 0            # --no-wayback
    e = _events(data_root)[-1]
    assert e["witness"] is None and e["witness_skipped"] == "no-wayback"
    assert e["absence"] == "unconfirmed"


def test_witness_budget_boundary(tmp_path, monkeypatch):
    sources = [_src("prov/a", "https://ex.org/a.txt"), _src("prov/b", "https://ex.org/b.txt")]
    reg, data_root = _day1(tmp_path, monkeypatch, sources)
    monkeypatch.setattr(rc_mod, "MAX_WITNESSES_PER_RUN", 1)
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err(), _err(), _err()]))
    calls = []

    def witness(url):
        calls.append(url)
        return _w("inconclusive")(url)
    monkeypatch.setattr(cap, "wayback_witness", witness)
    assert _run_wb(monkeypatch, reg, data_root) == 0
    evs = [e for e in _events(data_root) if e["outcome"] == "error"]
    assert calls == ["https://ex.org/a.txt"]
    assert evs[0]["witness_skipped"] is None and evs[1]["witness_skipped"] == "budget"


def test_rate_limited_witness_stops_further_witnesses(tmp_path, monkeypatch):
    sources = [_src("prov/a", "https://ex.org/a.txt"), _src("prov/b", "https://ex.org/b.txt")]
    reg, data_root = _day1(tmp_path, monkeypatch, sources)
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err(), _err(), _err()]))
    calls = []

    def witness(url):
        calls.append(url)
        return {"witness": "wayback", "saw": "inconclusive", "status": None,
                "snapshot": None, "reason": "rate-limited", "at": "t"}
    monkeypatch.setattr(cap, "wayback_witness", witness)
    assert _run_wb(monkeypatch, reg, data_root) == 0
    assert calls == ["https://ex.org/a.txt"]
    evs = [e for e in _events(data_root) if e["outcome"] == "error"]
    assert evs[0]["witness"]["reason"] == "rate-limited" and evs[0]["witness_skipped"] is None
    assert evs[1]["witness"] is None and evs[1]["witness_skipped"] == "rate-limited"


def test_shared_url_siblings_reuse_one_recheck_and_witness(tmp_path, monkeypatch):
    shared = "https://ex.org/portal.txt"
    sources = [_src("prov/a", shared), _src("prov/b", shared)]
    reg = _write_registry(tmp_path, sources)
    data_root = tmp_path / "data"
    monkeypatch.setattr(rc_mod, "RECHECK_DELAY", 0)
    monkeypatch.setattr(cap, "fetch", _seq([(b"shared\n", _meta(shared, "text/plain"))]))
    assert _run_wb(monkeypatch, reg, data_root) == 0
    fetches, calls = [], []

    def fetch(url, **k):
        fetches.append(url)
        raise _err()
    monkeypatch.setattr(cap, "fetch", fetch)

    def witness(url):
        calls.append(url)
        return _w("inconclusive")(url)
    monkeypatch.setattr(cap, "wayback_witness", witness)
    assert _run_wb(monkeypatch, reg, data_root) == 0
    evs = [e for e in _events(data_root) if e["outcome"] == "error"]
    assert len(evs) == 2 and calls == [shared]               # one witness, two events
    assert len(fetches) == 3                                  # a: first + re-check; b: first only
    assert "shared_from" not in evs[0] and evs[1]["shared_from"] == "prov/a"
    assert evs[0]["witness"] == evs[1]["witness"]


def test_rendered_404_with_prior_capture_enters_absence_path(tmp_path, monkeypatch):
    reg = _write_registry(tmp_path,
                          [_src("prov/model", "https://ex.org/page", render=True)])
    data_root = tmp_path / "data"
    monkeypatch.setattr(rc_mod, "RECHECK_DELAY", 0)
    monkeypatch.setattr(cap, "fetch_rendered",
                        lambda url, **k: (b"<html>hello world</html>",
                                          dict(_meta(url, "text/html"), rendered=True)))
    monkeypatch.setattr(cap, "fetch", _boom)
    assert _run_wb(monkeypatch, reg, data_root) == 0
    renders = []

    def render(url, **k):
        renders.append(url)
        raise _err(404, {"Server": "x"})
    monkeypatch.setattr(cap, "fetch_rendered", render)
    monkeypatch.setattr(cap, "wayback_witness", _w("absent", 404))
    assert _run_wb(monkeypatch, reg, data_root) == 1
    e = _events(data_root)[-1]
    assert e["absence"] == "confirmed" and len(renders) == 2   # re-check used the renderer


def test_rendered_failure_without_status_is_a_plain_error(tmp_path, monkeypatch):
    reg = _write_registry(tmp_path,
                          [_src("prov/model", "https://ex.org/page", render=True)])
    data_root = tmp_path / "data"

    def _raise(url, **k):
        raise RuntimeError("render failed")
    monkeypatch.setattr(cap, "fetch_rendered", _raise)
    monkeypatch.setattr(cap, "fetch", _boom)
    assert _run(monkeypatch, reg, data_root) == 1
    (e,) = _events(data_root)
    assert e["outcome"] == "error" and "absence" not in e


def test_live_witness_yesterday_restarts_streak_and_vetoes_the_day_route(tmp_path, monkeypatch):
    # the MAI pattern across days: runner 404 daily; witness inconclusive (d-2),
    # LIVE (d-1), inconclusive (today) — the day route must not confirm over a
    # sighting that proved the document existed yesterday
    reg, data_root = _day1(tmp_path, monkeypatch)
    _seed(data_root, _days_ago(2))
    _seed(data_root, _days_ago(1), absence="contradicted")
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("inconclusive"))
    assert _run_wb(monkeypatch, reg, data_root) == 0
    e = _events(data_root)[-1]
    assert e["absence"] == "unconfirmed" and e["confirmed_by"] == []
    assert e["absent_on"] == [_today()] and e["consecutive_absent_days"] == 1
    assert e["last_live_witness"] == _days_ago(1)


def test_day_route_resumes_once_the_live_sighting_ages_out(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    _seed(data_root, _days_ago(4), absence="contradicted")   # outside the window
    _seed(data_root, _days_ago(2))
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("inconclusive"))
    assert _run_wb(monkeypatch, reg, data_root) == 1
    e = _events(data_root)[-1]
    assert e["confirmed_by"] == ["consecutive-days"]
    assert e["absent_on"] == [_days_ago(2), _today()] and "last_live_witness" not in e


def test_fresh_absent_witness_confirms_despite_a_live_witness_yesterday(tmp_path, monkeypatch):
    # the witness route is the strong one: a document removed since yesterday
    reg, data_root = _day1(tmp_path, monkeypatch)
    _seed(data_root, _days_ago(1), absence="contradicted")
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("absent", 404))
    assert _run_wb(monkeypatch, reg, data_root) == 1
    e = _events(data_root)[-1]
    assert e["absence"] == "confirmed" and e["confirmed_by"] == ["witness"]


def test_vantage_alert_survives_an_inconclusive_day_in_between(tmp_path, monkeypatch):
    reg, data_root = _day1(tmp_path, monkeypatch)
    _seed(data_root, _days_ago(3), absence="contradicted")
    _seed(data_root, _days_ago(2), absence="contradicted")
    _seed(data_root, _days_ago(1))                              # inconclusive that day
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("live", 200))
    assert _run_wb(monkeypatch, reg, data_root) == 1           # red as a vantage problem
    e = _events(data_root)[-1]
    assert e["absence"] == "contradicted" and e["consecutive_contradicted_days"] == 3


def test_sibling_live_fetch_supersedes_an_earlier_absence_claim_in_the_run(tmp_path, monkeypatch):
    # registry sources share portal URLs; if the first sibling's fetch and re-check
    # 404 but a later sibling fetches the URL live minutes later, the run itself
    # proves the 404 was transient: the claim is superseded and the gate corrected
    shared = "https://ex.org/portal.txt"
    body = (b"shared\n", _meta(shared, "text/plain"))
    tslug = cap.target_slug("provider-live", shared)
    reg = _write_registry(tmp_path, [_src("prov/a", shared), _src("prov/b", shared)])
    data_root = tmp_path / "data"
    monkeypatch.setattr(rc_mod, "RECHECK_DELAY", 0)
    monkeypatch.setattr(cap, "fetch", _seq([body]))
    assert _run_wb(monkeypatch, reg, data_root) == 0
    with open(data_root / "events.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"ts": f"{_days_ago(1)}T06:00:00Z", "source": "prov/a",
                             "target": tslug, "url": shared, "kind": "provider-live",
                             "outcome": "error", "error": "HTTP 404",
                             "absence": "unconfirmed"}) + "\n")
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err(), body]))   # a: 404, 404; b: 200
    monkeypatch.setattr(cap, "wayback_witness", _w("inconclusive"))
    assert _run_wb(monkeypatch, reg, data_root) == 0
    claim, recovery, sibling = _events(data_root)[-3:]
    assert claim["source"] == "prov/a" and claim["absence"] == "confirmed"
    assert recovery["source"] == "prov/a" and recovery["outcome"] == "recheck-recovered"
    assert recovery["recovered_by"] == "prov/b" and recovery["prior_absence"] == "confirmed"
    assert [o["status_code"] for o in recovery["observations"]] == [404, 404]
    assert sibling["source"] == "prov/b" and sibling["outcome"] == "unchanged"
    assert ("prov/a", tslug) not in rc_mod.absence_streaks(data_root / "events.jsonl")


def test_absence_event_dates_come_from_the_event_clock(tmp_path, monkeypatch):
    # a run crossing UTC midnight must not stamp today's event with yesterday's dates
    reg, data_root = _day1(tmp_path, monkeypatch)
    clock = iter(["2026-08-22T23:59:59Z"] + ["2026-08-23T00:00:05Z"] * 50)
    monkeypatch.setattr(cap, "utc_now", lambda: next(clock))
    monkeypatch.setattr(cap, "fetch", _seq([_err(), _err()]))
    monkeypatch.setattr(cap, "wayback_witness", _w("inconclusive"))
    _run_wb(monkeypatch, reg, data_root)
    e = _events(data_root)[-1]
    assert e["ts"] == "2026-08-23T00:00:05Z" and e["absent_on"] == ["2026-08-23"]


def test_absence_streaks_reader(tmp_path):
    p = tmp_path / "events.jsonl"
    rows = [
        {"source": "s", "target": "t", "outcome": "error", "absence": "unconfirmed", "ts": "2026-08-20T06:00:00Z"},
        {"source": "s", "target": "t", "outcome": "error", "ts": "2026-08-21T06:00:00Z"},   # plain
        {"source": "s", "target": "t", "outcome": "error", "absence": "contradicted", "ts": "2026-08-22T06:00:00Z"},
        {"source": "s", "target": "t", "outcome": "error", "absence": "unconfirmed", "ts": "2026-08-23T06:00:00Z"},
        {"source": "s", "target": "t", "outcome": "error", "absence": "unconfirmed", "ts": "2026-8-24T06:00:00Z"},  # damaged
        {"source": "s", "target": "t", "outcome": "error", "absence": "unconfirmed", "ts": 20260825},           # damaged
        {"source": "s", "target": "u", "outcome": "error", "absence": "confirmed", "ts": "2026-08-22T06:00:00Z"},
        {"source": "s", "target": "u", "outcome": "unchanged", "ts": "2026-08-23T06:00:00Z"},
        {"source": "s", "target": "v", "outcome": "error", "absence": "confirmed", "ts": "2026-08-22T06:00:00Z"},
        {"source": "s", "target": "v", "outcome": "recheck-recovered", "ts": "2026-08-23T06:00:00Z"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    s = rc_mod.absence_streaks(p)
    # a live-witness day restarts the absence streak; damaged timestamps are skipped
    assert s[("s", "t")] == {"absent_on": {"2026-08-23"}, "contradicted_on": {"2026-08-22"}}
    assert ("s", "u") not in s and ("s", "v") not in s


def test_recheck_delay_env_parse_is_guarded(monkeypatch):
    monkeypatch.setenv("GPAI_RECHECK_DELAY", "not-a-number")
    assert rc_mod._delay_from_env() == 45.0
    monkeypatch.setenv("GPAI_RECHECK_DELAY", "9999")
    assert rc_mod._delay_from_env() == 300.0
    monkeypatch.setenv("GPAI_RECHECK_DELAY", "0")
    assert rc_mod._delay_from_env() == 0.0
