"""The operator's decision queue: candidates a machine cannot judge, asked and
answered by e-mail, and applied to the registry by the pipeline.

Some findings can be verified automatically and go straight into the ledger
(crawler/probe_missing.py does that). The rest need a person: a document that
may or may not be an Article 53(1)(d) summary, or a company nobody has assessed,
whose source id becomes a permanent public URL the site promises never to
rename. Those land here.

The loop, with no new credentials and nothing new to remember:

  propose   RETIRED: read reports/unattributed-leads.json and open ONE GitHub
            issue per candidate,
            labelled "decision". GitHub e-mails the repository owner.
  (reply)   the owner answers that e-mail. GitHub turns the reply into an issue
            comment. The words that count:
                /approve                     track it as proposed
                /approve id=org/model        track it under this id instead
                /reject  reason...           never propose this candidate again
  apply     .github/workflows/decisions.yml runs this on the comment, records the
            answer in crawler/decisions.json, and commits it.
  (merge)   build_registry.py turns an approved candidate into a registry target
            on the next refresh, and the sweep then captures the document with a
            hash, an OpenTimestamps proof and a Wayback witness.

Approval is not evidence. It only says "this is worth tracking"; what the site
eventually asserts still rests on the document the crawler fetches for itself.

Only the repository owner's comments are obeyed — checked here as well as in the
workflow, because this writes to a public accountability record.

  python crawler/decisions.py propose
  python crawler/decisions.py apply --issue 12 --author-association OWNER \\
                                    --comment "/approve id=acme-ai/acme-2"
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import requests

import capture as cap

HERE = Path(__file__).resolve().parent
# Named for what it is since the approval queue was retired (31 Aug 2026):
# documents the hunt fetched but would not attribute on its own. Nothing consumes
# it — it is a record of what was found and declined, kept because AIAL is one
# small project and a ledger that only mirrors them would have nowhere to start
# if they stopped. It lives under reports/ because it is output, not configuration.
PENDING = HERE.parent / "reports" / "unattributed-leads.json"
DECISIONS = HERE / "decisions.json"
API = "https://api.github.com"
LABEL = "decision"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9.-]*$")
# only these people may decide; the workflow gates on the same value
TRUSTED_ASSOCIATIONS = ("OWNER",)


def key_of(cand: dict) -> str:
    """A stable id for a candidate, so the same finding is never asked twice."""
    basis = f"{cand.get('source_id') or ''}|{cand.get('provider') or ''}|" \
            f"{cand.get('model') or ''}|{cand.get('url') or ''}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"WARNING: {path.name} is not valid JSON; treating it as empty", flush=True)
        return {}
    return data if isinstance(data, dict) else {}


def save(path: Path, data: dict) -> None:
    cap.atomic_write_text(path, json.dumps(dict(sorted(data.items())), indent=2,
                                           ensure_ascii=False))


def add_candidates(cands, pending=None) -> dict:
    """Merge findings into the queue. A candidate already queued, or already
    decided, is never added again."""
    pending = load(PENDING) if pending is None else pending
    decided = load(DECISIONS)
    for c in cands:
        k = key_of(c)
        if k in pending or k in decided:
            continue
        if not (c.get("url") and (c.get("model") or c.get("source_id"))):
            continue
        pending[k] = {"provider": c.get("provider"), "model": c.get("model"),
                      "source_id": c.get("source_id"), "url": c["url"],
                      "kind": c.get("kind", "provider-live"),
                      "why": c.get("why") or c.get("note") or "",
                      "classification": c.get("classification", ""),
                      "first_seen": cap.utc_now(), "issue": None}
    return pending


def issue_body(k: str, c: dict, notify: str = "") -> str:
    known = ("It names a source this ledger already tracks: "
             f"`{c['source_id']}`." if c.get("source_id") else
             "**No source id yet** — approving creates one, and a source id "
             "becomes a permanent public URL that this project promises never to "
             "rename, so please check the proposed id reads correctly.")
    proposed = c.get("source_id") or "<provider-slug>/<model-slug>"
    return (
        f"A candidate was found that the pipeline will not decide by itself.\n\n"
        f"| | |\n|---|---|\n"
        f"| Provider | {c.get('provider') or '—'} |\n"
        f"| Model | {c.get('model') or '—'} |\n"
        f"| Document | {c['url']} |\n"
        f"| Proposed id | `{proposed}` |\n"
        f"| How it was seen | {c.get('classification') or 'unclassified'} |\n\n"
        f"**Why this needs you:** {c.get('why') or 'it did not clear the automatic checks'}\n\n"
        f"{known}\n\n"
        f"---\n\n"
        f"**Reply to this e-mail** with one line:\n\n"
        f"```\n/approve\n```\n"
        f"or, to file it under a different id:\n\n"
        f"```\n/approve id={proposed}\n```\n"
        f"or\n\n"
        f"```\n/reject not an Article 53(1)(d) summary\n```\n\n"
        f"Approving only means *track this*. The crawler still fetches the "
        f"document itself and stores it with a hash and a timestamp proof before "
        f"the site asserts anything about it.\n\n"
        # GitHub never notifies you about your OWN actions, so this issue has to
        # be opened by the workflow's bot identity; the mention then reaches the
        # owner whatever the repository's watch setting is
        + (f"/cc @{notify}\n\n" if notify else "")
        + f"<!-- candidate:{k} -->\n")


def gh(method: str, path: str, token: str, **kw):
    r = requests.request(method, API + path, timeout=30, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"}, **kw)
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:200]}")
    return r.json()


def propose(repo: str, token: str, limit: int = 10) -> int:
    """Open an issue for every queued candidate that has none yet."""
    pending = load(PENDING)
    opened, failed = 0, 0
    for k, c in sorted(pending.items()):
        if c.get("issue") or opened >= limit:
            continue
        title = (f"Decide: {c.get('model') or c.get('source_id')} "
                 f"({c.get('provider') or 'unknown provider'})")
        try:
            issue = gh("POST", f"/repos/{repo}/issues", token,
                       json={"title": title[:200],
                             "body": issue_body(k, c, notify=repo.split("/")[0]),
                             "labels": [LABEL]})
        except Exception as exc:  # noqa: BLE001 — one refusal must not lose the rest
            failed += 1
            print(f"  could not open an issue for {k}: {exc}", flush=True)
            continue
        c["issue"] = issue["number"]
        opened += 1
        print(f"  opened #{issue['number']}: {title}", flush=True)
    if opened:
        save(PENDING, pending)
    print(f"{opened} issue(s) opened; {sum(1 for c in pending.values() if not c.get('issue'))}"
          f" still queued" + (f"; {failed} could not be opened" if failed else ""),
          flush=True)
    # a candidate that could not be asked stays queued and is retried next week;
    # the non-zero tells the workflow to redden the run so it is not silent
    return 1 if failed else 0


# The weekly Tier-3 routine ends its report with a fenced json block of
# candidates. Reading it here is what lets a judgement call the routine surfaced
# become a question the operator can answer, instead of prose nobody opens.
CANDIDATE_BLOCK = re.compile(
    r"CANDIDATES FOR THE LEDGER.*?```(?:json)?\s*(\[.*?\])\s*```", re.S | re.I)


def ingest_report(path: Path) -> int:
    """Queue the candidates named in a routine report. Unreadable or absent
    reports are not an error: the routine may simply have found nothing."""
    if not path.exists():
        print(f"no report at {path}", flush=True)
        return 0
    m = CANDIDATE_BLOCK.search(path.read_text(encoding="utf-8", errors="replace"))
    if not m:
        print(f"{path.name} carries no candidate block", flush=True)
        return 0
    try:
        raw = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        print(f"{path.name}'s candidate block is not valid JSON ({exc}); ignored",
              flush=True)
        return 0
    cands = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict) or not c.get("url"):
            continue
        # a routine writes prose; take only the fields we understand, and never
        # let it choose a source id — that is the operator's to confirm
        cands.append({"provider": str(c.get("provider") or "")[:120] or None,
                      "model": str(c.get("model") or "")[:200] or None,
                      "url": str(c["url"])[:500],
                      "classification": str(c.get("classification") or "")[:120],
                      "why": str(c.get("note") or "")[:500]})
    before = load(PENDING)
    after = add_candidates(cands, pending=dict(before))
    save(PENDING, after)
    print(f"{len(after) - len(before)} candidate(s) queued from {path.name} "
          f"({len(cands)} named in the report)", flush=True)
    return 0


COMMAND = re.compile(r"^\s*/(approve|reject)\b[ \t]*(.*)$", re.I | re.M)


def parse_command(comment: str):
    """(action, id_override, note) or None. The first command line wins, so a
    quoted e-mail thread cannot re-trigger an older instruction."""
    m = COMMAND.search(comment or "")
    if not m:
        return None
    action, rest = m.group(1).lower(), (m.group(2) or "").strip()
    ident = None
    mid = re.match(r"id=(\S+)\s*(.*)$", rest, re.I)
    if mid:
        ident, rest = mid.group(1).strip(), (mid.group(2) or "").strip()
    return action, ident, rest


def apply_decision(issue_number: int, comment: str, association: str,
                   author: str = "") -> int:
    if association.upper() not in TRUSTED_ASSOCIATIONS:
        print(f"ignoring a comment from {author or 'someone'} "
              f"({association or 'no association'}): only the repository owner "
              f"decides", flush=True)
        return 0
    parsed = parse_command(comment)
    if not parsed:
        print("no /approve or /reject in the comment; nothing to do", flush=True)
        return 0
    action, ident, note = parsed
    pending = load(PENDING)
    match = [(k, c) for k, c in pending.items() if c.get("issue") == issue_number]
    if not match:
        print(f"no queued candidate is waiting on issue #{issue_number}", flush=True)
        return 0
    k, cand = match[0]
    if ident and not ID_RE.match(ident):
        print(f"'{ident}' is not a valid source id (expected org-slug/model-slug); "
              f"nothing recorded", flush=True)
        return 2
    decisions = load(DECISIONS)
    decisions[k] = {"decision": action, "id": ident or cand.get("source_id"),
                    "url": cand["url"], "kind": cand.get("kind", "provider-live"),
                    "provider": cand.get("provider"), "model": cand.get("model"),
                    "note": note, "issue": issue_number, "at": cap.utc_now()}
    save(DECISIONS, decisions)
    del pending[k]
    save(PENDING, pending)
    print(f"recorded: {action} {decisions[k]['id'] or cand['url']}"
          + (f" ({note})" if note else ""), flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose", help="open an issue per queued candidate")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--limit", type=int, default=10)
    i = sub.add_parser("ingest", help="queue candidates from a routine report")
    i.add_argument("--report", default=str(HERE.parent / "reports" / "tier3-hunt-latest.md"))
    a = sub.add_parser("apply", help="record the owner's answer")
    a.add_argument("--issue", type=int, required=True)
    a.add_argument("--comment", default="")
    a.add_argument("--author-association", default="")
    a.add_argument("--author", default="")
    args = ap.parse_args(argv)

    if args.cmd == "ingest":
        return ingest_report(Path(args.report))
    if args.cmd == "propose":
        token = os.environ.get("GITHUB_TOKEN", "")
        if not (args.repo and token):
            print("propose needs GITHUB_REPOSITORY and GITHUB_TOKEN", flush=True)
            return 2
        return propose(args.repo, token, args.limit)
    return apply_decision(args.issue, args.comment, args.author_association, args.author)


if __name__ == "__main__":
    sys.exit(main())
