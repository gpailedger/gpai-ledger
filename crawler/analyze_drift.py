"""Compare each provider's LIVE summary against AIAL's write-once snapshot.

Method: pick the live capture whose stored format is a document (pdf/zip/md) when one
exists (portal/hub HTML pages are captured but are not the document); then compare
word sequences (layout- and extraction-noise-proof), not raw lines or bytes.
Verdicts:
  identical-bytes         same hash live vs archive
  same-content            similarity >= 0.995 (re-render / re-serialization)
  DRIFT-CANDIDATE         similarity < 0.995 on comparable captures — inspect!
  capture-method-change   compared captures used different capture methods — not
                          evidence of a provider edit
  bundle-covered          document tracked at file level inside a bundle capture
  inpage-baseline         in-page document with one capture; drift starts next change
  format-mismatch         only a non-document live capture exists (hub/portal page)
  incomplete              missing one side
Output: reports/drift-latest.md + reports/drift-latest.json (overwritten each run)
"""
import difflib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOC_EXTS = (".pdf", ".zip", ".md", ".txt", ".docx")


def load_manifest(cap_dir: str):
    p = DATA / cap_dir / "manifest.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def load_words(cap_dir: str):
    p = DATA / cap_dir / "extracted.txt"
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    # drop every "===== inner-file =====" banner the zip extractor inserts (a
    # multi-PDF bundle has one per member, not just the first)
    text = re.sub(r"^=====.*=====$", "", text, flags=re.M)
    return re.findall(r"[A-Za-z0-9€%\.\-/:@]+", text)


def live_captures(state: dict, source_id: str):
    """Non-retired provider entries, NEWEST capture first — a stale superseded
    entry must never be paired ahead of the current document (insertion order
    previously let the oldest target win and mask real drift).
    Sort key is the capture TIMESTAMP (the path's last segment): sorting the full
    path would let the target-slug hash segment decide before the timestamp."""
    out = []
    for key, entry in state.items():
        sid, tslug = key.split("::", 1)
        if entry.get("retired"):
            continue
        if sid == source_id and tslug.startswith(("provider-live", "provider-page")):
            out.append(entry)
    return sorted(out, key=lambda e: e.get("last_capture", "").rsplit("/", 1)[-1],
                  reverse=True)


def archive_capture(state: dict, source_id: str):
    for key, entry in state.items():
        sid, tslug = key.split("::", 1)
        if entry.get("retired"):
            continue
        if sid == source_id and tslug.startswith("aial-archive"):
            return entry
    return None


def main() -> None:
    registry = json.loads((ROOT / "crawler" / "sources.json").read_text(encoding="utf-8"))
    state = json.loads((DATA / "state.json").read_text(encoding="utf-8"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
        doc_live = None
        for entry in lives:
            m = load_manifest(entry["last_capture"])
            if not m:
                continue
            if (m["stored_as"].endswith(DOC_EXTS)
                    or m.get("http", {}).get("url") in inpage_urls
                    or entry.get("managed")):
                doc_live = entry
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

        # In-page publications: comparing a rendered page (with site navigation)
        # against AIAL's PDF print is structurally noisy — page chrome guarantees a
        # difference. Their drift signal is SELF-HISTORY: newest vs previous capture
        # of the same target; the daily sweep's text-dedupe already gates minting.
        doc_m = load_manifest(doc_live["last_capture"])
        if doc_m and doc_m.get("http", {}).get("url") in inpage_urls:
            vs = doc_live.get("versions", [])
            if len(vs) >= 2:
                lw = load_words(vs[-1]["dir"])
                aw = load_words(vs[-2]["dir"])
                m_new = load_manifest(vs[-1]["dir"]) or {}
                m_old = load_manifest(vs[-2]["dir"]) or {}
                method_changed = (
                    bool(m_new.get("http", {}).get("rendered"))
                    != bool(m_old.get("http", {}).get("rendered"))
                    or bool(m_new.get("http", {}).get("frames_captured"))
                    != bool(m_old.get("http", {}).get("frames_captured"))
                    or bool(m_new.get("http", {}).get("consent_nodes_removed"))
                    != bool(m_old.get("http", {}).get("consent_nodes_removed")))
                if lw and aw:
                    sm = difflib.SequenceMatcher(None, aw, lw, autojunk=False)
                    ratio = sm.ratio()
                    if method_changed and ratio < 0.995:
                        verdict = "capture-method-change"
                        note = ("compared captures were made with different capture "
                                "methods (rendering/frame/consent handling changed "
                                "between them) — not evidence of a provider edit")
                    else:
                        verdict = ("DRIFT-CANDIDATE" if ratio < 0.995 else "same-content")
                        note = ("in-page document: compared to its own previous "
                                "capture (cross-format archive comparison is "
                                "structurally noisy)")
                    results.append({"id": source["id"], "model": source["model"],
                                    "verdict": verdict, "similarity": round(ratio, 4),
                                    "note": note})
                    continue
            results.append({"id": source["id"], "model": source["model"],
                            "verdict": "inpage-baseline",
                            "note": "in-page document with a single capture — drift "
                                    "detection starts from its next content change"})
            continue

        if doc_live["last_sha256"] == arch["last_sha256"]:
            results.append({"id": source["id"], "model": source["model"],
                            "verdict": "identical-bytes"})
            continue

        lw, aw = load_words(doc_live["last_capture"]), load_words(arch["last_capture"])
        if not lw or not aw:
            results.append({"id": source["id"], "model": source["model"],
                            "verdict": "format-mismatch",
                            "note": "no extracted text on one side"})
            continue
        sm = difflib.SequenceMatcher(None, aw, lw, autojunk=False)
        ratio = sm.ratio()
        if ratio < 0.995:
            # Cross-format pairs (markdown vs PDF-print) produce spacing/hyphenation
            # word-split artifacts ("Summary:1.0" vs "Summary: 1.0", "re- flect").
            # A character stream stripped of everything non-alphanumeric is immune:
            # real edits change the letters, re-renders never do.
            a_chars = "".join(re.findall(r"[a-z0-9]+", " ".join(aw).lower()))
            l_chars = "".join(re.findall(r"[a-z0-9]+", " ".join(lw).lower()))
            if a_chars == l_chars:
                ratio = 1.0
            else:
                ratio = max(ratio, difflib.SequenceMatcher(
                    None, a_chars, l_chars, autojunk=False).ratio())
        if ratio >= 0.995:
            results.append({"id": source["id"], "model": source["model"],
                            "verdict": "same-content", "similarity": round(ratio, 4)})
        else:
            changes = []
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag != "equal":
                    changes.append({"op": tag, "old": " ".join(aw[i1:i2])[:300],
                                    "new": " ".join(lw[j1:j2])[:300]})
            results.append({"id": source["id"], "model": source["model"],
                            "verdict": "DRIFT-CANDIDATE", "similarity": round(ratio, 4),
                            "changes": changes[:40]})

    (ROOT / "reports" / "drift-latest.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    order = {"DRIFT-CANDIDATE": 0, "format-mismatch": 1, "incomplete": 2,
             "same-content": 3, "capture-method-change": 4, "bundle-covered": 5, "inpage-baseline": 6, "identical-bytes": 7}
    results.sort(key=lambda r: (order.get(r["verdict"], 9), r["id"]))
    lines = [f"# Live-vs-archive drift — {today}", "",
             "| Model | Verdict | Similarity / note |", "|---|---|---|"]
    for r in results:
        extra = r.get("note") or (str(r.get("similarity")) if "similarity" in r else "")
        lines.append(f"| {r['model']} | {r['verdict']} | {extra} |")
    (ROOT / "reports" / "drift-latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(json.dumps(counts, indent=2))
    for r in results:
        if r["verdict"] == "DRIFT-CANDIDATE":
            print(f"  DRIFT-CANDIDATE: {r['id']} similarity={r['similarity']}")


if __name__ == "__main__":
    main()
