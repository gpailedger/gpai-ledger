import pytest

import capture as cap


class FakeResp:
    """Duck-typed requests.Response: only the surface fetch() touches."""

    def __init__(self, status=200, url="https://example.org/doc",
                 headers=None, chunks=(b"body",)):
        self.status_code = status
        self.url = url
        self.headers = headers if headers is not None else {}
        self._chunks = list(chunks)
        self.iter_calls = 0
        self.closed = False

    def iter_content(self, n):
        self.iter_calls += 1
        for c in self._chunks:
            yield c

    def close(self):
        self.closed = True


class FakeGet:
    """Stands in for capture.requests.get: pops queued responses, records calls."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def sleeps(monkeypatch):
    rec = []
    monkeypatch.setattr(cap.time, "sleep", rec.append)
    return rec


def _wire(monkeypatch, *responses):
    fake = FakeGet(*responses)
    monkeypatch.setattr(cap.requests, "get", fake)
    return fake


# --- fetch: happy path ---

def test_fetch_200_returns_body_and_meta(monkeypatch, sleeps):
    r = FakeResp(200, url="https://example.org/final",
                 headers={"Content-Type": "text/html; charset=utf-8",
                          "ETag": '"e1"',
                          "Last-Modified": "Wed, 01 Jan 2026 00:00:00 GMT",
                          "Content-Length": "9"},
                 chunks=(b"the ", b"body!"))
    _wire(monkeypatch, r)
    body, meta = cap.fetch("https://example.org/doc")
    assert body == b"the body!"
    assert meta["url"] == "https://example.org/doc"
    assert meta["final_url"] == "https://example.org/final"
    assert meta["status_code"] == 200
    assert meta["content_type"] == "text/html"  # ;charset parameter split off
    assert meta["etag"] == '"e1"'
    assert meta["last_modified"] == "Wed, 01 Jan 2026 00:00:00 GMT"
    assert meta["content_length"] == "9"
    assert sleeps == []


# --- fetch: retry loop ---

def test_fetch_500_then_200_retries_and_succeeds(monkeypatch, sleeps):
    fake = _wire(monkeypatch, FakeResp(500), FakeResp(200, chunks=(b"ok",)))
    body, meta = cap.fetch("https://example.org/doc")
    assert body == b"ok" and meta["status_code"] == 200
    assert len(fake.calls) == 2
    assert sleeps == [3]  # 3 * (attempt + 1) backoff

def test_fetch_exhausted_retries_raise_last_error(monkeypatch, sleeps):
    fake = _wire(monkeypatch, FakeResp(500), FakeResp(500))
    with pytest.raises(RuntimeError, match="HTTP 500") as exc:
        cap.fetch("https://example.org/doc", retries=1)
    assert not isinstance(exc.value, cap.PermanentFetchError)
    assert len(fake.calls) == 2
    assert sleeps == [3, 6]

@pytest.mark.parametrize("status", sorted(cap.NO_RETRY_STATUS))
def test_fetch_permanent_status_single_attempt(monkeypatch, sleeps, status):
    fake = _wire(monkeypatch, FakeResp(status))
    with pytest.raises(cap.PermanentFetchError):
        cap.fetch("https://example.org/doc")
    assert len(fake.calls) == 1
    assert sleeps == []


# --- fetch: Retry-After ---

def test_fetch_429_sleeps_retry_after_then_retries(monkeypatch, sleeps):
    fake = _wire(monkeypatch, FakeResp(429, headers={"Retry-After": "7"}),
                 FakeResp(200, chunks=(b"ok",)))
    body, _ = cap.fetch("https://example.org/doc")
    assert body == b"ok" and len(fake.calls) == 2
    assert sleeps == [7]  # continue skips the generic backoff sleep

def test_fetch_retry_after_capped_at_120(monkeypatch, sleeps):
    _wire(monkeypatch, FakeResp(429, headers={"Retry-After": "999"}),
          FakeResp(200, chunks=(b"ok",)))
    cap.fetch("https://example.org/doc")
    assert sleeps == [120]


# --- fetch: size cap ---

def test_fetch_declared_length_over_cap_permanent_and_unread(monkeypatch, sleeps):
    r = FakeResp(200, headers={"Content-Length": str(cap.MAX_FETCH_BYTES + 1)})
    fake = _wire(monkeypatch, r)
    with pytest.raises(cap.PermanentFetchError):
        cap.fetch("https://example.org/doc")
    assert len(fake.calls) == 1
    assert r.iter_calls == 0 and r.closed

def test_fetch_streamed_body_over_cap_despite_lying_length(monkeypatch, sleeps):
    # same 16 MiB buffer yielded 4 times: 64 MiB counted, ~16 MiB resident
    chunk = b"\0" * (16 * 1024 * 1024)
    r = FakeResp(200, headers={"Content-Length": "100"}, chunks=[chunk] * 4)
    fake = _wire(monkeypatch, r)
    with pytest.raises(cap.PermanentFetchError):
        cap.fetch("https://example.org/doc")
    assert len(fake.calls) == 1


# --- fetch: validators / 304 ---

def test_fetch_sends_validator_headers(monkeypatch, sleeps):
    fake = _wire(monkeypatch, FakeResp(200, chunks=(b"ok",)))
    cap.fetch("https://example.org/doc",
              validators={"etag": '"e1"',
                          "last_modified": "Mon, 02 Feb 2026 00:00:00 GMT"})
    sent = fake.calls[0][1]["headers"]
    assert sent["If-None-Match"] == '"e1"'
    assert sent["If-Modified-Since"] == "Mon, 02 Feb 2026 00:00:00 GMT"
    assert sent["User-Agent"] == cap.USER_AGENT

def test_fetch_304_with_validators_returns_no_body(monkeypatch, sleeps):
    r = FakeResp(304, headers={"ETag": '"e1"'})
    fake = _wire(monkeypatch, r)
    body, meta = cap.fetch("https://example.org/doc", validators={"etag": '"e1"'})
    assert body is None
    assert meta["status_code"] == 304
    assert len(fake.calls) == 1 and r.closed

def test_fetch_304_without_validators_falls_through_to_error(monkeypatch, sleeps):
    fake = _wire(monkeypatch, FakeResp(304), FakeResp(304))
    with pytest.raises(RuntimeError, match="HTTP 304"):
        cap.fetch("https://example.org/doc", retries=1)
    assert len(fake.calls) == 2

def test_fetch_200_with_validators_returns_body(monkeypatch, sleeps):
    _wire(monkeypatch, FakeResp(200, chunks=(b"fresh",)))
    body, meta = cap.fetch("https://example.org/doc", validators={"etag": '"old"'})
    assert body == b"fresh" and meta["status_code"] == 200


# --- fetch: URL policy gate runs before any request ---

def test_fetch_refuses_private_url_before_any_request(monkeypatch, sleeps):
    fake = _wire(monkeypatch)  # empty queue: any get call would blow up
    with pytest.raises(RuntimeError):
        cap.fetch("http://127.0.0.1/x")
    assert fake.calls == []


# --- _read_capped ---

def test_read_capped_joins_chunks_under_limit():
    assert cap._read_capped(FakeResp(chunks=(b"ab", b"cd")), limit=10) == b"abcd"

def test_read_capped_raises_past_limit():
    with pytest.raises(cap.PermanentFetchError):
        cap._read_capped(FakeResp(chunks=(b"abcdef", b"ghijkl")), limit=10)


# --- _assert_public_http ---

@pytest.mark.parametrize("url", [
    "file:///x", "javascript:alert(1)", "ftp://x",
    "http://127.0.0.1/", "http://10.1.2.3/x", "http://[::1]/",
])
def test_assert_public_http_rejects(url):
    with pytest.raises(RuntimeError):
        cap._assert_public_http(url)

@pytest.mark.parametrize("url", ["https://example.org", "http://93.184.216.34"])
def test_assert_public_http_accepts(url):
    assert cap._assert_public_http(url) is None


# --- guess_ext: branch order is content-type -> URL suffix -> magic bytes ---

@pytest.mark.parametrize("ctype,ext", [
    ("application/pdf", ".pdf"), ("application/zip", ".zip"),
    ("text/html", ".html"), ("text/markdown", ".md"),
])
def test_guess_ext_maps_content_type(ctype, ext):
    assert cap.guess_ext(ctype, "https://a/x") == ext

def test_guess_ext_mapped_type_wins_over_magic_bytes():
    assert cap.guess_ext("text/html", "https://a/x", b"%PDF-1.7") == ".html"

def test_guess_ext_url_suffix_fallback_strips_query():
    assert cap.guess_ext("application/octet-stream", "https://a/b.PDF?sig=1") == ".pdf"

def test_guess_ext_url_suffix_wins_over_magic_bytes():
    assert cap.guess_ext("", "https://a/b.txt", b"%PDF-1.7") == ".txt"

def test_guess_ext_sniffs_pdf_magic_on_unmapped_type():
    assert cap.guess_ext("application/octet-stream", "https://a/download",
                         b"%PDF-1.7 x") == ".pdf"

def test_guess_ext_sniffs_zip_magic_on_unmapped_type():
    assert cap.guess_ext("application/octet-stream", "https://a/download",
                         b"PK\x03\x04rest") == ".zip"

def test_guess_ext_sniffs_html_with_leading_whitespace():
    assert cap.guess_ext("application/octet-stream", "https://a/x",
                         b"\n  <!DOCTYPE HTML><html>") == ".html"

def test_guess_ext_defaults_to_bin():
    assert cap.guess_ext("application/weird", "https://a/x", b"\x00\x01") == ".bin"
    assert cap.guess_ext(None, "https://a/x") == ".bin"


# --- absence-claim support: diagnostic errors, vantage, independent witness ---

def test_permanent_error_carries_status_and_diagnostic_headers(monkeypatch):
    resp = FakeResp(status=404, headers={"Server": "AzureFrontDoor", "X-Azure-Ref": "ref1",
                                         "X-Irrelevant": "x"})
    monkeypatch.setattr(cap.requests, "get", FakeGet(resp))
    monkeypatch.setattr(cap.time, "sleep", lambda s: None)
    with pytest.raises(cap.PermanentFetchError) as ei:
        cap.fetch("https://example.org/doc")
    assert ei.value.status_code == 404
    assert ei.value.headers == {"Server": "AzureFrontDoor", "X-Azure-Ref": "ref1"}


def test_events_record_vantage(tmp_path, monkeypatch):
    store = cap.Store(tmp_path / "data")
    store.event(source="s", target="t", outcome="error")
    line = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8")
    assert '"vantage": "operator"' in line or '"vantage": "github-runner"' in line


from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

SNAP = "https://web.archive.org/web/20260822090000/https://x/d.pdf"


def _memento(age_s=0):
    return format_datetime(datetime.now(timezone.utc) - timedelta(seconds=age_s))


def _witness_env(monkeypatch, save, replay_responses):
    monkeypatch.setattr(cap, "wayback_save", lambda url, **k: save)
    fake = FakeGet(*replay_responses)
    monkeypatch.setattr(cap.requests, "get", fake)
    sleeps = []
    monkeypatch.setattr(cap.time, "sleep", lambda s: sleeps.append(s))
    return fake, sleeps


OK_SAVE = {"ok": True, "status_code": 200, "snapshot": SNAP, "at": "t"}


def test_witness_live_when_fresh_replay_is_200(monkeypatch):
    _witness_env(monkeypatch, OK_SAVE, [FakeResp(200, url=SNAP, headers={
        "Memento-Datetime": _memento(5), "X-Archive-Orig-Server": "AzureFrontDoor"})])
    w = cap.wayback_witness("https://x/d.pdf")
    assert (w["saw"], w["status"], w["origin_server"]) == ("live", 200, "AzureFrontDoor")
    assert w["final_url"] == SNAP and w["memento_datetime"]


@pytest.mark.parametrize("status", [404, 410])
def test_witness_absent_when_fresh_replay_is_absence_status(monkeypatch, status):
    _witness_env(monkeypatch, OK_SAVE, [FakeResp(status, headers={"Memento-Datetime": _memento(5)})])
    w = cap.wayback_witness("https://x/d.pdf")
    assert (w["saw"], w["status"]) == ("absent", status)


def test_witness_stale_snapshot_is_inconclusive(monkeypatch):
    # Wayback redirected to the NEAREST (old) capture: a 404 from months ago is
    # not evidence about now — and neither would a 200 be
    fake, sleeps = _witness_env(monkeypatch, OK_SAVE, [
        FakeResp(404, headers={"Memento-Datetime": _memento(86400 * 30)})] * 4)
    w = cap.wayback_witness("https://x/d.pdf")
    assert w["saw"] == "inconclusive" and w["reason"] == "stale-snapshot"
    assert w["status"] is None and len(fake.calls) == 4 and len(sleeps) == 3


def test_witness_200_without_memento_is_inconclusive_not_live(monkeypatch):
    fake, _ = _witness_env(monkeypatch, OK_SAVE, [FakeResp(200)] * 4)
    w = cap.wayback_witness("https://x/d.pdf")
    assert (w["saw"], w["status"], w["reason"]) == ("inconclusive", None, "not-replayable")


def test_witness_fresh_non_absence_status_is_inconclusive_with_status(monkeypatch):
    _witness_env(monkeypatch, OK_SAVE, [FakeResp(503, headers={"Memento-Datetime": _memento(5)})])
    w = cap.wayback_witness("https://x/d.pdf")
    assert (w["saw"], w["status"], w["reason"]) == ("inconclusive", 503, "replay-status-503")


def test_witness_retries_until_fresh_capture_appears(monkeypatch):
    fake, sleeps = _witness_env(monkeypatch, OK_SAVE, [
        FakeResp(200), FakeResp(404, headers={"Memento-Datetime": _memento(1)})])
    w = cap.wayback_witness("https://x/d.pdf")
    assert w["saw"] == "absent" and len(fake.calls) == 2 and sleeps == [cap.WITNESS_POLL_S]


def test_witness_inconclusive_when_every_replay_raises(monkeypatch):
    fake, sleeps = _witness_env(monkeypatch, OK_SAVE, [RuntimeError("net")] * 4)
    w = cap.wayback_witness("https://x/d.pdf")
    assert w["saw"] == "inconclusive" and w["reason"].startswith("replay-error")
    assert len(sleeps) == 3


def test_witness_inconclusive_when_save_has_no_capture(monkeypatch):
    fake, _ = _witness_env(monkeypatch, {"ok": False, "status_code": 523, "snapshot": None}, [])
    w = cap.wayback_witness("https://x/d.pdf")
    assert w["saw"] == "inconclusive" and "spn-no-capture" in w["reason"]
    assert w["spn_status"] == 523 and fake.calls == []


def test_witness_rate_limited_is_flagged(monkeypatch):
    _witness_env(monkeypatch, {"ok": False, "status_code": 429, "snapshot": None}, [])
    w = cap.wayback_witness("https://x/d.pdf")
    assert (w["saw"], w["reason"]) == ("inconclusive", "rate-limited")


def test_wayback_save_records_spn_answer_separately_from_the_replay(monkeypatch):
    # SPN answered 302 (capture assigned); the final hop is the replay of an
    # archived 404 page — status_code keeps the final hop, spn_status SPN's verdict
    hop = FakeResp(302, headers={"Location": SNAP})
    resp = FakeResp(404, url=SNAP, headers={"Memento-Datetime": _memento(1)})
    resp.history = [hop]
    monkeypatch.setattr(cap.requests, "get", FakeGet(resp))
    s = cap.wayback_save("https://x/d.pdf")
    assert s["ok"] and s["snapshot"] == SNAP
    assert (s["spn_status"], s["status_code"]) == (302, 404)


def test_witness_replay_429_behind_an_accepted_capture_is_not_rate_limited(monkeypatch):
    save = {"ok": True, "status_code": 429, "spn_status": 302, "snapshot": SNAP, "at": "t"}
    fake, _ = _witness_env(monkeypatch, save, [FakeResp(429)] * 4)
    w = cap.wayback_witness("https://x/d.pdf")
    assert w["reason"] != "rate-limited" and w["spn_status"] == 302
    assert w["saw"] == "inconclusive" and len(fake.calls) == 4


def test_witness_unparsable_memento_is_flagged_without_repolling(monkeypatch):
    fake, sleeps = _witness_env(monkeypatch, OK_SAVE, [
        FakeResp(404, headers={"Memento-Datetime": "not a date"})] * 4)
    w = cap.wayback_witness("https://x/d.pdf")
    assert (w["saw"], w["reason"]) == ("inconclusive", "memento-unparsable")
    assert len(fake.calls) == 1 and sleeps == []


def test_witness_naive_memento_is_read_as_utc(monkeypatch):
    naive = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S -0000")
    _witness_env(monkeypatch, OK_SAVE, [FakeResp(404, headers={"Memento-Datetime": naive})])
    w = cap.wayback_witness("https://x/d.pdf")
    assert w["saw"] == "absent"


def test_wayback_save_snapshot_from_content_location(monkeypatch):
    resp = FakeResp(200, url="https://web.archive.org/save/https://x/d.pdf",
                    headers={"Content-Location": "/web/20260822090000/https://x/d.pdf"})
    monkeypatch.setattr(cap.requests, "get", FakeGet(resp))
    s = cap.wayback_save("https://x/d.pdf")
    assert s["ok"] and s["snapshot"] == SNAP


def test_wayback_save_snapshot_from_first_redirect_hop(monkeypatch):
    # no Content-Location; r.url is an OLDER nearest capture — must not be used
    hop = FakeResp(302, headers={"Location": SNAP})
    resp = FakeResp(200, url="https://web.archive.org/web/20200101000000/https://x/d.pdf")
    resp.history = [hop]
    monkeypatch.setattr(cap.requests, "get", FakeGet(resp))
    s = cap.wayback_save("https://x/d.pdf")
    assert s["ok"] and s["snapshot"] == SNAP


def test_wayback_save_not_ok_without_any_capture(monkeypatch):
    resp = FakeResp(523, url="https://web.archive.org/save/https://x/d.pdf")
    monkeypatch.setattr(cap.requests, "get", FakeGet(resp))
    s = cap.wayback_save("https://x/d.pdf")
    assert not s["ok"] and s["snapshot"] is None and s["status_code"] == 523
