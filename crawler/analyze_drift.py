"""Compare each provider's LIVE summary against AIAL's write-once snapshot, and
every version of every target against its own previous version.

Method: pick the live capture whose stored format is a document (pdf/zip/md) when one
exists (portal/hub HTML pages are captured but are not the document); then compare
word sequences (layout- and extraction-noise-proof), not raw lines or bytes. When two
stored extracts differ, BOTH captures are re-extracted from their stored bytes with the
current extractor before any verdict is given, so a change of extractor version can
never read as a provider edit (`compared_via` names the tool used on each side and
`same_tool` says whether it was one tool). When a side cannot be re-extracted, the
stored extracts decide and the verdict is `changed-unverified`: a candidate, never
published as a content change.

Identity rule (RULE_VERSION): two texts are identical when their identity streams
are equal — the Unicode word characters of the text, case-sensitive, after NFC
composition and with typographic ligatures (ﬁ, ﬂ, …) spelled out, with tick and
cross glyphs kept as tokens (the Commission template is built from tick boxes) and a
separator between two digits kept (1.5 and 1,5 are not 15). Spacing, hyphenation and
other punctuation are ignored: "Summary:1.0" vs "Summary: 1.0" and "re- flect" vs
"reflect" are word-split artifacts of extraction or layout, neither counted nor
listed. Moves are decided on WHOLE WORDS first: a block of words deleted in one
place and inserted elsewhere unchanged — at least LONG_MOVE_WORDS words anywhere, or
a shorter block of at least MIN_MOVE_WORDS words that is the entire deleted or
inserted run (a running header that changed page) — is counted under moved_words,
not word_delta; a transposition inside a word is an edit, and a phrase two rewritten
paragraphs happen to share is counted as changed. The remaining differences are
measured on the identity character streams of each region the word-level alignment
marks as different (plus one word of context), with every character mapped back to
its word, so the count does not depend on how a token aligner happened to anchor
and the cost is bounded by the size of the changed regions, not of the document; a
region too large to align character by character (CHAR_ALIGN_CAP) is counted word
by word and the record says so (`alignment`). Each listed change shows the words
around the difference on both sides.

Live-vs-archive verdicts:
  identical-bytes         same hash live vs archive
  same-content            identity streams equal (re-render / re-serialization)
  near-identical          similarity >= 0.995 but the text differs — word_delta and the
                          changes are listed; a one-word edit in a long document lands
                          here, never under same-content
  DRIFT-CANDIDATE         similarity < 0.995 on comparable captures — inspect!
  capture-method-change   compared captures used different capture methods — not
                          evidence of a provider edit
  bundle-covered          document tracked at file level inside a bundle capture
  inpage-baseline         in-page document with one capture; drift starts next change
  format-mismatch         only a non-document live capture exists (hub/portal page)
  incomplete              missing one side

Self-history: reports/version-diffs.json is a durable ledger with one record per
consecutive version pair of every target, and one per live-vs-archive pair:
  identical-text | changed (word_delta, moved_words, similarity, changes) |
  changed-unverified (stored extracts differ, a side could not be re-extracted) |
  method-changed (the text differs but the two captures were made with different
  capture methods — rendering, frames or consent handling — so the difference is
  not evidence of a content change; when an earlier capture made with the SAME
  method as the newest one exists, the newest is also compared with it like for
  like, and a real edit found that way makes the record `changed` with
  `compared_with` naming that capture) | no-text
A record is computed once under the rule version it names; if the rule changes, the
record is recomputed and the earlier verdict kept under prior_verdicts. The site
takes its "content changed" notes and /changes/ entries from that ledger.
Output: reports/drift-latest.md + reports/drift-latest.json (overwritten each run),
reports/version-diffs.json (the ledger).
"""
import difflib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import capture as cap

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOC_EXTS = (".pdf", ".zip", ".md", ".txt", ".docx")
SIMILAR = 0.995
RULE_VERSION = "identity-v4"
MIN_MOVE_WORDS = 2      # shortest block that can be a move (when it is a whole run)
LONG_MOVE_WORDS = 4     # a block this long is a move wherever it sits
CHAR_ALIGN_CAP = 25_000_000   # len(old chars) * len(new chars) aligned per region
# tick / cross glyphs carry meaning in the Commission template: kept as tokens
GLYPHS = {"☒": "⊠", "☑": "⊠", "✓": "⊠", "✔": "⊠", "■": "⊠",
          "☐": "⊡", "□": "⊡", "▢": "⊡", "✗": "⊗", "✘": "⊗"}
# typographic ligatures are a font's choice, not the author's
LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
                           "ﬅ": "ft", "ﬆ": "st"})
WORD = re.compile(r"[☒☑✓✔■☐□▢✗✘]|[\w€%\.\-/:@,]+")
IDENT = re.compile(r"[\w⊠⊡⊗·]+")


def vdiffs_path() -> Path:
    return ROOT / "reports" / "version-diffs.json"


def load_manifest(cap_dir: str):
    p = DATA / cap_dir / "manifest.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def words_of(text: str):
    # drop every "===== inner-file =====" banner the zip extractor inserts (a
    # multi-PDF bundle has one per member, not just the first)
    text = re.sub(r"^=====.*=====$", "", text, flags=re.M)
    # compose accents and spell out ligatures BEFORE tokenising: a combining
    # mark is not a word character and would split "café" into two tokens
    text = unicodedata.normalize("NFC", text).translate(LIGATURES)
    return WORD.findall(text)


def load_words(cap_dir: str):
    p = DATA / cap_dir / "extracted.txt"
    if not p.exists():
        return None
    return words_of(p.read_text(encoding="utf-8"))


def extractor_for(stored_as: str) -> str:
    """The tool capture.extract_text dispatches to for a stored file."""
    ext = "." + stored_as.split(".", 1)[1] if "." in stored_as else ""
    if ext in (".pdf", ".zip"):
        try:
            import pypdf
            return f"pypdf {pypdf.__version__}"
        except Exception:  # noqa: BLE001
            return "pypdf"
    if ext == ".html":
        try:
            import bs4
            return f"beautifulsoup4 {bs4.__version__} (html.parser)"
        except Exception:  # noqa: BLE001
            return "beautifulsoup4 (html.parser)"
    if ext in (".md", ".txt", ".json"):
        return "utf-8 decode"
    return f"no extractor for {ext or 'this format'}"


def reextract(cap_dir: str):
    """(words, extractor label) of the capture's text extracted NOW from its
    stored bytes; words is None when the bytes are missing or no text results."""
    m = load_manifest(cap_dir)
    if not m or "." not in str(m.get("stored_as", "")):
        return None, None
    label = extractor_for(m["stored_as"])
    p = DATA / cap_dir / m["stored_as"]
    if not p.exists():
        return None, label
    text, _notes = cap.extract_text(p.read_bytes(), "." + m["stored_as"].split(".", 1)[1])
    return (words_of(text) if text else None), label


def _identity(words) -> str:
    s = unicodedata.normalize("NFC", " ".join(words)).translate(LIGATURES)
    s = "".join(GLYPHS.get(ch, ch) for ch in s)
    s = re.sub(r"(?<=\d)[.,](?=\d)", "·", s)   # a separator between digits is meaning
    return "".join(IDENT.findall(s))


def _stream(words):
    """Identity stream of a word list plus, per character, the owning word."""
    chars, owner = [], []
    for i, w in enumerate(words):
        ident = _identity([w])
        chars.append(ident)
        owner.extend([i] * len(ident))
    return "".join(chars), owner


def _moves(ia, ib):
    """Whole-word moves: a block of word identities deleted in one place that
    reappears unchanged inside an insertion elsewhere (never glyphs alone). Only
    identity-bearing tokens take part (a bare "-" has none). Common sub-runs
    between a deletion and an insertion are matched — the word aligner lumps a
    moved block together with its neighbours — and, since the aligner leaves no
    common word inside one replace region, a match between two regions is a
    crossed block, never an unchanged word. A block of LONG_MOVE_WORDS or more
    counts anywhere; a shorter block (MIN_MOVE_WORDS or more) counts only when
    it is the entire deleted or the entire inserted run — a running header that
    changed page — so a phrase two paragraphs happen to share in a rewrite is
    counted as changed, not moved.
    Returns (moved indices in a, moved indices in b, [(i1, i2, j1, j2)])."""
    ka = [i for i, w in enumerate(ia) if w]
    kb = [j for j, w in enumerate(ib) if w]
    sa, sb = [ia[i] for i in ka], [ib[j] for j in kb]
    ops = [op for op in difflib.SequenceMatcher(None, sa, sb, autojunk=False).get_opcodes()
           if op[0] != "equal"]
    dels = [(i1, i2) for t, i1, i2, j1, j2 in ops
            if t in ("delete", "replace") and i2 - i1 >= MIN_MOVE_WORDS]
    ins = [(j1, j2) for t, i1, i2, j1, j2 in ops
           if t in ("insert", "replace") and j2 - j1 >= MIN_MOVE_WORDS]
    ma, mb, blocks = set(), set(), []
    for i1, i2 in dels:
        for j1, j2 in ins:
            sm = difflib.SequenceMatcher(None, sa[i1:i2], sb[j1:j2], autojunk=False)
            for x, y, n in sm.get_matching_blocks():
                if n < MIN_MOVE_WORDS:
                    continue
                if n < LONG_MOVE_WORDS and n != i2 - i1 and n != j2 - j1:
                    continue
                if re.fullmatch(r"[⊠⊡⊗]*", "".join(sa[i1 + x:i1 + x + n])):
                    continue
                oi = [ka[k] for k in range(i1 + x, i1 + x + n)]
                oj = [kb[k] for k in range(j1 + y, j1 + y + n)]
                if any(k in ma for k in oi) or any(k in mb for k in oj):
                    continue
                ma.update(oi)
                mb.update(oj)
                blocks.append((oi[0], oi[-1] + 1, oj[0], oj[-1] + 1))
    blocks.sort()
    return ma, mb, blocks


def _region_groups(ra, rb, i1, i2, j1, j2):
    """Character-level difference groups for one word region [i1,i2) x [j1,j2)
    of the residual sequences, aligned with one word of context on each side.
    Returns (groups, coarse, unmatched_old_chars, unmatched_new_chars): groups
    are [old_idx set, new_idx set, old_span, new_span] with indices into ra/rb;
    coarse is True when the region was too large to align character by
    character and was counted word by word (all its characters unmatched)."""
    a1, a2 = max(0, i1 - 1), min(len(ra), i2 + 1)
    b1, b2 = max(0, j1 - 1), min(len(rb), j2 + 1)
    sa, oa = _stream(ra[a1:a2])
    sb, ob = _stream(rb[b1:b2])
    oa = [a1 + k for k in oa]
    ob = [b1 + k for k in ob]
    if sa == sb:
        return [], False, 0, 0    # a word-split artifact: the same identity characters
    if len(sa) * len(sb) > CHAR_ALIGN_CAP:
        old_idx, new_idx = set(range(i1, i2)), set(range(j1, j2))
        ua = sum(len(_identity([ra[k]])) for k in old_idx)
        ub = sum(len(_identity([rb[k]])) for k in new_idx)
        return [[old_idx, new_idx, (i1 - 1, i2), (j1 - 1, j2)]], True, ua, ub
    groups, ua, ub = [], 0, 0
    for t, c1, c2, d1, d2 in difflib.SequenceMatcher(None, sa, sb, autojunk=False).get_opcodes():
        if t == "equal":
            continue
        ua += c2 - c1
        ub += d2 - d1
        old_idx, new_idx = set(oa[c1:c2]), set(ob[d1:d2])
        # context: the words around an insertion/deletion point on the side
        # that has no differing characters of its own
        old_near = old_idx or {oa[p] for p in (c1 - 1, c1) if 0 <= p < len(sa)}
        new_near = new_idx or {ob[p] for p in (d1 - 1, d1) if 0 <= p < len(sb)}
        old_span = (min(old_near) - 1, max(old_near) + 1) if old_near else None
        new_span = (min(new_near) - 1, max(new_near) + 1) if new_near else None
        groups.append([old_idx, new_idx, old_span, new_span])
    return groups, False, ua, ub


def compare_words(a, b) -> dict:
    """{similarity, identical, word_delta, moved_words, changes[, alignment]} for
    two word sequences. identical = identity streams equal. Moves are whole-word
    runs (see _moves); the remaining differences are measured region by region
    on the identity character streams with each character mapped back to its
    word, so the count never depends on how a token aligner anchored and the
    cost is bounded by the changed regions: word_delta = the number of words
    whose characters differ, per merged region. Each listed change shows the
    words around the difference on both sides. similarity = the better of the
    word-level ratio and the identity-character ratio of the regional
    alignments (no document-wide alignment)."""
    word_ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    ia, ib = [_identity([w]) for w in a], [_identity([w]) for w in b]
    if "".join(ia) == "".join(ib):
        return {"similarity": 1.0, "identical": True, "word_delta": 0,
                "moved_words": 0, "changes": []}
    ma, mb, blocks = _moves(ia, ib)
    changes = [{"op": "moved", "old": " ".join(a[i1:i2])[:300], "new": " ".join(b[j1:j2])[:300]}
               for i1, i2, j1, j2 in blocks]
    moved_words = sum(i2 - i1 for i1, i2, _, _ in blocks)
    # residual sequences: moved words removed on both sides
    ra = [w for i, w in enumerate(a) if i not in ma]
    rb = [w for j, w in enumerate(b) if j not in mb]
    ira = [_identity([w]) for w in ra]
    irb = [_identity([w]) for w in rb]
    groups, coarse, unmatched_a, unmatched_b = [], False, 0, 0
    for t, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ira, irb, autojunk=False).get_opcodes():
        if t == "equal":
            continue
        g, c, ua, ub = _region_groups(ra, rb, i1, i2, j1, j2)
        groups += g
        coarse = coarse or c
        unmatched_a += ua
        unmatched_b += ub

    def _overlap(p, q):
        return p is not None and q is not None and p[0] <= q[1] and q[0] <= p[1]

    merged = True
    while merged:
        merged = False
        for x in range(len(groups)):
            for y in range(x + 1, len(groups)):
                gx, gy = groups[x], groups[y]
                if _overlap(gx[2], gy[2]) or _overlap(gx[3], gy[3]):
                    gx[0] |= gy[0]
                    gx[1] |= gy[1]
                    for s in (2, 3):
                        if gx[s] is None:
                            gx[s] = gy[s]
                        elif gy[s] is not None:
                            gx[s] = (min(gx[s][0], gy[s][0]), max(gx[s][1], gy[s][1]))
                    del groups[y]
                    merged = True
                    break
            if merged:
                break
    delta = 0
    for old_idx, new_idx, old_span, new_span in groups:
        delta += max(len(old_idx), len(new_idx))
        tag = "replace" if old_idx and new_idx else ("delete" if old_idx else "insert")
        old_txt = " ".join(ra[max(0, old_span[0]):old_span[1] + 1]) if old_span else ""
        new_txt = " ".join(rb[max(0, new_span[0]):new_span[1] + 1]) if new_span else ""
        changes.append({"op": tag, "old": old_txt[:300], "new": new_txt[:300]})
    if word_ratio >= SIMILAR:
        ratio = word_ratio
    else:
        # identity-character ratio from the regional alignments: every character
        # outside a non-equal opcode is matched (moved words included), the same
        # 2M/(|a|+|b|) SequenceMatcher reports, without a document-wide alignment
        total = sum(len(x) for x in ia) + sum(len(x) for x in ib)
        ratio = max(word_ratio, (total - unmatched_a - unmatched_b) / total) if total else 1.0
    out = {"similarity": round(ratio, 4), "identical": False, "word_delta": delta,
           "moved_words": moved_words, "changes": changes[:40]}
    if coarse:
        out["alignment"] = "word-level (a changed region was too large for character alignment)"
    return out


def compare_captures(a_dir: str, b_dir: str, roles=("previous", "newest")) -> dict:
    """Compare two captures' text on a COMMON extractor: stored extracts first;
    when they differ, both captures are re-extracted from their bytes with the
    current extractor (named per side), so extractor-version drift between
    capture eras cannot pass for an edit. If a side cannot be re-extracted the
    stored extracts decide and the verdict is changed-unverified."""
    a_stored, b_stored = load_words(a_dir), load_words(b_dir)
    if a_stored is None or b_stored is None:
        missing = [r for r, w in zip(roles, (a_stored, b_stored)) if w is None]
        return {"verdict": "no-text", "same_tool": False, "rule": RULE_VERSION,
                "compared_via": f"stored extracts (no extracted text for the "
                                f"{' and '.join(missing)} capture)"}
    res = compare_words(a_stored, b_stored)
    via, same_tool, verified = "stored extracts", False, True
    if not res["identical"]:
        (ra, la), (rb, lb) = reextract(a_dir), reextract(b_dir)
        if ra is not None and rb is not None:
            res = compare_words(ra, rb)
            same_tool = la == lb
            via = (f"re-extracted from the stored bytes with {la}" if same_tool else
                   f"re-extracted from the stored bytes: {la} ({roles[0]}) vs {lb} ({roles[1]})")
        else:
            failed = [r for r, w in zip(roles, (ra, rb)) if w is None]
            via = (f"stored extracts (re-extraction unavailable for the "
                   f"{' and '.join(failed)} capture)")
            verified = False
    if res["identical"]:
        verdict = "identical-text"
    else:
        verdict = "changed" if verified else "changed-unverified"
    rec = {"verdict": verdict, "similarity": res["similarity"],
           "word_delta": res["word_delta"], "moved_words": res["moved_words"],
           "same_tool": same_tool, "compared_via": via, "rule": RULE_VERSION}
    if not res["identical"]:
        rec["changes"] = res["changes"]
    if res.get("alignment"):
        rec["alignment"] = res["alignment"]
    return rec


def capture_method(m: dict) -> tuple:
    """The aspects of a capture method that change a rendered page's text
    without the document changing: browser rendering, captured frames, consent
    chrome handling (by state — a banner that survived one capture and not the
    next is a method difference, not an edit; a capture made before consent
    handling existed has state None)."""
    h = (m or {}).get("http", {}) or {}
    consent = None if "consent_nodes_removed" not in h else int(h.get("consent_nodes_removed") or 0)
    return (bool(h.get("rendered")), bool(h.get("frames_captured")), consent,
            bool(h.get("consent_dismissed")))


def method_changed(a_dir: str, b_dir: str) -> bool:
    ma, mb = load_manifest(a_dir) or {}, load_manifest(b_dir) or {}
    if not (str(ma.get("stored_as", "")).endswith(".html")
            and str(mb.get("stored_as", "")).endswith(".html")):
        return False
    return capture_method(ma) != capture_method(mb)


def load_vdiffs() -> dict:
    p = vdiffs_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


_RESTRICTED_DIR_CACHE = {}


def _dir_is_restricted(d) -> bool:
    """Whether one capture directory holds content this project withholds.

    Two rules, and BOTH must be honoured here or the redaction has a hole: the
    capture's kind (an evaluation, in any of its forms) and its URL (AIAL's
    tracker root publishes the full grade table under the ordinary watch-page
    kind). The site checks both; a check here that saw only the kind would
    republish, in a committed report file, exactly what the site withholds."""
    if not d:
        return False
    if d in _RESTRICTED_DIR_CACHE:
        return _RESTRICTED_DIR_CACHE[d]
    verdict = cap.kind_of_capture_dir(d) in cap.RESTRICTED_KINDS
    if not verdict:
        m = load_manifest(d) or {}
        http = m.get("http") or {}
        verdict = (m.get("target_kind") in cap.RESTRICTED_KINDS
                   or http.get("url") in cap.RESTRICTED_URLS
                   or http.get("final_url") in cap.RESTRICTED_URLS)
    _RESTRICTED_DIR_CACHE[d] = verdict
    return verdict


def excerpts_allowed(rec) -> bool:
    """Whether a diff record may carry verbatim excerpts of what it compared.
    reports/version-diffs.json and reports/drift-latest.json are committed to a
    public repository: a record touching content this project archives but does
    not republish keeps its METRICS (a word count is a fact about a change) and
    loses the words themselves."""
    return not any(_dir_is_restricted(d)
                   for d in (rec.get("from_dir"), rec.get("to_dir")))


def redact_excerpts(rec: dict) -> dict:
    if excerpts_allowed(rec):
        return rec
    return {**rec, "changes": [], "changes_withheld": True}


def save_vdiffs(vdiffs: dict) -> None:
    p = vdiffs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cap.atomic_write_text(p, json.dumps(
        {k: redact_excerpts(v) for k, v in sorted(vdiffs.items())},
        indent=2, ensure_ascii=False))


def pair_key(source_id: str, tslug: str, from_dir: str, to_dir: str) -> str:
    return f"{source_id}::{tslug}::{from_dir}>{to_dir}"


def _store(vdiffs: dict, key: str, rec: dict, **identity) -> None:
    """Insert a record; a record computed under an older rule version is
    replaced, with its verdict kept under prior_verdicts."""
    old = vdiffs.get(key)
    rec.update(identity)
    rec["computed_at"] = cap.utc_now()
    if old:
        kept = {k: old.get(k) for k in ("verdict", "similarity", "word_delta",
                                         "moved_words", "rule", "computed_at")}
        rec["prior_verdicts"] = old.get("prior_verdicts", []) + [kept]
    vdiffs[key] = rec


def _current(vdiffs: dict, key: str):
    rec = vdiffs.get(key)
    return rec if rec and rec.get("rule") == RULE_VERSION else None


def _like_for_like(vs, i, cur: dict):
    """The newest version before vs[i] whose capture method equals cur's, or None."""
    want = capture_method(load_manifest(cur["dir"]) or {})
    for v in reversed(vs[:i]):
        if capture_method(load_manifest(v["dir"]) or {}) == want:
            return v
    return None


def update_version_diffs(state: dict, vdiffs: dict) -> int:
    """Give every consecutive version pair of every target a ledger record under
    the current rule. Returns the number of records computed."""
    added = 0
    for key, entry in state.items():
        sid, tslug = key.split("::", 1)
        vs = entry.get("versions", [])
        for i, (prev, cur) in enumerate(zip(vs, vs[1:])):
            k = pair_key(sid, tslug, prev["dir"], cur["dir"])
            if _current(vdiffs, k):
                continue
            rec = compare_captures(prev["dir"], cur["dir"])
            if rec["verdict"] in ("changed", "changed-unverified") and method_changed(prev["dir"], cur["dir"]):
                # a text difference between captures made with different methods
                # is not evidence of a content change; the measurements stay —
                # but a page whose method flaps on every capture must not be able
                # to hide an edit: compare like for like with the newest earlier
                # capture made with the same method, when there is one
                rec["verdict"] = "method-changed"
                alt = _like_for_like(vs, i, cur)
                if alt:
                    rec2 = compare_captures(alt["dir"], cur["dir"],
                                            roles=("same-method earlier", "newest"))
                    rec["same_method_pair"] = {
                        "from_dir": alt["dir"], "verdict": rec2["verdict"],
                        "similarity": rec2.get("similarity"), "word_delta": rec2.get("word_delta"),
                        "moved_words": rec2.get("moved_words"),
                        "compared_via": rec2.get("compared_via")}
                    if rec2["verdict"] == "changed" and rec2.get("word_delta"):
                        rec.update({"verdict": "changed", "similarity": rec2["similarity"],
                                    "word_delta": rec2["word_delta"],
                                    "moved_words": rec2.get("moved_words", 0),
                                    "changes": rec2.get("changes", []),
                                    "same_tool": rec2.get("same_tool", False),
                                    "compared_via": rec2["compared_via"] + " — like for like: "
                                    "compared with the newest earlier capture made with the "
                                    "same method, not the immediately previous one",
                                    "compared_with": alt["dir"]})
                        if rec2.get("alignment"):
                            rec["alignment"] = rec2["alignment"]
            _store(vdiffs, k, rec, source=sid, target=tslug,
                   from_dir=prev["dir"], to_dir=cur["dir"],
                   from_sha256=prev["sha256"], to_sha256=cur["sha256"])
            added += 1
    return added


def newest_pair_record(vdiffs: dict, source_id: str, tslug: str, entry: dict) -> dict:
    """The ledger verdict of the entry's newest version against its previous one."""
    vs = entry.get("versions", [])
    if len(vs) < 2:
        return {"verdict": "single-version"}
    rec = vdiffs.get(pair_key(source_id, tslug, vs[-2]["dir"], vs[-1]["dir"]))
    if not rec:
        return {"verdict": "no-record"}
    return {f: rec[f] for f in ("verdict", "similarity", "word_delta", "moved_words",
                                "changes", "compared_via", "same_tool", "from_dir",
                                "to_dir", "compared_with", "alignment") if f in rec}


def live_captures(state: dict, source_id: str):
    """(tslug, entry) for non-retired provider entries, NEWEST capture first — a
    stale superseded entry must never be paired ahead of the current document
    (insertion order previously let the oldest target win and mask real drift).
    Sort key is the capture TIMESTAMP (the path's last segment): sorting the full
    path would let the target-slug hash segment decide before the timestamp."""
    out = []
    for key, entry in state.items():
        sid, tslug = key.split("::", 1)
        if entry.get("retired"):
            continue
        if sid == source_id and tslug.startswith(("provider-live", "provider-page")):
            out.append((tslug, entry))
    return sorted(out, key=lambda te: te[1].get("last_capture", "").rsplit("/", 1)[-1],
                  reverse=True)


def archive_capture(state: dict, source_id: str):
    for key, entry in state.items():
        sid, tslug = key.split("::", 1)
        if entry.get("retired"):
            continue
        if sid == source_id and tslug.startswith("aial-archive"):
            return entry
    return None


def _history_cell(sh) -> str:
    v = (sh or {}).get("verdict")
    if v == "changed":
        moved = sh.get("moved_words") or 0
        return (f"changed ({sh.get('word_delta')} word(s)"
                + (f", {moved} moved" if moved else "")
                + (", like for like" if sh.get("compared_with") else "") + ")")
    if v == "changed-unverified":
        return f"differs by the stored extracts ({sh.get('word_delta')} word(s); not re-extracted)"
    return {"identical-text": "identical text", "single-version": "single version",
            "method-changed": "capture method changed (not comparable)",
            "no-text": "no text to compare", "no-record": "—"}.get(v, "—")


def main() -> None:
    registry = json.loads((ROOT / "crawler" / "sources.json").read_text(encoding="utf-8"))
    state = json.loads((DATA / "state.json").read_text(encoding="utf-8"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # the self-history ledger first: every target, every consecutive pair
    vdiffs = load_vdiffs()
    added = update_version_diffs(state, vdiffs)

    results = []
    for source in registry["sources"]:
        if source["status"] != "published":
            continue
        arch = archive_capture(state, source["id"])
        lives = live_captures(state, source["id"])
        if not arch or not lives:
            results.append({"id": source["id"], "model": source["model"],
                            "verdict": "incomplete",
                            "note": f"live={len(lives)} archive={'y' if arch else 'n'}"})
            continue

        # prefer a document-format live capture; in-page publications (registry
        # inpage flag) ARE documents even though stored as rendered .html, and
        # extractor-managed entries are documents by construction
        inpage_urls = {t["url"] for t in source.get("targets", []) if t.get("inpage")}
        doc_live, doc_tslug = None, None
        for tslug, entry in lives:
            m = load_manifest(entry["last_capture"])
            if not m:
                continue
            if (m["stored_as"].endswith(DOC_EXTS)
                    or m.get("http", {}).get("url") in inpage_urls
                    or entry.get("managed")):
                doc_live, doc_tslug = entry, tslug
                break
        if doc_live is None:
            if source["id"].startswith("anthropic/claude") and source["status"] == "published":
                results.append({"id": source["id"], "model": source["model"],
                                "verdict": "bundle-covered",
                                "note": "document tracked at file level inside the "
                                        "anthropic/trust-center-bundle capture (inner "
                                        "per-file SHA-256s); the portal watch covers "
                                        "listing changes"})
            else:
                results.append({"id": source["id"], "model": source["model"],
                                "verdict": "format-mismatch",
                                "note": "live captures are hub/portal HTML only — "
                                        "document URL needed"})
            continue
        history = newest_pair_record(vdiffs, source["id"], doc_tslug, doc_live)

        # In-page publications: comparing a rendered page (with site navigation)
        # against AIAL's PDF print is structurally noisy — page chrome guarantees a
        # difference. Their drift signal is SELF-HISTORY: newest vs previous capture
        # of the same target (the ledger record); the daily sweep's text-dedupe
        # already gates minting.
        doc_m = load_manifest(doc_live["last_capture"])
        if doc_m and doc_m.get("http", {}).get("url") in inpage_urls:
            vs = doc_live.get("versions", [])
            if len(vs) >= 2 and history.get("verdict") in ("identical-text", "changed",
                                                           "changed-unverified",
                                                           "method-changed"):
                sim = history.get("similarity", 1.0)
                note = ("in-page document: compared to its own previous capture "
                        "(cross-format archive comparison is structurally noisy)")
                if history["verdict"] == "identical-text":
                    verdict = "same-content"
                elif history["verdict"] == "method-changed":
                    verdict = "capture-method-change"
                    note = ("compared captures were made with different capture "
                            "methods (rendering/frame/consent handling changed "
                            "between them) — not evidence of a provider edit")
                elif sim >= SIMILAR:
                    verdict = "near-identical"
                else:
                    verdict = "DRIFT-CANDIDATE"
                rec = {"id": source["id"], "model": source["model"], "verdict": verdict,
                       "similarity": round(sim, 4), "compared_via": history.get("compared_via"),
                       "same_tool": history.get("same_tool", False),
                       "note": note, "self_history": history}
                if verdict in ("near-identical", "DRIFT-CANDIDATE"):
                    rec["word_delta"] = history.get("word_delta")
                    rec["moved_words"] = history.get("moved_words", 0)
                    rec["changes"] = history.get("changes", [])
                results.append(rec)
                continue
            if len(vs) >= 2:
                results.append({"id": source["id"], "model": source["model"],
                                "verdict": "inpage-baseline",
                                "note": "in-page document: its newest pair has no "
                                        "comparable text (see self_history)",
                                "self_history": history})
                continue
            results.append({"id": source["id"], "model": source["model"],
                            "verdict": "inpage-baseline",
                            "note": "in-page document with a single capture — drift "
                                    "detection starts from its next content change",
                            "self_history": history})
            continue

        if doc_live["last_sha256"] == arch["last_sha256"]:
            results.append({"id": source["id"], "model": source["model"],
                            "verdict": "identical-bytes", "self_history": history})
            continue

        # live vs archive: computed once per pair and kept in the ledger too
        k = pair_key(source["id"], "live-vs-archive", arch["last_capture"], doc_live["last_capture"])
        rec = _current(vdiffs, k)
        if not rec:
            rec = compare_captures(arch["last_capture"], doc_live["last_capture"],
                                   roles=("archive", "live"))
            _store(vdiffs, k, rec, source=source["id"], target="live-vs-archive",
                   from_dir=arch["last_capture"], to_dir=doc_live["last_capture"],
                   from_sha256=arch["last_sha256"], to_sha256=doc_live["last_sha256"])
            added += 1
        if rec["verdict"] == "no-text":
            results.append({"id": source["id"], "model": source["model"],
                            "verdict": "format-mismatch",
                            "note": "no extracted text on one side",
                            "self_history": history})
            continue
        base = {"id": source["id"], "model": source["model"],
                "similarity": rec["similarity"], "compared_via": rec["compared_via"],
                "same_tool": rec.get("same_tool", False), "self_history": history}
        if rec["verdict"] == "identical-text":
            results.append({**base, "verdict": "same-content"})
        else:
            verdict = "near-identical" if rec["similarity"] >= SIMILAR else "DRIFT-CANDIDATE"
            # this list only ever compares a provider's live document with its
            # archived copy, so no restricted kind reaches it today; the guard
            # keeps that true if the comparison is ever widened
            allowed = excerpts_allowed(rec)
            results.append({**base, "verdict": verdict, "word_delta": rec["word_delta"],
                            "moved_words": rec.get("moved_words", 0),
                            "changes": rec.get("changes", []) if allowed else [],
                            **({} if allowed else {"changes_withheld": True})})

    if added:
        save_vdiffs(vdiffs)
    cap.atomic_write_text(ROOT / "reports" / "drift-latest.json",
                          json.dumps(results, indent=2, ensure_ascii=False))

    order = {"DRIFT-CANDIDATE": 0, "near-identical": 1, "format-mismatch": 2,
             "incomplete": 3, "same-content": 4, "capture-method-change": 5,
             "bundle-covered": 6, "inpage-baseline": 7, "identical-bytes": 8}
    results.sort(key=lambda r: (order.get(r["verdict"], 9), r["id"]))
    lines = [f"# Live-vs-archive drift — {today}", "",
             "| Model | Verdict | Similarity / note | Newest vs previous version |",
             "|---|---|---|---|"]
    for r in results:
        extra = r.get("note") or (str(r.get("similarity")) if "similarity" in r else "")
        if r["verdict"] == "near-identical":
            moved = r.get("moved_words") or 0
            extra = (f"{r['similarity']} — {r.get('word_delta')} word(s) differ"
                     + (f", {moved} moved" if moved else ""))
        lines.append(f"| {r['model']} | {r['verdict']} | {extra} | "
                     f"{_history_cell(r.get('self_history'))} |")
    cap.atomic_write_text(ROOT / "reports" / "drift-latest.md", "\n".join(lines) + "\n")

    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(json.dumps(counts, indent=2))
    print(f"version-diffs ledger: {added} record(s) computed, {len(vdiffs)} total")
    for r in results:
        if r["verdict"] in ("DRIFT-CANDIDATE", "near-identical"):
            print(f"  {r['verdict']}: {r['id']} similarity={r['similarity']} "
                  f"word_delta={r.get('word_delta')} moved={r.get('moved_words', 0)}")
        sh = r.get("self_history") or {}
        if sh.get("verdict") in ("changed", "changed-unverified"):
            print(f"  SELF-HISTORY {sh['verdict'].upper()}: {r['id']} "
                  f"word_delta={sh.get('word_delta')} moved={sh.get('moved_words', 0)} "
                  f"({sh.get('compared_via')})")


if __name__ == "__main__":
    main()
