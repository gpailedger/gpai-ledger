"""crawler/decisions.py — the operator's decision queue. A comment on a public
repository reaches this code, and an approval adds a target to a public
accountability record, so the trust boundary and the command parsing are tested
harder than the happy path.
"""
import json
from pathlib import Path

import pytest

from conftest import load_module

ROOT = Path(__file__).resolve().parent.parent
DEC = load_module(str(ROOT / "crawler" / "decisions.py"), "decisions_mod")
BR = load_module(str(ROOT / "crawler" / "build_registry.py"), "build_registry_dec")

CAND = {"source_id": "prov/model", "provider": "Prov", "model": "Model",
        "url": "https://prov.example/doc.pdf", "why": "only 1 template marker"}


@pytest.fixture(autouse=True)
def _tmp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(DEC, "PENDING", tmp_path / "pending.json")
    monkeypatch.setattr(DEC, "DECISIONS", tmp_path / "decisions.json")
    return tmp_path


def _queue(cand=None):
    pend = DEC.add_candidates([cand or CAND])
    DEC.save(DEC.PENDING, pend)
    return next(iter(pend))


# --- the queue ---

def test_a_candidate_is_queued_once_and_never_re_asked():
    k = _queue()
    again = DEC.add_candidates([CAND])
    assert list(again) == [k]
    DEC.save(DEC.DECISIONS, {k: {"decision": "reject"}})
    assert DEC.add_candidates([CAND], pending={}) == {}    # already decided


def test_a_candidate_without_a_url_or_a_name_is_not_queued():
    assert DEC.add_candidates([{"provider": "P"}]) == {}
    assert DEC.add_candidates([{"url": "https://x/y.pdf"}]) == {}


def test_the_issue_body_carries_the_key_and_the_reply_syntax():
    k = _queue()
    body = DEC.issue_body(k, json.loads(DEC.PENDING.read_text(encoding="utf-8"))[k])
    assert f"<!-- candidate:{k} -->" in body
    assert "/approve" in body and "/reject" in body
    assert "https://prov.example/doc.pdf" in body


# --- the trust boundary ---

@pytest.mark.parametrize("assoc", ["", "NONE", "CONTRIBUTOR", "COLLABORATOR",
                                   "MEMBER", "FIRST_TIME_CONTRIBUTOR"])
def test_only_the_repository_owner_can_decide(assoc):
    k = _queue()
    DEC.save(DEC.PENDING, {**json.loads(DEC.PENDING.read_text(encoding="utf-8")),
                           k: {**json.loads(DEC.PENDING.read_text(encoding="utf-8"))[k],
                               "issue": 7}})
    assert DEC.apply_decision(7, "/approve", assoc, "stranger") == 0
    assert not DEC.DECISIONS.exists()          # nothing recorded


def _queued_on_issue(n=7, cand=None):
    k = _queue(cand)
    pend = json.loads(DEC.PENDING.read_text(encoding="utf-8"))
    pend[k]["issue"] = n
    DEC.save(DEC.PENDING, pend)
    return k


def test_an_owner_approval_is_recorded_and_leaves_the_queue():
    k = _queued_on_issue()
    assert DEC.apply_decision(7, "/approve", "OWNER", "owner") == 0
    d = json.loads(DEC.DECISIONS.read_text(encoding="utf-8"))[k]
    assert d["decision"] == "approve" and d["id"] == "prov/model"
    assert json.loads(DEC.PENDING.read_text(encoding="utf-8")) == {}


def test_a_rejection_keeps_the_candidate_from_ever_returning():
    k = _queued_on_issue()
    DEC.apply_decision(7, "/reject not an Art. 53 summary", "OWNER", "owner")
    d = json.loads(DEC.DECISIONS.read_text(encoding="utf-8"))[k]
    assert d["decision"] == "reject" and d["note"] == "not an Art. 53 summary"
    assert DEC.add_candidates([CAND], pending={}) == {}


# --- parsing what arrives by e-mail ---

@pytest.mark.parametrize("body,expected", [
    ("/approve", ("approve", None, "")),
    ("  /approve  ", ("approve", None, "")),
    ("/APPROVE", ("approve", None, "")),
    ("/approve id=acme-ai/acme-2", ("approve", "acme-ai/acme-2", "")),
    ("/reject wrong document", ("reject", None, "wrong document")),
    ("Sure, go ahead.\n/approve\n\nOn Mon someone wrote:", ("approve", None, "")),
    ("no command here", None),
    ("", None),
])
def test_the_command_is_read_out_of_a_real_email_reply(body, expected):
    assert DEC.parse_command(body) == expected


def test_a_quoted_older_instruction_cannot_re_trigger():
    # e-mail clients quote the thread; the first command wins, not the quoted one
    body = "/reject stale link\n\n> On Mon, the bot wrote:\n> /approve\n"
    assert DEC.parse_command(body)[0] == "reject"


def test_a_malformed_source_id_is_refused_without_recording_anything():
    _queued_on_issue()
    assert DEC.apply_decision(7, "/approve id=Not A Valid Id", "OWNER", "o") == 2
    assert not DEC.DECISIONS.exists()


def test_a_comment_on_an_issue_nothing_is_waiting_on_is_ignored():
    _queued_on_issue(7)
    assert DEC.apply_decision(999, "/approve", "OWNER", "o") == 0
    assert not DEC.DECISIONS.exists()


# --- what an approval does to the registry ---

def _aial(tmp_path, names):
    repo = tmp_path / "aial"
    (repo / "evals").mkdir(parents=True, exist_ok=True)
    for n in names:
        (repo / "evals" / f"{n}.yaml").write_text(
            f"model_name: {n}\norganization: Testorg\n", encoding="utf-8")
    return repo


def test_an_approval_adds_a_target_and_publishes_an_existing_source(tmp_path):
    out = tmp_path / "sources.json"
    repo = _aial(tmp_path, ["alpha"])
    dec_file = Path(BR.__file__).parent / "decisions.json"
    dec_file.write_text(json.dumps({"k1": {
        "decision": "approve", "id": "testorg/alpha",
        "url": "https://prov.example/alpha.pdf", "kind": "provider-live",
        "at": "2026-08-29T10:00:00Z", "note": "checked by hand"}}), encoding="utf-8")
    try:
        BR.main(str(repo), out_path=out)
    finally:
        dec_file.unlink()
    s = {x["id"]: x for x in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    alpha = s["testorg/alpha"]
    assert alpha["status"] == "published"
    t = [t for t in alpha["targets"] if t["url"].endswith("alpha.pdf")][0]
    assert "approved by the operator on 2026-08-29" in t["note"]
    assert "checked by hand" in t["note"]


def test_an_approval_can_create_a_source_the_registry_did_not_have(tmp_path):
    out = tmp_path / "sources.json"
    repo = _aial(tmp_path, ["alpha"])
    dec_file = Path(BR.__file__).parent / "decisions.json"
    dec_file.write_text(json.dumps({"k2": {
        "decision": "approve", "id": "acme-ai/acme-2", "provider": "Acme AI",
        "model": "Acme 2", "url": "https://acme.example/summary.pdf",
        "kind": "provider-live", "at": "2026-08-29T10:00:00Z"}}), encoding="utf-8")
    try:
        BR.main(str(repo), out_path=out)
    finally:
        dec_file.unlink()
    s = {x["id"]: x for x in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    assert s["acme-ai/acme-2"]["status"] == "published"
    assert s["acme-ai/acme-2"]["provider"] == "Acme AI"


def test_a_rejection_never_reaches_the_registry(tmp_path):
    out = tmp_path / "sources.json"
    repo = _aial(tmp_path, ["alpha"])
    dec_file = Path(BR.__file__).parent / "decisions.json"
    dec_file.write_text(json.dumps({"k3": {
        "decision": "reject", "id": "testorg/alpha",
        "url": "https://prov.example/no.pdf", "at": "2026-08-29T10:00:00Z"}}),
        encoding="utf-8")
    try:
        BR.main(str(repo), out_path=out)
    finally:
        dec_file.unlink()
    s = {x["id"]: x for x in json.loads(out.read_text(encoding="utf-8"))["sources"]}
    assert all(not t["url"].endswith("no.pdf") for t in s["testorg/alpha"]["targets"])
