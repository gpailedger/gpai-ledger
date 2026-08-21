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
