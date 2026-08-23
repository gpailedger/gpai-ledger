"""Reader-lens lint for the generated site: mechanical checks for the defect classes
a first-time visitor would trip over. Runs after site/build.py (locally and in CI);
exits non-zero on findings so regressions surface before publication.

Checks:
  L1  every internal link on every page resolves to a generated file
  L2  no mojibake markers (UTF-8 double-encoding, replacement chars) in rendered text
  L3  a model page with captures shows at least one semantic section
  L4  document-class rows never carry the app-shell note; published sources with
      captures have at least one Document-versions entry
  L5  a version page serving a document blob shows extracted text, a no-text
      note, or the structured-facts treatment — never a bare document
  L6  no template placeholders or python repr artifacts leak into pages
  L7  page titles are unique (permalink pages are addressable and distinguishable)
  L9  a non-restricted version page whose SHA-256 has a blob in dist/blob links
      that blob from its Stored-file cell
  L10 captured HTML is never served executable from the blob store
  L11 every state.json entry's source id exists in crawler/sources.json (an
      orphan entry would silently render nowhere)
  L12 at most one rel=canonical per page; JSON-LD blocks parse as JSON
  L13 model pages and reference pages carry a meta description
  L14 sitemap.xml (when present) lists only URLs that resolve to built pages,
      and never a noindexed page
  L15 when GPAI_SITE_URL is set (a domain-mode deploy), the SEO surface must
      exist: sitemap.xml, robots.txt, changes/atom.xml, a canonical on the
      index — and CNAME when GPAI_CUSTOM_DOMAIN is set. A dev-mode build must
      never silently ship to production.
"""
import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DIST = Path(__file__).resolve().parent / "dist"
# internal links are prefix-absolute; the prefix mirrors build.py's env
import os
PREFIX = os.environ.get("GPAI_SITE_ROOT", "/")
if not PREFIX.endswith("/"):
    PREFIX += "/"

MOJIBAKE = re.compile(r"â€|Ã¢|Ã©|Ã¨|�")
PLACEHOLDER = re.compile(r"\{[a-z_]+\}|<class '|Traceback \(most recent")
LINK = re.compile(r"href='([^']+)'|href=\"([^\"]+)\"")
# the verbatim third-party extract on version pages: the provider's typography,
# not this site's rendering — L2/L6 police the generator's own output only
EXTRACT_BLOCK = re.compile(r"<pre class='extract'>.*?</pre>", re.S)


DIST_SIZE_LIMIT_MB = 800
# the corpus the built site must account for (L19); only consulted for a real
# build, i.e. one that wrote ledger.json
DATA = Path(__file__).resolve().parent.parent / "data"

def main() -> int:
    import json
    findings = []
    noindexed = set()
    # generated pages only: copied capture artifacts (raw.html) are archived
    # third-party documents, not subject to site checks
    pages = sorted(p for p in DIST.rglob("*.html")
                   if p.name in ("index.html", "about.html", "404.html"))
    if not pages:
        print("site/lint: no pages found — run site/build.py first")
        return 1

    titles = {}
    n_version_pages = 0
    for p in pages:
        html = p.read_text(encoding="utf-8")
        rel = p.relative_to(DIST).as_posix()

        # L1 internal links resolve. Prefix-absolute links resolve against
        # the dist root; directory URLs must contain an index.html.
        for m in LINK.finditer(html):
            href = (m.group(1) or m.group(2) or "").split("#")[0]
            if not href or href.startswith(("http://", "https://", "mailto:")):
                continue
            if href.startswith(PREFIX):
                target = DIST / href[len(PREFIX):]
            elif href.startswith("/"):
                findings.append(f"L1 root link outside the site prefix on {rel}: {href}")
                continue
            else:
                target = p.parent / href
            target = target.resolve()
            if target.is_dir():
                target = target / "index.html"
            if not target.exists():
                findings.append(f"L1 broken link on {rel}: {href}")

        parts = rel.split("/")

        # L2 mojibake. U+FFFD is exempt when the page carries the extraction note
        # explaining a source-font limitation; double-encoding markers never are.
        scan = EXTRACT_BLOCK.sub("", html)
        mm = MOJIBAKE.search(scan)
        if mm and not (mm.group(0) == "�" and "are shown as �" in html):
            findings.append(f"L2 mojibake marker {mm.group(0)!r} on {rel}")

        # L8 masthead must link About on every page
        if f'href="{PREFIX}about/"' not in html and "about.html" not in html:
            findings.append(f"L8 no About link on {rel}")

        # L6 template/repr leakage
        pm = PLACEHOLDER.search(scan)
        if pm:
            findings.append(f"L6 template/repr artifact {pm.group(0)!r} on {rel}")

        # L7 title uniqueness
        tm = re.search(r"<title>(.*?)</title>", html, re.S)
        title = (tm.group(1).strip() if tm else "")
        if title in titles:
            findings.append(f"L7 duplicate title '{title}': {rel} vs {titles[title]}")
        else:
            titles[title] = rel

        # model pages: L3/L4
        if len(parts) == 4 and parts[0] == "ledger" and parts[3] == "index.html":
            has_captures = "v/" in html
            has_section = any(s in html for s in
                              ("Document versions", "Watch-surface captures",
                               "Captures of superseded target URLs"))
            if has_captures and not has_section:
                findings.append(f"L3 model page with captures but no semantic section: {rel}")
            if "class='tag tag-published'" in html and has_captures \
                    and "Document versions" not in html:
                findings.append(f"L4 published source lacks a Document-versions entry: {rel}")
            if "Document versions" in html:
                doc_section = html.split("Document versions", 1)[1].split("<h2>", 1)[0]
                if "JS app shell" in doc_section:
                    findings.append(f"L4 app-shell note inside Document versions: {rel}")

        # L12 canonical sanity + JSON-LD validity
        n_canon = html.count('rel="canonical"')
        if n_canon > 1:
            findings.append(f"L12 {n_canon} canonical links on {rel}")
        for jm in re.finditer(
                r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
            try:
                json.loads(jm.group(1))
            except Exception:
                findings.append(f"L12 unparseable JSON-LD on {rel}")

        # L13 indexable model/reference pages need a meta description
        is_noindex = 'name="robots" content="noindex"' in html
        is_model_page = len(parts) == 4 and parts[0] == "ledger"
        is_ref_page = len(parts) <= 2 and rel != "404.html"
        if (is_model_page or is_ref_page) and not is_noindex \
                and 'name="description"' not in html:
            findings.append(f"L13 no meta description on {rel}")

        if is_noindex:
            noindexed.add("/" + rel[: -len("index.html")].rstrip("/") + "/"
                          if rel.endswith("index.html") else "/" + rel)

        # version pages: L5 + L9
        if len(parts) == 6 and parts[0] == "ledger" and parts[3] == "v":
            sha_m = re.search(r"SHA-256</th><td><code>([0-9a-f]{64})</code>", html)
            stored_m = re.search(r"Stored file</th><td>(.*?)</td>", html, re.S)
            restricted = "not served (" in (stored_m.group(1) if stored_m else "")

            # L9: if this capture's bytes are in the blob store, the page must
            # link them (restricted pages intentionally do not serve bytes)
            if sha_m and stored_m and not restricted:
                blob_hits = list((DIST / "blob").glob(sha_m.group(1) + ".*"))
                blob_files = [b.name for b in blob_hits
                              if not b.name.endswith(".ots")]
                if blob_files and not any(f"blob/{name}" in stored_m.group(1)
                                          for name in blob_files):
                    findings.append(f"L9 blob exists but Stored-file cell does not "
                                    f"link it: {rel}")

            # L17: a capture with a proof in the blob store must link ITS OWN proof
            # (the content-addressed name is shared by identical captures)
            if sha_m:
                own = list((DIST / "blob").glob(f"{sha_m.group(1)}.*.{parts[4]}.ots"))
                if own and not any(f"blob/{p.name}" in html for p in own):
                    findings.append(f"L17 version page does not link its own proof "
                                    f"{own[0].name}: {rel}")
            # L18: the displayed hash must be the linked blob's content address
            if sha_m and stored_m:
                for linked in re.findall(r"blob/([0-9a-f]{64})", stored_m.group(1)):
                    if linked != sha_m.group(1):
                        findings.append(f"L18 SHA-256 cell {sha_m.group(1)[:12]}… links a "
                                        f"different blob {linked[:12]}…: {rel}")
            n_version_pages += 1

            # L5: a document-format capture must show extracted text, a no-text
            # note, or the structured-facts treatment
            is_doc_fmt = bool(stored_m and not restricted and re.search(
                r"[0-9a-f]{64}\.(pdf|zip|md|docx|doc|txt)\b", stored_m.group(1)))
            has_text = "<h2>Extracted text" in html
            has_note = ("No text extracted for this format" in html
                        or "app shell" in html
                        or "<h2>Structured facts</h2>" in html)
            if is_doc_fmt and not has_text and not has_note:
                findings.append(f"L5 document version page with no text and no note: {rel}")

    # L10: captured HTML must never be served executable from the blob store
    for f in (DIST / "blob").glob("*.html"):
        findings.append(f"L10 captured HTML served executable: blob/{f.name}")

    # L11: every state entry must belong to a registered source — an orphan key
    # (source deleted or renamed in the registry) renders NOWHERE and would be
    # silent unpublishing. Lint owns this gate because lint blocks the deploy.
    import json
    root = DIST.parent.parent
    state_p = root / "data" / "state.json"
    sources_p = root / "crawler" / "sources.json"
    if state_p.exists() and sources_p.exists():
        state = json.loads(state_p.read_text(encoding="utf-8"))
        ids = {s["id"] for s in json.loads(
            sources_p.read_text(encoding="utf-8"))["sources"]}
        for key in sorted(state):
            if key.split("::", 1)[0] not in ids:
                findings.append(f"L11 state entry has no registry source "
                                f"(renders nowhere): {key}")

    # L14 sitemap consistency: every listed URL resolves to a built page and
    # is not noindexed (contradictory signals)
    sm = DIST / "sitemap.xml"
    if sm.exists():
        for loc in re.findall(r"<loc>([^<]+)</loc>", sm.read_text(encoding="utf-8")):
            path = re.sub(r"^https?://[^/]+", "", loc)
            if not path.startswith(PREFIX):
                findings.append(f"L14 sitemap URL outside site prefix: {loc}")
                continue
            relpath = path[len(PREFIX):]
            target = DIST / relpath
            if target.is_dir() or relpath == "":
                target = DIST / relpath / "index.html"
            if not target.exists():
                findings.append(f"L14 sitemap URL has no built page: {loc}")
            if "/" + relpath.rstrip("/") + "/" in noindexed if relpath else False:
                findings.append(f"L14 noindexed page listed in sitemap: {loc}")

    # L19: a real build (one that wrote ledger.json) must have one version page
    # per version in the corpus — a silently omitted version is a silent
    # omission from the evidence record
    if (DIST / "ledger.json").exists() and (DATA / "state.json").exists():
        state = json.loads((DATA / "state.json").read_text(encoding="utf-8"))
        expected = sum(len(e.get("versions", [])) for e in state.values())
        if n_version_pages != expected:
            findings.append(f"L19 {n_version_pages} version pages built for {expected} "
                            f"versions in data/state.json")

    # L15: a domain-mode deploy must carry its full SEO surface
    site_url = os.environ.get("GPAI_SITE_URL", "")
    if site_url:
        for required in ("sitemap.xml", "robots.txt", "changes/atom.xml"):
            if not (DIST / required).exists():
                findings.append(f"L15 domain-mode build missing {required}")
        if os.environ.get("GPAI_CUSTOM_DOMAIN") and not (DIST / "CNAME").exists():
            findings.append("L15 domain-mode build missing CNAME")
        idx = (DIST / "index.html")
        if idx.exists() and 'rel="canonical"' not in idx.read_text(encoding="utf-8"):
            findings.append("L15 domain-mode index has no canonical")

    # L16: GitHub Pages refuses sites over 1 GB; the built size must stay well under
    total_mb = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file()) / 1e6
    if total_mb > DIST_SIZE_LIMIT_MB:
        findings.append(f"L16 built site is {total_mb:.0f} MB — above the "
                        f"{DIST_SIZE_LIMIT_MB} MB guard (GitHub Pages limit 1 GB)")
    print(f"site/lint: built site {total_mb:.1f} MB")
    print(f"site/lint: {len(pages)} pages checked, {len(findings)} findings")
    for f in findings:
        print("  " + f)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
