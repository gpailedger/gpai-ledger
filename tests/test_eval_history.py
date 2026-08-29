"""The harvest of AIAL's evaluation history from their git history.

What these protect: the grade a model was given months ago is only recoverable
from upstream git, and it must land under the right model, exactly once, without
ever claiming this project observed it at the time it was committed.
"""
import json

import capture as cap
import harvest_eval_history as H
import pytest

NL = chr(10)


A_YAML = b'model_name: "Phi-4"\norganization: "Microsoft"\nS1:\n  D1:\n    score: 9\n'
B_YAML = b'model_name: "Phi-4"\norganization: "Microsoft"\nS1:\n  D1:\n    score: 7\n'


@pytest.fixture(autouse=True)
def _data_in_tmp(tmp_path, monkeypatch):
    # the harvester writes through its own module-level DATA and reads the real
    # registry; a test that patched only one of them would touch the repository
    monkeypatch.setattr(H, "DATA", tmp_path / "data")
    monkeypatch.setattr(H, "REGISTRY", tmp_path / "sources.json")
    (tmp_path / "data").mkdir()
    return tmp_path


def _registry(tmp_path, sources):
    (tmp_path / "sources.json").write_text(json.dumps({"sources": sources}),
                                           encoding="utf-8")


# --- what gets harvested -----------------------------------------------------

def test_each_distinct_state_is_planned_once_and_dated_by_its_first_commit(monkeypatch):
    # a file reverted to a value it already held must not be harvested twice, and
    # the date recorded is when the state STARTED to stand
    commits = [{"sha": "c1", "commit": {"author": {"date": "2026-03-01T00:00:00Z"}}},
               {"sha": "c2", "commit": {"author": {"date": "2026-04-01T00:00:00Z"}}},
               {"sha": "c3", "commit": {"author": {"date": "2026-05-01T00:00:00Z"}}}]
    trees = {"c1": {"phi.yaml": "blobA"},
             "c2": {"phi.yaml": "blobB"},
             "c3": {"phi.yaml": "blobA"}}          # reverted
    monkeypatch.setattr(H, "commits", lambda tok: commits)
    monkeypatch.setattr(H, "tree_at", lambda sha, tok: trees[sha])
    plan = H.plan("")
    assert [p["blob"] for p in plan] == ["blobA", "blobB"]
    assert plan[0]["date"].startswith("2026-03"), "not dated by its first commit"
    assert plan[0]["commit"] == "c1"


def test_a_missing_evals_directory_is_normal_but_any_other_failure_is_not(monkeypatch):
    # a 404 means the directory did not exist at that commit. A rate limit or a
    # 5xx read as "no evaluations here" would silently drop states AND re-date
    # the ones that follow, because plan() dates a state by the first commit at
    # which it SEES it — a grade published as standing months after it did.
    import urllib.error

    def _raise(code):
        def _f(path, tok):
            raise urllib.error.HTTPError("u", code, "boom", {}, None)
        return _f

    monkeypatch.setattr(H, "api", _raise(404))
    assert H.tree_at("deadbeef", "") == {}

    monkeypatch.setattr(H, "api", _raise(403))          # rate limited
    with pytest.raises(urllib.error.HTTPError):
        H.tree_at("deadbeef", "")

    monkeypatch.setattr(H, "api", _raise(502))
    with pytest.raises(urllib.error.HTTPError):
        H.tree_at("deadbeef", "")

    monkeypatch.setattr(H, "api", lambda p, t: (_ for _ in ()).throw(ValueError("bad json")))
    with pytest.raises(RuntimeError):
        H.tree_at("deadbeef", "")


def test_every_state_of_one_evaluation_is_one_targets_history():
    # each state is fetched from its own pinned commit URL, but they are versions
    # of ONE target — otherwise the history is 318 unrelated single-capture targets
    a = H.identity_url("phi.yaml")
    b = H.identity_url("phi.yaml")
    assert a == b and "/main/" in a
    assert H.pinned_url("c1", "phi.yaml") != H.pinned_url("c2", "phi.yaml")
    # a filename with a space must still produce a fetchable URL
    assert " " not in H.pinned_url("c1", "inkling small.yaml")
    assert "inkling%20small.yaml" in H.pinned_url("c1", "inkling small.yaml")


# --- where it lands ----------------------------------------------------------

def test_a_renamed_evaluation_still_lands_under_its_model(_data_in_tmp):
    # phi.yaml became phi-4.yaml; the filename changed, the content did not
    _registry(_data_in_tmp, [{"id": "microsoft/phi-4", "provider": "Microsoft",
                              "model": "Phi-4", "targets": []}])
    idx = H.registry_index()
    assert H.resolve(A_YAML.decode(), "phi.yaml", idx) == "microsoft/phi-4"


def test_an_evaluation_of_a_model_we_do_not_track_is_not_filed_under_a_neighbour(
        _data_in_tmp):
    _registry(_data_in_tmp, [{"id": "microsoft/phi-4", "provider": "Microsoft",
                              "model": "Phi-4", "targets": []}])
    idx = H.registry_index()
    other = 'model_name: "Gemini 2.5 Flash Image"\norganization: "Google"\n'
    assert H.resolve(other, "gemini-2.5-flash-image.yaml", idx) == H.FALLBACK_SOURCE


def test_the_filename_only_decides_when_the_content_does_not(_data_in_tmp):
    _registry(_data_in_tmp, [{"id": "org/model", "provider": "Org", "model": "Model",
                              "targets": [{"kind": "aial-eval",
                                           "url": "https://x/evals/weird.yaml"}]}])
    idx = H.registry_index()
    assert H.resolve("model_name: \"\"\norganization: \"\"\n", "weird.yaml",
                     idx) == "org/model"


# --- honesty of the record ---------------------------------------------------

def test_a_harvested_state_records_upstream_provenance_without_claiming_to_have_seen_it(
        _data_in_tmp, monkeypatch):
    _registry(_data_in_tmp, [{"id": "microsoft/phi-4", "provider": "Microsoft",
                              "model": "Phi-4", "targets": []}])
    monkeypatch.setattr(H, "commits", lambda tok: [
        {"sha": "c1", "commit": {"author": {"date": "2026-03-01T00:00:00Z"}}}])
    monkeypatch.setattr(H, "tree_at", lambda sha, tok: {"phi.yaml": "blobA"})
    monkeypatch.setattr(H, "token", lambda: "")
    monkeypatch.setattr(cap, "fetch", lambda url, **k: (A_YAML, {
        "url": url, "final_url": url, "status_code": 200,
        "content_type": "text/plain", "etag": None, "last_modified": None,
        "content_length": str(len(A_YAML)), "fetched_at": "2026-08-29T14:00:00Z"}))
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (b"proof", {"ok": True, "calendars": []}))
    assert H.main() == 0

    m = json.loads(next((_data_in_tmp / "data" / "captures").glob("*/*/*/manifest.json"))
                   .read_text(encoding="utf-8"))
    assert m["target_kind"] == "aial-eval-history"
    assert m["source_id"] == "microsoft/phi-4"
    assert m["git_commit"] == "c1" and m["git_blob_sha"] == "blobA"
    assert m["git_commit_date"] == "2026-03-01T00:00:00Z"
    # the fetch time is OURS and must not be backdated to the commit
    assert m["http"]["fetched_at"] == "2026-08-29T14:00:00Z"
    assert m["harvested_from"] == "upstream git history"
    # the pinned URL is what was actually fetched
    assert "/c1/evals/phi.yaml" in m["http"]["url"]
    # a commit-pinned URL on GitHub is already immutable: no snapshot is claimed
    assert "wayback" not in m


def test_a_second_run_re_fetches_nothing(_data_in_tmp, monkeypatch):
    _registry(_data_in_tmp, [{"id": "microsoft/phi-4", "provider": "Microsoft",
                              "model": "Phi-4", "targets": []}])
    monkeypatch.setattr(H, "commits", lambda tok: [
        {"sha": "c1", "commit": {"author": {"date": "2026-03-01T00:00:00Z"}}}])
    monkeypatch.setattr(H, "tree_at", lambda sha, tok: {"phi.yaml": "blobA"})
    monkeypatch.setattr(H, "token", lambda: "")
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (b"proof", {"ok": True}))
    calls = []

    def _fetch(url, **k):
        calls.append(url)
        return A_YAML, {"url": url, "final_url": url, "status_code": 200,
                        "content_type": "text/plain", "etag": None,
                        "last_modified": None, "content_length": "1",
                        "fetched_at": "2026-08-29T14:00:00Z"}

    monkeypatch.setattr(cap, "fetch", _fetch)
    H.main()
    assert len(calls) == 1
    H.main()
    assert len(calls) == 1, "an already-held state was fetched again"


def test_upstream_provenance_can_never_overwrite_a_computed_field(_data_in_tmp,
                                                                  monkeypatch):
    # manifest_extra is caller-supplied; it must not be able to rewrite the hash,
    # the size, or the HTTP record that the evidence rests on
    store = cap.Store(_data_in_tmp / "data")
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (None, {"ok": False}))
    _rel, m = cap.store_new_version(
        store, source_id="s/m", provider="P", model="M", kind="aial-eval-history",
        tslug="aial-eval-history-abcd1234", event_url="https://x/y", raw=b"hello",
        meta={"url": "https://x/y", "fetched_at": "2026-08-29T00:00:00Z"},
        ext=".yaml", text="hello", notes=[], text_sha=None, do_ots=False,
        manifest_extra={"sha256": "0" * 64, "size_bytes": 99999,
                        "http": {"url": "https://evil/"}, "git_commit": "c1"})
    assert m["sha256"] == cap.sha256_hex(b"hello")
    assert m["size_bytes"] == 5
    assert m["http"]["url"] == "https://x/y"
    assert m["git_commit"] == "c1"


def test_the_harvested_history_is_restricted_like_the_evaluation_itself():
    assert "aial-eval-history" in cap.RESTRICTED_KINDS
    assert "aial-eval-page" in cap.RESTRICTED_KINDS
    assert "aial-method" in cap.RESTRICTED_KINDS


# --- the published earlier-version pages -------------------------------------

PAGE_URL = "https://aial.ie/research/gpai-training-transparency/evals/apertus/"
PAGE_HTML = (b'<html><body>'
             b'<a href="/research/gpai-training-transparency/evals/apertus/version-2026-01-12">a</a>'
             b'<a href="/research/gpai-training-transparency/evals/apertus/version-2026-03-30">b</a>'
             b'<a href="/research/gpai-training-transparency/evals/apertus/version-2026-01-12">dup</a>'
             b'<a href="https://elsewhere.example/version-2026-01-12">offsite</a>'
             b'<a href="/research/gpai-training-transparency/methodology">not a version</a>'
             b'</body></html>')


def test_only_aials_own_version_pages_are_discovered_and_each_only_once():
    got = H.version_links(PAGE_HTML.decode(), PAGE_URL)
    assert got == [PAGE_URL + "version-2026-01-12", PAGE_URL + "version-2026-03-30"]
    assert not H.version_links("<a href='/evals/x/'>no versions here</a>", PAGE_URL)


def _store_page(store, url=PAGE_URL, html=PAGE_HTML):
    cap.store_new_version(
        store, source_id="swiss-ai-initiative/apertus", provider="Swiss AI",
        model="Apertus", kind="aial-eval-page",
        tslug=cap.target_slug("aial-eval-page", url), event_url=url, raw=html,
        meta={"url": url, "final_url": url, "status_code": 200,
              "content_type": "text/html", "etag": None, "last_modified": None,
              "content_length": str(len(html)), "fetched_at": "2026-08-29T14:00:00Z"},
        ext=".html", text="page text", notes=[], text_sha=None, do_ots=False)


def test_earlier_versions_are_found_in_pages_already_captured_not_by_crawling(
        _data_in_tmp, monkeypatch):
    # discovery must cost aial.ie nothing: the links come out of a capture the
    # sweep already made, and only the pages we do not hold are fetched
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (None, {"ok": False}))
    store = cap.Store(_data_in_tmp / "data")
    _store_page(store)
    fetched = []

    def _fetch(url, **k):
        fetched.append(url)
        body = b"<html><body>an earlier version</body></html>"
        return body, {"url": url, "final_url": url, "status_code": 200,
                      "content_type": "text/html", "etag": None,
                      "last_modified": None, "content_length": str(len(body)),
                      "fetched_at": "2026-08-29T15:00:00Z"}

    monkeypatch.setattr(cap, "fetch", _fetch)
    stored, errors = H.harvest_version_pages(store)
    assert (stored, errors) == (2, 0)
    assert sorted(fetched) == [PAGE_URL + "version-2026-01-12",
                               PAGE_URL + "version-2026-03-30"]
    # a page here never changes once published: a second run must fetch nothing
    fetched.clear()
    store2 = cap.Store(_data_in_tmp / "data")
    assert H.harvest_version_pages(store2) == (0, 0)
    assert fetched == []


def test_a_published_version_is_filed_under_the_model_it_belongs_to(_data_in_tmp,
                                                                    monkeypatch):
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (None, {"ok": False}))
    store = cap.Store(_data_in_tmp / "data")
    _store_page(store)
    monkeypatch.setattr(cap, "fetch", lambda url, **k: (
        b"<html>v</html>", {"url": url, "final_url": url, "status_code": 200,
                            "content_type": "text/html", "etag": None,
                            "last_modified": None, "content_length": "14",
                            "fetched_at": "2026-08-29T15:00:00Z"}))
    H.harvest_version_pages(store)
    mans = [json.loads(m.read_text(encoding="utf-8"))
            for m in (_data_in_tmp / "data" / "captures").glob("*/*/*/manifest.json")]
    versions = [m for m in mans if m.get("aial_published_version")]
    assert len(versions) == 2
    assert all(m["source_id"] == "swiss-ai-initiative/apertus" for m in versions)
    assert sorted(m["aial_published_version"] for m in versions) == ["2026-01-12",
                                                                    "2026-03-30"]


def test_two_evaluations_with_identical_bytes_both_keep_their_history(_data_in_tmp,
                                                                     monkeypatch):
    # a newly added eval is often a byte-identical copy of the template, and a
    # rename lands the same blob under a new name: dedupe on the blob alone would
    # silently drop the second file's entire history
    _registry(_data_in_tmp, [])
    monkeypatch.setattr(H, "commits", lambda tok: [
        {"sha": "c1", "commit": {"author": {"date": "2026-03-01T00:00:00Z"}}}])
    monkeypatch.setattr(H, "tree_at", lambda sha, tok: {"a.yaml": "sameblob",
                                                        "b.yaml": "sameblob"})
    monkeypatch.setattr(H, "token", lambda: "")
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (None, {"ok": False}))
    monkeypatch.setattr(H, "PAUSE_S", 0)
    monkeypatch.setattr(cap, "fetch", lambda url, **k: (A_YAML, {
        "url": url, "final_url": url, "status_code": 200, "content_type": "text/plain",
        "etag": None, "last_modified": None, "content_length": "1",
        "fetched_at": "2026-08-29T14:00:00Z"}))
    H.main()
    paths = {json.loads(m.read_text(encoding="utf-8"))["git_path"]
             for m in (_data_in_tmp / "data" / "captures").glob("*/*/*/manifest.json")}
    assert paths == {"evals/a.yaml", "evals/b.yaml"}, paths


def test_an_evaluation_resolves_by_the_document_it_graded_when_both_names_changed(
        _data_in_tmp):
    # AIAL's "Sintesi" is the ledger's "FastwebMIIA": neither the filename nor the
    # model name matches, but both point at the same published summary
    _registry(_data_in_tmp, [{
        "id": "fastweb/fastwebmiia", "provider": "Fastweb", "model": "FastwebMIIA",
        "targets": [{"kind": "provider-live",
                     "url": "https://www.fastweb.it/sintesi%20contenuti.pdf"}]}])
    idx = H.registry_index()
    text = NL.join([
        'model_name: "Sintesi"',
        'organization: "FastWeb"',
        'public_summary_link: "https://www.fastweb.it/sintesi%20contenuti.pdf?x=1"',
    ])
    assert H.resolve(text, "sintesi.yaml", idx) == "fastweb/fastwebmiia"


def test_a_run_stops_on_the_clock_and_leaves_the_rest_for_the_next_run(_data_in_tmp,
                                                                       monkeypatch):
    # the CI step has a timeout; a count-only bound would let the job be KILLED
    # mid-write instead of stopping cleanly
    _registry(_data_in_tmp, [])
    monkeypatch.setattr(H, "commits", lambda tok: [
        {"sha": f"c{i}", "commit": {"author": {"date": f"2026-03-0{i}T00:00:00Z"}}}
        for i in range(1, 5)])
    monkeypatch.setattr(H, "tree_at",
                        lambda sha, tok: {f"{sha}.yaml": f"blob-{sha}"})
    monkeypatch.setattr(H, "token", lambda: "")
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (None, {"ok": False}))
    monkeypatch.setattr(H, "BUDGET_S", -1)          # the budget is already spent
    monkeypatch.setattr(H, "PAUSE_S", 0)
    calls = []
    monkeypatch.setattr(cap, "fetch", lambda url, **k: (calls.append(url), (A_YAML, {
        "url": url, "final_url": url, "status_code": 200, "content_type": "text/plain",
        "etag": None, "last_modified": None, "content_length": "1",
        "fetched_at": "2026-08-29T14:00:00Z"}))[1])
    assert H.main() == 0
    assert calls == [], "the budget did not stop the run before any fetch"


def test_an_evaluation_the_ledger_cannot_place_still_describes_itself(_data_in_tmp,
                                                                      monkeypatch):
    # filed under AIAL's source because the model is untracked, it must NOT
    # inherit that source's provider and model: an evaluation of Claude Fable 5
    # published as the model "GPAI Training Transparency tracker" is a false
    # statement about both organisations
    _registry(_data_in_tmp, [{"id": "aial/tracker",
                              "provider": "AI Accountability Lab (AIAL)",
                              "model": "GPAI Training Transparency tracker",
                              "targets": []}])
    monkeypatch.setattr(H, "commits", lambda tok: [
        {"sha": "c1", "commit": {"author": {"date": "2026-03-01T00:00:00Z"}}}])
    monkeypatch.setattr(H, "tree_at", lambda sha, tok: {"claude-fable-5.yaml": "b1"})
    monkeypatch.setattr(H, "token", lambda: "")
    monkeypatch.setattr(H, "PAUSE_S", 0)
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (None, {"ok": False}))
    body = (NL.join(['model_name: "Claude Fable 5"', 'organization: "Anthropic"',
                     "S1:", "  D1:", "    score: 9"]) + NL).encode()
    monkeypatch.setattr(cap, "fetch", lambda url, **k: (body, {
        "url": url, "final_url": url, "status_code": 200, "content_type": "text/plain",
        "etag": None, "last_modified": None, "content_length": "1",
        "fetched_at": "2026-08-29T14:00:00Z"}))
    H.main()
    m = json.loads(next((_data_in_tmp / "data" / "captures").glob("*/*/*/manifest.json"))
                   .read_text(encoding="utf-8"))
    assert m["source_id"] == "aial/tracker", "filing is unchanged"
    assert m["provider"] == "Anthropic" and m["model"] == "Claude Fable 5"


def test_a_renamed_model_resolves_through_a_registry_alias(_data_in_tmp):
    # the ledger carries a renamed model as "Old / New"; each side alone is a
    # name it claims, so matching one is a match and not a guess
    _registry(_data_in_tmp, [{"id": "anthropic/claude-mythos-5-fable-5",
                              "provider": "Anthropic",
                              "model": "Claude Mythos 5 / Claude Fable 5",
                              "targets": []}])
    idx = H.registry_index()
    text = NL.join(['model_name: "Claude Fable 5"', 'organization: "Anthropic"'])
    assert H.resolve(text, "claude-fable-5.yaml", idx) == "anthropic/claude-mythos-5-fable-5"


def test_a_state_older_than_one_already_held_is_refused_not_appended(_data_in_tmp,
                                                                     monkeypatch):
    # harvesting is oldest-first, so an older state means an earlier run skipped
    # it; appending would make prior_sha256 assert a succession that never was
    _registry(_data_in_tmp, [])
    monkeypatch.setattr(H, "token", lambda: "")
    monkeypatch.setattr(H, "PAUSE_S", 0)
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (None, {"ok": False}))
    seq = {"n": 0}
    bodies = [b"newer state" + NL.encode(), b"older state" + NL.encode()]
    monkeypatch.setattr(cap, "fetch", lambda url, **k: (
        bodies[min(seq["n"], 1)], {"url": url, "final_url": url, "status_code": 200,
                                   "content_type": "text/plain", "etag": None,
                                   "last_modified": None, "content_length": "1",
                                   "fetched_at": "2026-08-29T14:00:00Z"}))
    monkeypatch.setattr(H, "commits", lambda tok: [
        {"sha": "c2", "commit": {"author": {"date": "2026-05-01T00:00:00Z"}}}])
    monkeypatch.setattr(H, "tree_at", lambda sha, tok: {"x.yaml": "newblob"})
    assert H.main() == 0
    seq["n"] = 1
    monkeypatch.setattr(H, "commits", lambda tok: [
        {"sha": "c1", "commit": {"author": {"date": "2026-03-01T00:00:00Z"}}}])
    monkeypatch.setattr(H, "tree_at", lambda sha, tok: {"x.yaml": "oldblob"})
    assert H.main() == 1, "an out-of-order state was accepted silently"
    mans = [json.loads(m.read_text(encoding="utf-8"))
            for m in (_data_in_tmp / "data" / "captures").glob("*/*/*/manifest.json")]
    assert len(mans) == 1 and mans[0]["git_blob_sha"] == "newblob"


# --- the graded snapshots that only the history knows about ------------------

def _eval_capture(store, sid, provider, model, archive_name, kind="aial-eval"):
    body = (NL.join([f'model_name: "{model}"', f'organization: "{provider}"',
                     f'archive_file_name: "{archive_name}"']) + NL)
    cap.store_new_version(
        store, source_id=sid, provider=provider, model=model, kind=kind,
        tslug=cap.target_slug(kind, "https://x/" + archive_name),
        event_url="https://x/e", raw=body.encode(),
        meta={"url": "https://x/e", "final_url": "https://x/e", "status_code": 200,
              "content_type": "text/plain", "etag": None, "last_modified": None,
              "content_length": "1", "fetched_at": "2026-08-29T14:00:00Z"},
        ext=".yaml", text=body, notes=[], text_sha=None, do_ots=False)


def test_a_snapshot_named_only_by_an_old_grade_is_still_wanted(_data_in_tmp,
                                                               monkeypatch):
    # when AIAL re-grades against a newer document the old filename stops being
    # referenced by the current file — but that document is the one the earlier
    # grade was given to, and may be the only surviving copy
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (None, {"ok": False}))
    store = cap.Store(_data_in_tmp / "data")
    _eval_capture(store, "microsoft/phi-4", "Microsoft", "Phi-4",
                  "Phi_4_2026_08_01.pdf")
    _eval_capture(store, "microsoft/phi-4", "Microsoft", "Phi-4",
                  "Phi 4 -- 2026_02_05.pdf", kind="aial-eval-history")
    names = H.named_archives()
    assert set(names) == {"Phi_4_2026_08_01.pdf", "Phi 4 -- 2026_02_05.pdf"}
    assert names["Phi 4 -- 2026_02_05.pdf"][0] == "microsoft/phi-4"


def test_a_value_that_is_not_a_filename_is_never_requested(_data_in_tmp,
                                                           monkeypatch):
    # AIAL's field is free text and has carried a bare slug; requesting it would
    # 404 on every run forever
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (None, {"ok": False}))
    store = cap.Store(_data_in_tmp / "data")
    _eval_capture(store, "openai/gpt-5-5", "OpenAI", "GPT-5.5", "gpt-5-5")
    assert H.named_archives() == {}


def test_a_snapshot_aial_never_published_does_not_redden_the_run_forever(
        _data_in_tmp, monkeypatch):
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (None, {"ok": False}))
    monkeypatch.setattr(H, "PAUSE_S", 0)
    store = cap.Store(_data_in_tmp / "data")
    _eval_capture(store, "microsoft/phi-4", "Microsoft", "Phi-4", "Gone.pdf")

    def _gone(url, **k):
        raise cap.PermanentFetchError("not found", status_code=404)

    monkeypatch.setattr(cap, "fetch", _gone)
    stored, errors = H.harvest_named_archives(store)
    assert (stored, errors) == (0, 0), "a 404 was counted as a failure of ours"


def test_a_snapshot_already_held_is_not_fetched_again(_data_in_tmp, monkeypatch):
    monkeypatch.setattr(cap, "ots_stamp", lambda d: (None, {"ok": False}))
    monkeypatch.setattr(H, "PAUSE_S", 0)
    store = cap.Store(_data_in_tmp / "data")
    _eval_capture(store, "microsoft/phi-4", "Microsoft", "Phi-4", "Held.pdf")
    url = H.ARCHIVE_BASE + "Held.pdf"
    cap.store_new_version(
        store, source_id="microsoft/phi-4", provider="Microsoft", model="Phi-4",
        kind="aial-archive", tslug=cap.target_slug("aial-archive", url),
        event_url=url, raw=b"%PDF-1.4 held",
        meta={"url": url, "final_url": url, "status_code": 200,
              "content_type": "application/pdf", "etag": None,
              "last_modified": None, "content_length": "13",
              "fetched_at": "2026-08-29T14:00:00Z"},
        ext=".pdf", text="held", notes=[], text_sha=None, do_ots=False)
    calls = []
    monkeypatch.setattr(cap, "fetch", lambda url, **k: calls.append(url))
    assert H.harvest_named_archives(store) == (0, 0)
    assert calls == []


def test_a_graded_snapshot_is_a_providers_document_and_is_not_withheld():
    # AIAL mirrors the PROVIDER's mandated disclosure; withholding it would hide
    # the very thing this archive exists to hold
    assert H.ARCHIVE_KIND == "aial-archive"
    assert H.ARCHIVE_KIND not in cap.RESTRICTED_KINDS

