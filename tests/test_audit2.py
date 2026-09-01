"""Regression tests for the 23 Aug 2026 production audit: site counts and
wording that must state only what the data supports, per-capture proofs,
the consent-chrome dedupe, the OTS upgrade guard and the C5 bundle check."""
import inspect
import datetime
import json
import sys

import pytest

NL = chr(10)

import build
import capture as cap
import upgrade_ots
import verify_corpus
from test_store_and_sweep import _events, _meta, _run_wb, _seq, _src, _write_registry


SHA_A = "a" * 64
SHA_B = "b" * 64


def _row(sha, text_sha=None, stored="raw.pdf", retired=False):
    return {"sha": sha, "text_sha": text_sha, "stored_as": stored, "url": "https://ex.org/d",
            "managed": None, "retired": retired}


# --- distinct document count (status / index) --------------------------------

def test_byte_identical_copies_count_as_one_document_version():
    rows = [_row(SHA_A), _row(SHA_A), _row(SHA_B)]
    assert build.distinct_documents(rows, set()) == 2


def test_text_identical_recaptures_count_once_and_retired_rows_never():
    rows = [_row(SHA_A, "t1"), _row(SHA_B, "t1"), _row("c" * 64, "t2", retired=True)]
    assert build.distinct_documents(rows, set()) == 1


# --- status 'Last checked' ----------------------------------------------------

def test_status_checked_uses_own_event_then_sweep_date_then_dash():
    checked = {"x/y": "2026-08-21T05:00:00Z", "z/w": "2026-08-23T05:00:00Z"}
    assert build.status_checked({"id": "x/y", "targets": [{"url": "u"}]}, checked) == "2026-08-21"
    assert build.status_checked({"id": "q/r", "targets": [{"url": "u"}]}, checked) == "2026-08-23"
    assert build.status_checked({"id": "q/r", "targets": []}, checked) == "—"


# --- a missing model with no known location is not 'checked' -----------------

def _missing(targets):
    return {"id": "prov/no-loc", "model": "NoLoc", "provider": "Prov", "status": "missing",
            "targets": targets, "aial": {}}


def test_no_location_model_page_does_not_claim_daily_checks():
    body, _title = build.render_model_page(_missing([]), [], {"other/src": "2026-08-23T05:00:00Z"})
    assert "No candidate location for a summary is known" in body
    assert "re-checked on the daily schedule" not in body
    assert "tracks the locations where one would appear" not in body
    assert "no location is known" in build.model_page_desc(_missing([]), [], set())


def test_missing_model_with_targets_keeps_the_checked_line():
    body, _ = build.render_model_page(_missing([{"url": "https://ex.org/p", "kind": "watch-page"}]),
                                      [], {"other/src": "2026-08-23T05:00:00Z"})
    assert "re-checked on the daily schedule" in body
    assert "Tracked daily" in build.model_page_desc(_missing([{"url": "u"}]), [], set())


# --- prior_cell: a pruned predecessor is described by its own event -----------

def test_prior_cell_tool_prune_keeps_the_rule_wording():
    ev = {"via": "prune_capture", "reason": "html re-capture", "ts": "2026-08-21T10:00:00Z"}
    out = build.prior_cell(SHA_A, set(), pruned={SHA_A: ev})
    assert "by the prune rule" in out


def test_prior_cell_curation_prune_shows_the_recorded_reason_not_the_rule():
    ev = {"reason": "intermediate capture from the renderer iteration", "ts": "2026-08-18T17:30:00Z"}
    out = build.prior_cell(SHA_A, set(), pruned={SHA_A: ev})
    assert "pre-launch curation on 2026-08-18" in out
    assert "intermediate capture from the renderer iteration" in out
    assert "by the prune rule" not in out and "not in this repository's history" in out


def test_prior_cell_unknown_removal_makes_no_identity_claim():
    out = build.prior_cell(SHA_A, set(), pruned={})
    assert "no prune event names it" in out and "content-identical" not in out


def test_prior_cell_accepts_a_three_field_prior_ref():
    out = build.prior_cell(SHA_A, {SHA_A}, prior_ref=("20260811T100000Z", "2026-08-11", SHA_A))
    assert "href='../20260811T100000Z/'" in out


# --- version page: own proof, honest wording, repack detection ----------------

def _manifest(sha=SHA_A, prior=None, ots_at="2026-08-20T10:58:00Z"):
    return {"stored_as": "raw.pdf", "sha256": sha, "size_bytes": 3, "text_sha256": "t",
            "target_kind": "provider-live", "prior_sha256": prior,
            "http": {"url": "https://ex.org/doc.pdf", "fetched_at": "2026-08-20T10:57:00Z",
                     "status_code": 200},
            "ots": {"ok": True, "at": ots_at}, "extraction_notes": []}


SRC = {"id": "prov/model", "model": "Model", "provider": "Prov", "status": "published"}


def test_version_page_links_its_own_proof_with_upper_bound_wording(monkeypatch):
    monkeypatch.setattr(build, "SHA_FIRST", {SHA_A: ("20260811T100000Z", "other/src")})
    monkeypatch.setattr(build, "REPACKED_SHAS", set())
    out = build.render_version_page(SRC, _manifest(), "20260820T105700Z", {SHA_A}, "text",
                                    True, True, None)
    assert f"{SHA_A}.pdf.20260820T105700Z.ots" in out
    assert "proves the capture time" not in out
    assert "no later than the attestation time" in out
    assert "identical bytes were first archived on 11 Aug 2026" in out
    assert "ledger/other/src/v/20260811T100000Z/" in out


def test_earliest_capture_carries_no_earlier_note(monkeypatch):
    monkeypatch.setattr(build, "SHA_FIRST", {SHA_A: ("20260820T105700Z", "prov/model")})
    monkeypatch.setattr(build, "REPACKED_SHAS", set())
    out = build.render_version_page(SRC, _manifest(), "20260820T105700Z", {SHA_A}, "text",
                                    True, True, None)
    assert "first archived on" not in out


def test_scope_repacked_bundle_is_detected_from_the_event_log(monkeypatch):
    monkeypatch.setattr(build, "SHA_FIRST", {})
    monkeypatch.setattr(build, "REPACKED_SHAS", {SHA_B})
    m = _manifest(prior=SHA_B, ots_at="2026-08-19T20:25:31Z")
    m["stored_as"] = "raw.zip"
    out = build.render_version_page(SRC, m, "20260819T202531Z", {SHA_A}, "text", True, True, None)
    assert "Original bundle" in out and SHA_B in out
    assert "assembled from the fetched bundle" in out
    assert "no later than the stamp date" in out


def test_own_ots_name_is_unique_per_capture():
    assert build.own_ots_name(_manifest(), "20260820T105700Z") == f"{SHA_A}.pdf.20260820T105700Z.ots"
    assert build.own_ots_name(_manifest(), "20260821T000000Z") != build.own_ots_name(_manifest(), "20260820T105700Z")


# --- consent chrome: root fix + dedupe against every retained version ---------

def test_consent_strip_matches_partner_banners_and_reject_optional():
    assert "'use cookies'" in cap.CONSENT_STRIP_JS
    assert "Reject optional" in inspect.getsource(cap._fetch_rendered_impl)


def test_html_text_equal_to_any_retained_version_does_not_mint(tmp_path, monkeypatch):
    import run_capture as rc_mod
    url = "https://ex.org/page.html"
    reg = _write_registry(tmp_path, [_src("prov/model", url)])
    data_root = tmp_path / "data"
    monkeypatch.setattr(rc_mod, "RECHECK_DELAY", 0)
    state_a = (b"<html><p>summary text</p><p>We and our partners use cookies</p></html>",
               _meta(url, "text/html"))
    state_b = (b"<html><p>summary text</p></html>", _meta(url, "text/html"))
    state_a2 = (b"<html><!-- nonce --><p>summary text</p><p>We and our partners use cookies</p></html>",
                _meta(url, "text/html"))
    for body in (state_a, state_b):
        monkeypatch.setattr(cap, "fetch", _seq([body]))
        assert _run_wb(monkeypatch, reg, data_root) == 0
    monkeypatch.setattr(cap, "fetch", _seq([state_a2]))
    assert _run_wb(monkeypatch, reg, data_root) == 0
    state = json.loads((data_root / "state.json").read_text(encoding="utf-8"))
    (entry,) = state.values()
    assert len(entry["versions"]) == 2
    last = _events(data_root)[-1]
    assert last["outcome"] == "unchanged-content"
    assert last["matches"] == entry["versions"][0]["dir"]


# --- OTS upgrade: a partial calendar answer keeps the pending marker ---------

def _serialized(ts):
    from opentimestamps.core.serialize import BytesSerializationContext
    ctx = BytesSerializationContext()
    ts.serialize(ctx)
    return ctx.getbytes()


class _Resp:
    def __init__(self, content):
        self.status_code, self.content = 200, content


def test_upgrade_keeps_pending_marker_until_a_bitcoin_attestation_is_reached(monkeypatch):
    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
    from opentimestamps.core.timestamp import Timestamp
    uri = "https://alice.btc.calendar.opentimestamps.org"
    msg = b"m" * 32

    partial = Timestamp(msg)
    partial.attestations.add(PendingAttestation("https://bob.btc.calendar.opentimestamps.org"))
    monkeypatch.setattr(upgrade_ots.requests, "get", lambda *a, **k: _Resp(_serialized(partial)))
    ts = Timestamp(msg)
    ts.attestations.add(PendingAttestation(uri))
    assert upgrade_ots.upgrade_timestamp(ts) is True
    assert any(isinstance(a, PendingAttestation) and str(a.uri).endswith("opentimestamps.org")
               for a in ts.attestations)
    assert not upgrade_ots.is_anchored(ts)

    complete = Timestamp(msg)
    complete.attestations.add(BitcoinBlockHeaderAttestation(961998))
    monkeypatch.setattr(upgrade_ots.requests, "get", lambda *a, **k: _Resp(_serialized(complete)))
    ts = Timestamp(msg)
    ts.attestations.add(PendingAttestation(uri))
    assert upgrade_ots.upgrade_timestamp(ts) is True
    assert upgrade_ots.is_anchored(ts)
    assert not any(isinstance(a, PendingAttestation) for a in ts.attestations)


# --- verify_corpus C5: a bundle's served text needs a hash claim too ----------

def test_c5_fails_a_zip_bundle_whose_served_text_has_no_hash(tmp_path, monkeypatch):
    root = tmp_path / "data"
    d = root / "captures/prov__model/provider-live-aaaa1111/20260811T100000Z"
    d.mkdir(parents=True)
    raw = b"PK\x05\x06" + b"\0" * 18
    (d / "raw.zip").write_bytes(raw)
    (d / "extracted.txt").write_text("served text", encoding="utf-8")
    sha = cap.sha256_hex(raw)
    (d / "manifest.json").write_text(json.dumps({
        "stored_as": "raw.zip", "sha256": sha, "size_bytes": len(raw), "text_sha256": None,
        "ots": {"ok": False}, "http": {"url": "https://ex.org/b.zip", "status_code": 200},
        "source_id": "prov/model", "target_kind": "provider-live", "extraction_notes": []}),
        encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"prov/model::provider-live-aaaa1111": {
        "versions": [{"sha256": sha, "dir": "captures/prov__model/provider-live-aaaa1111/20260811T100000Z"}],
        "last_sha256": sha, "last_text_sha256": None,
        "last_capture": "captures/prov__model/provider-live-aaaa1111/20260811T100000Z"}}), encoding="utf-8")
    # a realistic corpus has swept recently; an empty log is itself a C11 failure
    (root / "events.jsonl").write_text(json.dumps({
        "ts": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outcome": "unchanged"}) + NL, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["verify_corpus.py", "--data-root", str(root)])
    verify_corpus.FAILS.clear()
    verify_corpus.WARNS.clear()
    rc = verify_corpus.main()
    assert rc == 1
    assert any(c == "C5" and "unverifiable" in msg for c, _p, msg in verify_corpus.FAILS)
    verify_corpus.FAILS.clear()


@pytest.mark.parametrize("wf", [".github/workflows/ledger.yml", ".github/workflows/hunt.yml"])
def test_push_retry_loops_pass_shellcheck_unused_variable_rule(wf):
    from pathlib import Path
    text = (Path(build.__file__).resolve().parent.parent / wf).read_text(encoding="utf-8")
    assert "for i in 1 2 3; do" not in text and "for _ in 1 2 3; do" in text


def test_c9_warns_on_a_proof_still_pending_after_a_week(tmp_path, monkeypatch):
    import hashlib
    from opentimestamps.core.notary import PendingAttestation
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp
    root = tmp_path / "data"
    d = root / "captures/prov__model/provider-live-aaaa1111/20260701T100000Z"
    d.mkdir(parents=True)
    raw = b"%PDF-1.4 old"
    (d / "raw.pdf").write_bytes(raw)
    ts = Timestamp(hashlib.sha256(raw).digest())
    ts.attestations.add(PendingAttestation("https://alice.btc.calendar.opentimestamps.org"))
    ctx = BytesSerializationContext()
    DetachedTimestampFile(OpSHA256(), ts).serialize(ctx)
    (d / "raw.pdf.ots").write_bytes(ctx.getbytes())
    sha = cap.sha256_hex(raw)
    (d / "manifest.json").write_text(json.dumps({
        "stored_as": "raw.pdf", "sha256": sha, "size_bytes": len(raw), "text_sha256": None,
        "ots": {"ok": True, "at": "2026-07-01T10:00:05Z"},
        "http": {"url": "https://ex.org/d.pdf", "status_code": 200},
        "source_id": "prov/model", "target_kind": "provider-live", "extraction_notes": []}),
        encoding="utf-8")
    (root / "state.json").write_text(json.dumps({"prov/model::provider-live-aaaa1111": {
        "versions": [{"sha256": sha, "dir": "captures/prov__model/provider-live-aaaa1111/20260701T100000Z"}],
        "last_sha256": sha, "last_text_sha256": None,
        "last_capture": "captures/prov__model/provider-live-aaaa1111/20260701T100000Z"}}), encoding="utf-8")
    # a realistic corpus has swept recently; an empty log is itself a C11 failure
    (root / "events.jsonl").write_text(json.dumps({
        "ts": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outcome": "unchanged"}) + NL, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["verify_corpus.py", "--data-root", str(root)])
    verify_corpus.FAILS.clear()
    verify_corpus.WARNS.clear()
    assert verify_corpus.main() == 0
    assert any(c == "C9" and "pending" in msg for c, _p, msg in verify_corpus.WARNS)
    verify_corpus.WARNS.clear()


# --- drift ledger: identity-v4, bounded alignment, like-for-like ---------------

def test_ligatures_and_nfd_accents_are_not_edits():
    import analyze_drift
    import unicodedata
    for old, new in (("the ﬁnal ﬂow", "the final flow"),
                     (unicodedata.normalize("NFD", "café résumé"), "café résumé")):
        res = analyze_drift.compare_words(analyze_drift.words_of(old), analyze_drift.words_of(new))
        assert res["identical"] is True, (old, new)


def test_alignment_cost_is_bounded_by_the_changed_region():
    import analyze_drift
    import time
    doc = " ".join(f"word{i}" for i in range(8000))
    edited = doc.replace("word4000", "edited")
    t0 = time.monotonic()
    res = analyze_drift.compare_words(analyze_drift.words_of(doc), analyze_drift.words_of(edited))
    assert time.monotonic() - t0 < 5
    assert res["word_delta"] == 1 and res["moved_words"] == 0 and "alignment" not in res


def test_a_region_too_large_to_align_is_counted_word_by_word(monkeypatch):
    import analyze_drift
    monkeypatch.setattr(analyze_drift, "CHAR_ALIGN_CAP", 10)
    res = analyze_drift.compare_words(analyze_drift.words_of("a b c one two three d"),
                                      analyze_drift.words_of("a b c four five six seven d"))
    assert res["word_delta"] == 4 and "alignment" in res
    assert res["changes"][0]["op"] == "replace"


def test_method_flap_cannot_hide_an_edit(tmp_path, monkeypatch):
    import analyze_drift
    from test_drift_verdicts import run_drift
    # A (consent 1) -> B (consent 2, same text) -> C (consent 1, text EDITED):
    # B->C differs in method, but like for like with A the edit is real
    data = tmp_path / "data"
    captures = []
    for ts, consent, text in (("20260801T060000Z", 1, "alpha beta gamma"),
                              ("20260802T060000Z", 2, "alpha beta gamma"),
                              ("20260803T060000Z", 1, "alpha beta delta")):
        d = data / "captures/prov__model/provider-page-aaaa1111" / ts
        d.mkdir(parents=True)
        (d / "raw.html").write_bytes(f"<p>{text}</p>{ts}".encode())
        (d / "extracted.txt").write_text(text, encoding="utf-8")
        (d / "manifest.json").write_text(json.dumps({
            "stored_as": "raw.html", "sha256": cap.sha256_hex(f"<p>{text}</p>{ts}".encode()),
            "text_sha256": cap.canonical_text_sha(text), "size_bytes": 1,
            "http": {"url": "https://ex.org/p", "rendered": True, "consent_nodes_removed": consent}}),
            encoding="utf-8")
        captures.append((f"captures/prov__model/provider-page-aaaa1111/{ts}",
                         cap.sha256_hex(f"<p>{text}</p>{ts}".encode())))
    (data / "state.json").write_text(json.dumps({"prov/model::provider-page-aaaa1111": {
        "versions": [{"sha256": sha, "dir": d} for d, sha in captures],
        "last_sha256": captures[-1][1], "last_capture": captures[-1][0]}}), encoding="utf-8")
    monkeypatch.setattr(analyze_drift.cap, "extract_text",
                        lambda raw, ext: (raw.decode()[3:].split("</p>")[0], []))
    run_drift(monkeypatch, tmp_path, data, [])
    ledger = json.loads((tmp_path / "root" / "reports" / "version-diffs.json").read_text(encoding="utf-8"))
    ab = ledger[f"prov/model::provider-page-aaaa1111::{captures[0][0]}>{captures[1][0]}"]
    bc = ledger[f"prov/model::provider-page-aaaa1111::{captures[1][0]}>{captures[2][0]}"]
    assert ab["verdict"] == "identical-text"
    assert bc["verdict"] == "changed" and bc["word_delta"] == 1
    assert bc["compared_with"] == captures[0][0] and "like for like" in bc["compared_via"]
    assert bc["same_method_pair"]["from_dir"] == captures[0][0]


# --- a real PDF goes through the extractor (not only the failure path) ---------

def _pdf_with_text(text):
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] "
            "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"]
    stream = f"BT /F1 18 Tf 20 80 Td ({text}) Tj ET"
    objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_pdf_text_extraction_success_path():
    text, notes = cap.extract_text(_pdf_with_text("hello ledger"), ".pdf")
    assert text and "hello ledger" in text
    assert not any("failed" in str(n) for n in notes)


# --- the public diff ledger must not leak words we may not republish ---------

EVAL_DIR = "captures/adobe__adobe-firefly/aial-eval-c70dd1a0/20260829T140000Z"
DOC_DIR = "captures/adobe__adobe-firefly/provider-live-9a31ba20/20260829T140000Z"


def _rec(from_dir, to_dir, marker="WORDS"):
    return {"verdict": "changed", "similarity": 0.9, "word_delta": 4,
            "moved_words": 0, "from_dir": from_dir, "to_dir": to_dir,
            "changes": [{"op": "replace", "old": "was " + marker,
                         "new": "now " + marker}]}


def test_a_capture_dir_names_its_target_kind():
    import capture as cap
    assert cap.kind_of_capture_dir(EVAL_DIR) == "aial-eval"
    assert cap.kind_of_capture_dir(DOC_DIR) == "provider-live"
    assert cap.kind_of_capture_dir(EVAL_DIR.replace("/", chr(92))) == "aial-eval"
    assert cap.kind_of_capture_dir("") == ""


def test_a_restricted_pair_may_not_carry_excerpts():
    import analyze_drift as ad
    assert ad.excerpts_allowed(_rec(DOC_DIR, DOC_DIR)) is True
    # either side is enough to withhold
    assert ad.excerpts_allowed(_rec(EVAL_DIR, EVAL_DIR)) is False
    assert ad.excerpts_allowed(_rec(DOC_DIR, EVAL_DIR)) is False


def test_the_committed_diff_ledger_keeps_the_metrics_and_drops_the_words(tmp_path,
                                                                        monkeypatch):
    # AIAL revises a grade: the ledger may say THAT it changed and by how much,
    # never quote what it said — reports/version-diffs.json is a public file
    import analyze_drift as ad
    out = tmp_path / "version-diffs.json"
    monkeypatch.setattr(ad, "vdiffs_path", lambda: out)
    ad.save_vdiffs({"eval-pair": _rec(EVAL_DIR, EVAL_DIR, "AIALS-GRADE-PROSE"),
                    "doc-pair": _rec(DOC_DIR, DOC_DIR, "PROVIDERS-OWN-PROSE")})
    written = out.read_text(encoding="utf-8")
    assert "AIALS-GRADE-PROSE" not in written, "an evaluation's words were published"
    saved = json.loads(written)
    ev = saved["eval-pair"]
    assert ev["changes"] == [] and ev["changes_withheld"] is True
    assert ev["word_delta"] == 4 and ev["similarity"] == 0.9, "the metrics were lost"
    # a provider's own document is unaffected: its edits are the whole point
    assert saved["doc-pair"]["changes"][0]["new"] == "now PROVIDERS-OWN-PROSE"
    assert "changes_withheld" not in saved["doc-pair"]


TRACKER_DIR = "captures/aial__tracker/watch-page-465bd4ee/20260829T000000Z"


def test_the_diff_ledger_withholds_a_page_restricted_by_its_url_not_its_kind(
        tmp_path, monkeypatch):
    # AIAL's tracker root publishes the full grade table under the ordinary
    # watch-page kind: a gate that saw only the kind would republish, in a
    # committed report file, exactly what the site withholds
    import analyze_drift as ad
    (tmp_path / TRACKER_DIR).mkdir(parents=True)
    url = "https://aial.ie/research/gpai-training-transparency/"
    (tmp_path / TRACKER_DIR / "manifest.json").write_text(json.dumps({
        "target_kind": "watch-page", "http": {"url": url, "final_url": url}}),
        encoding="utf-8")
    monkeypatch.setattr(ad, "DATA", tmp_path)
    monkeypatch.setattr(ad, "_RESTRICTED_DIR_CACHE", {})
    rec = _rec(TRACKER_DIR, TRACKER_DIR, "AIALS-GRADE-TABLE")
    assert ad.excerpts_allowed(rec) is False
    out = tmp_path / "vd.json"
    monkeypatch.setattr(ad, "vdiffs_path", lambda: out)
    ad.save_vdiffs({"k": rec})
    assert "AIALS-GRADE-TABLE" not in out.read_text(encoding="utf-8")
    saved = json.loads(out.read_text(encoding="utf-8"))["k"]
    assert saved["changes_withheld"] is True and saved["word_delta"] == 4


def test_an_ordinary_page_on_the_same_host_still_carries_its_excerpts(tmp_path,
                                                                      monkeypatch):
    # the rule is the URL, not the host: AIAL's mirrors of PROVIDER documents are
    # the providers' own mandated disclosures and must stay fully in the record
    import analyze_drift as ad
    d = "captures/adobe__adobe-firefly/aial-archive-c70dd1a0/20260811T000000Z"
    (tmp_path / d).mkdir(parents=True)
    url = "https://aial.ie/research/gpai-training-transparency/archive/Adobe.pdf"
    (tmp_path / d / "manifest.json").write_text(json.dumps({
        "target_kind": "aial-archive", "http": {"url": url, "final_url": url}}),
        encoding="utf-8")
    monkeypatch.setattr(ad, "DATA", tmp_path)
    monkeypatch.setattr(ad, "_RESTRICTED_DIR_CACHE", {})
    assert ad.excerpts_allowed(_rec(d, d, "PROVIDER-PROSE")) is True

