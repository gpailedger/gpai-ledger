"""Tests for crawler/retry_wayback.py and crawler/upgrade_ots.py — the two
maintenance passes over stored provenance (Wayback save retries / snapshot
re-verification, and OpenTimestamps restamp + bitcoin-anchor upgrades).

Both scripts walk the module-level DATA root, so every test patches DATA to a
fixture corpus. All network seams (cap.wayback_save, cap.ots_stamp, and each
script's own `requests` binding) are mocked; time.sleep throttling is stubbed.
"""
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

from conftest import load_module, sha

ROOT = Path(__file__).resolve().parent.parent
RW = load_module(str(ROOT / "crawler" / "retry_wayback.py"), "retry_wayback_mod")
UO = load_module(str(ROOT / "crawler" / "upgrade_ots.py"), "upgrade_ots_mod")

SNAP = "https://web.archive.org/web/20260815060000/https://example.org/doc.pdf"


class FakeRequests:
    """Stands in for the `requests` binding inside a script's namespace."""

    def __init__(self, status=200, content=b""):
        self.calls = []
        self.status = status
        self.content = content

    def head(self, url, **kw):
        self.calls.append(url)
        return types.SimpleNamespace(status_code=self.status)

    def get(self, url, **kw):
        self.calls.append(url)
        return types.SimpleNamespace(status_code=self.status,
                                     content=self.content)


def wayback_save_recorder(monkeypatch, result=None):
    calls = []

    def fake(url, timeout=120):
        calls.append(url)
        return dict(result)

    monkeypatch.setattr(RW.cap, "wayback_save", fake)
    return calls


def run_retry(monkeypatch, root, *extra_argv):
    monkeypatch.setattr(RW, "DATA", root)
    # a clock that never advances: the wall-clock budget is exercised by its own test
    monkeypatch.setattr(RW, "time", types.SimpleNamespace(sleep=lambda s: None,
                                                          monotonic=lambda: 0.0))
    monkeypatch.setattr(sys, "argv", ["retry_wayback.py", *extra_argv])
    return RW.main()


def manifest_of(d):
    return json.loads((d / "manifest.json").read_text(encoding="utf-8"))


# --- retry_wayback default mode ---

def test_signed_url_marked_gave_up_without_retry(corpus, monkeypatch):
    d, _ = corpus.add_capture(
        url="https://cdn.example.org/doc.pdf?X-Amz-Signature=deadbeef",
        wayback={"ok": False, "status_code": 403})
    root = corpus.finish()
    calls = wayback_save_recorder(monkeypatch, {"ok": True})
    assert run_retry(monkeypatch, root) == 0
    m = manifest_of(d)
    assert m["wayback"]["gave_up"].startswith("signed URL")
    assert calls == []


def test_exhausted_attempts_marked_gave_up(corpus, monkeypatch):
    d, _ = corpus.add_capture(
        wayback={"ok": False, "status_code": 503},
        extra_manifest={"wayback_attempts": [{"ok": False, "status_code": 523}] * RW.MAX_ATTEMPTS})
    root = corpus.finish()
    calls = wayback_save_recorder(monkeypatch, {"ok": True})
    run_retry(monkeypatch, root)
    m = manifest_of(d)
    assert m["wayback"]["gave_up"] == (f"exhausted {RW.MAX_ATTEMPTS} answered attempts "
                                       f"({RW.MAX_ATTEMPTS + 1} in total)")
    assert calls == []


def test_retryable_failure_retried_and_prior_attempt_archived(corpus, monkeypatch):
    prior = {"ok": False, "status_code": 503}
    d, _ = corpus.add_capture(wayback=dict(prior))
    root = corpus.finish()
    result = {"ok": True, "status_code": 200, "snapshot": SNAP}
    calls = wayback_save_recorder(monkeypatch, result)
    run_retry(monkeypatch, root)
    m = manifest_of(d)
    assert calls == ["https://example.org/doc.pdf"]
    assert m["wayback"] == result
    assert m["wayback_attempts"] == [prior]


def test_ok_wayback_left_untouched(corpus, monkeypatch):
    d, _ = corpus.add_capture(wayback={"ok": True, "snapshot": SNAP})
    root = corpus.finish()
    before = (d / "manifest.json").read_bytes()
    calls = wayback_save_recorder(monkeypatch, {"ok": True})
    run_retry(monkeypatch, root)
    assert calls == []
    assert (d / "manifest.json").read_bytes() == before


# --- retry_wayback --verify ---

def test_verify_live_snapshot_gains_snapshot_verified(corpus, monkeypatch):
    d, _ = corpus.add_capture(wayback={"ok": True, "snapshot": SNAP})
    root = corpus.finish()
    fake = FakeRequests(status=200)
    monkeypatch.setattr(RW, "requests", fake)
    assert run_retry(monkeypatch, root, "--verify") == 0
    m = manifest_of(d)
    assert fake.calls == [SNAP]
    assert m["wayback"]["ok"] is True
    assert m["wayback"]["snapshot_verified"]


def test_verify_dead_snapshot_needs_two_dated_404s(corpus, monkeypatch):
    # a single definitive 404 only marks the snapshot suspect; the demotion —
    # with the prior block archived — happens on the SECOND dated 404. This is
    # the regression test for the 20 Aug 2026 incident where throttled
    # connections were mass-recorded as dead snapshots.
    old_wb = {"ok": True, "snapshot": SNAP}
    d, _ = corpus.add_capture(wayback=dict(old_wb))
    root = corpus.finish()
    monkeypatch.setattr(RW, "requests", FakeRequests(status=404))
    run_retry(monkeypatch, root, "--verify")
    m = manifest_of(d)
    assert m["wayback"]["ok"] is True
    assert m["wayback"]["suspect_dead_since"]
    run_retry(monkeypatch, root, "--verify")                   # same UTC date: no 2nd strike
    assert manifest_of(d)["wayback"]["ok"] is True
    monkeypatch.setattr(RW.cap, "utc_now", lambda: "2099-01-01T00:00:00Z")
    run_retry(monkeypatch, root, "--verify")
    m = manifest_of(d)
    assert m["wayback"]["ok"] is False
    assert "on two dated checks" in m["wayback"]["error"]
    assert m["wayback"]["snapshot"] == SNAP
    assert m["wayback_attempts"][0]["snapshot"] == SNAP


class _RedirectingRequests(FakeRequests):
    """A 200 served after Wayback redirected to ANOTHER capture timestamp."""

    def __init__(self, final_url):
        super().__init__(status=200)
        self.final_url = final_url

    def get(self, url, **kw):
        self.calls.append(url)
        return types.SimpleNamespace(status_code=200, content=b"", url=self.final_url)


def test_verify_200_from_a_different_capture_timestamp_is_a_strike(corpus, monkeypatch):
    # Wayback redirects a missing timestamp to the NEAREST capture: a 200 there
    # says nothing about the recorded snapshot
    d, _ = corpus.add_capture(wayback={"ok": True, "snapshot": SNAP})
    root = corpus.finish()
    monkeypatch.setattr(RW, "requests", _RedirectingRequests(
        "https://web.archive.org/web/20240101000000/https://example.org/doc.pdf"))
    run_retry(monkeypatch, root, "--verify")
    m = manifest_of(d)
    assert "snapshot_verified" not in m["wayback"]
    assert m["wayback"]["suspect_dead_since"] and "different capture" in m["wayback"]["suspect_reason"]


def test_verify_200_at_the_same_timestamp_via_url_canonicalisation_verifies(corpus, monkeypatch):
    d, _ = corpus.add_capture(wayback={"ok": True, "snapshot": SNAP})
    root = corpus.finish()
    monkeypatch.setattr(RW, "requests", _RedirectingRequests(SNAP + "/"))
    run_retry(monkeypatch, root, "--verify")
    assert manifest_of(d)["wayback"]["snapshot_verified"]


def test_verify_exceptions_and_throttling_give_no_verdict(corpus, monkeypatch):
    # connection drops / 429 / 5xx are archive.org throttling, not evidence of
    # death: the manifest must stay untouched either way
    d, _ = corpus.add_capture(wayback={"ok": True, "snapshot": SNAP})
    root = corpus.finish()
    class Boom:
        def get(self, *a, **k):
            raise ConnectionError("throttled")
    monkeypatch.setattr(RW, "requests", Boom())
    monkeypatch.setattr(RW.time, "sleep", lambda s: None)
    run_retry(monkeypatch, root, "--verify")
    m = manifest_of(d)
    assert m["wayback"]["ok"] is True and "suspect_dead_since" not in m["wayback"]
    monkeypatch.setattr(RW, "requests", FakeRequests(status=503))
    run_retry(monkeypatch, root, "--verify")
    m = manifest_of(d)
    assert m["wayback"]["ok"] is True and "suspect_dead_since" not in m["wayback"]


def test_verify_skips_already_verified_snapshot(corpus, monkeypatch):
    d, _ = corpus.add_capture(wayback={
        "ok": True, "snapshot": SNAP,
        "snapshot_verified": "2026-08-16T00:00:00Z"})
    root = corpus.finish()
    before = (d / "manifest.json").read_bytes()
    fake = FakeRequests(status=200)
    monkeypatch.setattr(RW, "requests", fake)
    run_retry(monkeypatch, root, "--verify")
    assert fake.calls == []
    assert (d / "manifest.json").read_bytes() == before


# --- upgrade_ots: calendar allowlist ---

def test_allowed_calendar_accepts_https_allowlisted_hosts():
    for host in sorted(UO.ALLOWED_CALENDAR_HOSTS):
        assert UO._allowed_calendar(f"https://{host}")


def test_allowed_calendar_rejects_http_scheme():
    assert not UO._allowed_calendar(
        "http://alice.btc.calendar.opentimestamps.org")


def test_allowed_calendar_rejects_unknown_host():
    assert not UO._allowed_calendar("https://evil.example.com")


# --- upgrade_ots: restamp_missing ---

def test_restamp_stamps_capture_missing_proof(corpus, monkeypatch):
    d, m0 = corpus.add_capture(
        with_ots=False, extra_manifest={"ots": {"ok": False,
                                                "error": "calendars down"}})
    root = corpus.finish()
    seen = []

    def fake_stamp(digest):
        seen.append(digest)
        return b"OTSPROOFBYTES", {"ok": True, "calendars": [UO.cap.OTS_CALENDARS[0]]}

    monkeypatch.setattr(UO.cap, "ots_stamp", fake_stamp)
    monkeypatch.setattr(UO, "DATA", root)
    assert UO.restamp_missing() == 1
    m = manifest_of(d)
    assert seen == [bytes.fromhex(m0["sha256"])]
    assert (d / "raw.pdf.ots").read_bytes() == b"OTSPROOFBYTES"
    assert m["ots"]["ok"] is True
    assert m["ots"]["restamped_at"]
    assert "stamp submitted after capture" in m["ots"]["note"]


def test_restamp_leaves_ok_capture_untouched(corpus, monkeypatch):
    d, _ = corpus.add_capture()  # ots ok, .ots present
    root = corpus.finish()
    before = (d / "manifest.json").read_bytes()
    monkeypatch.setattr(UO.cap, "ots_stamp",
                        lambda digest: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(UO, "DATA", root)
    assert UO.restamp_missing() == 0
    assert (d / "manifest.json").read_bytes() == before


def test_restamp_leaves_existing_ots_file_untouched(corpus, monkeypatch):
    # submission recorded as failed, but a proof file exists on disk anyway
    d, _ = corpus.add_capture(with_ots=False,
                              extra_manifest={"ots": {"ok": False}})
    (d / "raw.pdf.ots").write_bytes(b"already here")
    root = corpus.finish()
    before = (d / "manifest.json").read_bytes()
    monkeypatch.setattr(UO.cap, "ots_stamp",
                        lambda digest: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(UO, "DATA", root)
    assert UO.restamp_missing() == 0
    assert (d / "raw.pdf.ots").read_bytes() == b"already here"
    assert (d / "manifest.json").read_bytes() == before


# --- upgrade_ots: main() proof-age gate and upgrade pass ---

def make_anchored_ots(digest_hex):
    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

    ts = Timestamp(bytes.fromhex(digest_hex))
    ts.attestations.add(BitcoinBlockHeaderAttestation(850000))
    dtf = DetachedTimestampFile(OpSHA256(), ts)
    ctx = BytesSerializationContext()
    dtf.serialize(ctx)
    return ctx.getbytes()


def run_upgrade(monkeypatch, root, fake_requests):
    monkeypatch.setattr(UO, "DATA", root)
    monkeypatch.setattr(UO, "requests", fake_requests)
    return UO.main()


def test_young_proof_skipped_before_any_calendar_contact(corpus, monkeypatch,
                                                         capsys):
    # capture dir named with the current time: younger than MIN_PROOF_AGE_HOURS
    now_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    corpus.add_capture(ts=now_ts)
    root = corpus.finish()
    fake = FakeRequests(status=200, content=b"x")
    assert run_upgrade(monkeypatch, root, fake) == 0
    assert fake.calls == []
    assert ("ots proofs: 0 already anchored, 0 upgraded now, "
            "1 still pending, 0 unreadable") in capsys.readouterr().out


def test_old_pending_proof_polled_and_stays_pending_on_404(corpus, monkeypatch,
                                                           capsys):
    # fixture default dir 20260815T060000Z is well past the age gate
    d, _ = corpus.add_capture()
    root = corpus.finish()
    before = (d / "raw.pdf.ots").read_bytes()
    fake = FakeRequests(status=404)
    assert run_upgrade(monkeypatch, root, fake) == 0
    assert len(fake.calls) == 1
    assert "/timestamp/" in fake.calls[0]
    assert ("ots proofs: 0 already anchored, 0 upgraded now, "
            "1 still pending, 0 unreadable") in capsys.readouterr().out
    assert (d / "raw.pdf.ots").read_bytes() == before


def test_anchored_proof_skipped_without_calendar_contact(corpus, monkeypatch,
                                                         capsys):
    d, _ = corpus.add_capture(raw=b"%PDF-1.4 anchored")
    anchored = make_anchored_ots(sha(b"%PDF-1.4 anchored"))
    (d / "raw.pdf.ots").write_bytes(anchored)
    root = corpus.finish()
    fake = FakeRequests(status=200, content=b"x")
    assert run_upgrade(monkeypatch, root, fake) == 0
    assert fake.calls == []
    assert ("ots proofs: 1 already anchored, 0 upgraded now, "
            "0 still pending, 0 unreadable") in capsys.readouterr().out
    assert (d / "raw.pdf.ots").read_bytes() == anchored


def test_transport_failures_do_not_count_toward_the_wayback_retry_budget():
    import retry_wayback as rw
    attempts = [{"ok": False, "status_code": None, "error": "ConnectionError", "at": "2026-08-2%dT07:00:00Z" % i}
                for i in range(1, 6)] + [{"ok": False, "status_code": 429, "at": "2026-08-27T07:00:00Z"}]
    assert rw.answered_attempts(attempts) == []
    assert len(rw.attempt_dates(attempts)) == 6
    answered = [{"ok": False, "status_code": 523, "at": "2026-08-2%dT07:00:00Z" % i} for i in range(1, 6)]
    assert len(rw.answered_attempts(answered)) == 5


def test_retry_gives_up_only_after_answered_attempts_or_many_dates(corpus, monkeypatch):
    import retry_wayback as rw
    corpus.add_capture(ts="20260811T060000Z", raw=b"%PDF-1.4 a", text="t",
                       wayback={"ok": False, "status_code": None, "error": "ReadTimeout",
                                "at": "2026-08-11T06:00:10Z"})
    root = corpus.finish()
    mp = next(root.rglob("manifest.json"))
    m = json.loads(mp.read_text(encoding="utf-8"))
    m["wayback_attempts"] = [{"ok": False, "status_code": None, "error": "ConnectionError",
                              "at": "2026-08-1%dT06:00:10Z" % i} for i in range(2, 7)]
    mp.write_text(json.dumps(m), encoding="utf-8")
    monkeypatch.setattr(rw, "DATA", root)
    saves = []
    monkeypatch.setattr(rw.cap, "wayback_save", lambda url, **k: saves.append(url) or {"ok": True, "status_code": 200, "snapshot": "s", "at": "2026-08-23T07:00:00Z"})
    monkeypatch.setattr(rw.time, "sleep", lambda s: None)
    monkeypatch.setattr("sys.argv", ["retry_wayback.py"])
    assert rw.main() == 0
    assert saves == ["https://example.org/doc.pdf"]      # six transport failures: still retried
    m = json.loads(mp.read_text(encoding="utf-8"))
    assert m["wayback"]["ok"] is True and "gave_up" not in m["wayback"]


# --- retry_wayback: every capture needs a witness of ITS OWN ---

STALE = "https://web.archive.org/web/20260701000000/https://example.org/doc.pdf"


def test_capture_that_never_had_a_save_attempt_is_retried(corpus, monkeypatch):
    # a manifest with no wayback block at all was skipped outright until
    # 28 Aug 2026, so 43 early captures never got a snapshot
    d, _ = corpus.add_capture()
    root = corpus.finish()
    assert "wayback" not in manifest_of(d)
    calls = wayback_save_recorder(monkeypatch, {"ok": True, "snapshot": SNAP, "fresh": True})
    assert run_retry(monkeypatch, root) == 0
    assert calls == ["https://example.org/doc.pdf"]
    assert manifest_of(d)["wayback"]["snapshot"] == SNAP


def test_snapshot_older_than_the_capture_is_retried_for_a_real_witness(corpus, monkeypatch):
    d, _ = corpus.add_capture(wayback={"ok": True, "snapshot": STALE})
    root = corpus.finish()
    fresh = {"ok": True, "snapshot": SNAP, "fresh": True}
    calls = wayback_save_recorder(monkeypatch, fresh)
    run_retry(monkeypatch, root)
    m = manifest_of(d)
    assert calls == ["https://example.org/doc.pdf"]
    assert m["wayback"] == fresh
    assert m["wayback_attempts"][0]["snapshot"] == STALE     # the older one is kept


def test_a_failed_refresh_never_costs_us_the_snapshot_we_hold(corpus, monkeypatch):
    d, _ = corpus.add_capture(wayback={"ok": True, "snapshot": STALE})
    root = corpus.finish()
    wayback_save_recorder(monkeypatch, {"ok": False, "status_code": 503})
    run_retry(monkeypatch, root)
    m = manifest_of(d)
    assert m["wayback"]["snapshot"] == STALE and m["wayback"]["ok"] is True
    assert "last_refresh_attempt" in m["wayback"]


def test_a_failed_refresh_records_what_the_archive_answered(corpus, monkeypatch):
    # without this a target that can never be refreshed looks unexplained
    d, _ = corpus.add_capture(wayback={"ok": True, "snapshot": STALE})
    root = corpus.finish()
    wayback_save_recorder(monkeypatch, {"ok": False, "status_code": 200, "answered": True,
                                        "error": "error:not-found", "via": "spn2"})
    run_retry(monkeypatch, root)
    m = manifest_of(d)
    assert m["wayback"]["last_refresh_error"] == "error:not-found"
    assert m["wayback"]["last_refresh_via"] == "spn2"
    assert m["wayback_attempts"][-1]["error"] == "error:not-found"


def test_a_fresh_snapshot_is_left_alone(corpus, monkeypatch):
    # capture fetched_at and snapshot stamp agree: it already witnesses this capture
    d, _ = corpus.add_capture(wayback={"ok": True, "snapshot": SNAP})
    root = corpus.finish()
    before = (d / "manifest.json").read_bytes()
    calls = wayback_save_recorder(monkeypatch, {"ok": True})
    run_retry(monkeypatch, root)
    assert calls == [] and (d / "manifest.json").read_bytes() == before


def test_the_run_is_bounded_and_serves_the_neediest_first(corpus, monkeypatch):
    for i in range(3):                                  # nothing archived at all
        corpus.add_capture(tslug=f"provider-live-none{i}", url=f"https://example.org/none{i}.pdf")
    for i in range(3):                                  # only an older snapshot
        corpus.add_capture(tslug=f"provider-live-old{i}", url=f"https://example.org/old{i}.pdf",
                           wayback={"ok": True, "snapshot": STALE})
    root = corpus.finish()
    monkeypatch.setattr(RW, "MAX_PER_RUN", 3)
    calls = wayback_save_recorder(monkeypatch, {"ok": True, "snapshot": SNAP, "fresh": True})
    run_retry(monkeypatch, root)
    assert len(calls) == 3
    assert all("none" in u for u in calls)              # captures with no snapshot first


def test_has_witness_judges_a_legacy_manifest_from_its_capture_time(corpus):
    d_fresh, _ = corpus.add_capture(tslug="provider-live-f", wayback={"ok": True, "snapshot": SNAP})
    d_stale, _ = corpus.add_capture(tslug="provider-live-s", wayback={"ok": True, "snapshot": STALE})
    d_none, _ = corpus.add_capture(tslug="provider-live-n")
    corpus.finish()
    assert RW.has_witness(manifest_of(d_fresh)) is True
    assert RW.has_witness(manifest_of(d_stale)) is False
    assert RW.has_witness(manifest_of(d_none)) is False


def test_a_refusing_archive_stops_the_pass_instead_of_being_hammered(corpus, monkeypatch):
    # 28 Aug 2026: every save timed out because the Internet Archive was dropping
    # this runner's connections; 25 attempts cost half an hour and learned nothing
    for i in range(8):
        corpus.add_capture(tslug=f"provider-live-x{i}", url=f"https://example.org/{i}.pdf")
    root = corpus.finish()
    calls = wayback_save_recorder(
        monkeypatch, {"ok": False, "error": "ReadTimeout(...)"})
    run_retry(monkeypatch, root)
    assert len(calls) == RW.TRANSPORT_FAILURES_BEFORE_STOP


def test_a_real_verdict_does_not_trip_the_circuit_breaker(corpus, monkeypatch):
    # 404 from Save Page Now is an answer about the URL, not the Archive refusing us
    for i in range(5):
        corpus.add_capture(tslug=f"provider-live-y{i}", url=f"https://example.org/y{i}.pdf")
    root = corpus.finish()
    calls = wayback_save_recorder(monkeypatch, {"ok": False, "status_code": 404})
    run_retry(monkeypatch, root)
    assert len(calls) == 5


def test_the_pass_stops_when_its_wall_clock_budget_is_spent(corpus, monkeypatch):
    for i in range(6):
        corpus.add_capture(tslug=f"provider-live-z{i}", url=f"https://example.org/z{i}.pdf")
    root = corpus.finish()
    clock = iter([0, 0, 1, 2, 10_000, 10_001, 10_002, 10_003, 10_004])
    monkeypatch.setattr(RW, "time", types.SimpleNamespace(
        sleep=lambda s: None, monotonic=lambda: next(clock)))
    calls = wayback_save_recorder(monkeypatch, {"ok": True, "snapshot": SNAP, "fresh": True})
    monkeypatch.setattr(sys, "argv", ["retry_wayback.py"])
    monkeypatch.setattr(RW, "DATA", root)
    RW.main()
    assert 0 < len(calls) < 6          # stopped early, did not work the whole queue


def test_budget_env_override_is_clamped(monkeypatch):
    monkeypatch.setenv("GPAI_WAYBACK_BUDGET", "5")
    assert RW.budget_s() == 60.0
    monkeypatch.setenv("GPAI_WAYBACK_BUDGET", "99999")
    assert RW.budget_s() == 3600.0
    monkeypatch.setenv("GPAI_WAYBACK_BUDGET", "not a number")
    assert RW.budget_s() == float(RW.BUDGET_S)


def test_superseded_attempts_do_not_hold_a_capture_at_its_cap(corpus, monkeypatch):
    # the cap was reached through the anonymous endpoint, which answered by
    # returning a pre-existing capture; that says nothing about what the
    # authenticated API can archive, so it must not keep the capture capped
    old = [{"ok": True, "status_code": 200, "snapshot": STALE, "superseded": True}] * 5
    d, _ = corpus.add_capture(wayback={"ok": True, "snapshot": STALE},
                              extra_manifest={"wayback_attempts": old})
    root = corpus.finish()
    calls = wayback_save_recorder(monkeypatch, {"ok": True, "snapshot": SNAP, "fresh": True})
    run_retry(monkeypatch, root)
    assert calls == ["https://example.org/doc.pdf"]
    assert manifest_of(d)["wayback"]["snapshot"] == SNAP
    # the superseded history is kept, not deleted
    assert sum(1 for a in manifest_of(d)["wayback_attempts"] if a.get("superseded")) == 5


def test_an_spn_verdict_counts_as_an_answered_attempt(corpus, monkeypatch):
    verdicts = [{"answered": True, "error": "error:not-found"}] * RW.MAX_ATTEMPTS
    d, _ = corpus.add_capture(wayback={"ok": False, "answered": True,
                                       "error": "error:not-found"},
                              extra_manifest={"wayback_attempts": verdicts})
    root = corpus.finish()
    calls = wayback_save_recorder(monkeypatch, {"ok": True})
    run_retry(monkeypatch, root)
    assert manifest_of(d)["wayback"]["gave_up"].startswith("exhausted")
    assert calls == []


def test_repeated_spn_verdicts_do_not_trip_the_refusal_breaker(corpus, monkeypatch):
    for i in range(5):
        corpus.add_capture(tslug=f"provider-live-v{i}", url=f"https://example.org/v{i}.pdf")
    root = corpus.finish()
    calls = wayback_save_recorder(
        monkeypatch, {"ok": False, "answered": True, "error": "error:not-found"})
    run_retry(monkeypatch, root)
    assert len(calls) == 5


def test_repeated_failed_refreshes_do_not_retire_a_capture_that_has_a_snapshot(corpus, monkeypatch):
    # the kept block used to be re-filed as an "answered attempt" every time, so
    # a few dropped connections exhausted the budget of a capture we had archived
    d, _ = corpus.add_capture(wayback={"ok": True, "snapshot": STALE})
    root = corpus.finish()
    wayback_save_recorder(monkeypatch, {"ok": False, "error": "ReadTimeout(...)"})
    for _ in range(RW.MAX_ATTEMPTS + 2):
        run_retry(monkeypatch, root)
    m = manifest_of(d)
    assert "gave_up" not in m["wayback"]
    assert m["wayback"]["snapshot"] == STALE
    answered = RW.answered_attempts(m.get("wayback_attempts", []) + [m["wayback"]])
    assert len(answered) <= 1, "the held snapshot was counted as an attempt again"

