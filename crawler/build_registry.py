"""Build crawler/sources.json from AIAL's eval metadata + independently verified target URLs.

Usage: python build_registry.py <path-to-aial-repo-clone>

AIAL (aial.ie, Trinity College Dublin / ADAPT) publishes per-model eval YAMLs with the
provider's live summary URL, dates, and their write-once archive filename. We seed the
registry from that metadata with attribution, then add direct document URLs verified at
Gate 1 and the Commission's own regulatory baseline documents.
"""
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Display names normalized to the provider's official form (AIAL metadata uses
# spaced variants; microsoft.ai uses hyphenated names).
MODEL_NAME_OVERRIDES = {
    "MAI Code 1 Flash": "MAI-Code-1-Flash",
    "MAI Cyber 1 Flash": "MAI-Cyber-1-Flash",
    "MAI Image 2": "MAI-Image-2",
    "MAI Image 2.5": "MAI-Image-2.5",
}

AIAL_ARCHIVE_BASE = "https://aial.ie/research/gpai-training-transparency/archive/"
AIAL_EVAL_BASE = "https://aial.ie/research/gpai-training-transparency/evals/"

# URLs dropped from AIAL metadata: mechanically dead links (expired pre-signed URLs,
# login-gated repos) and dead links whose content has a verified successor tracked in
# EXTRA_TARGETS. A dead link with no known successor stays in the registry so its
# recurring error event remains the record of continued absence.
DROP_URL_PREFIXES = (
    # Cohere: pre-signed URL (expired); document now captured via derived_targets.py
    "https://fdr-prod-docs-files-public.s3",
    # Bria: Drive URL dead since 11 Aug 2026; content relocated to bria.ai/eu-policy
    # (see reports/2026-08-17-bria-relocation.md)
    "https://drive.google.com/file/d/13BhHQhd7vcDArPmpBrl_b2FpdBqkYU4n",
    # Apertus 1.5: HF repo is login-gated (HTTP 401); public copy tracked in
    # EXTRA_TARGETS from the provider's apertus-legal GitHub repo
    "https://huggingface.co/swiss-ai/Apertus-v1.5-70B",
)

# Corrections to AIAL metadata bugs; drop each override once the upstream eval YAML
# is fixed. muse-spark.yaml's archive_file_name points at Gemini_2026_07_21.pdf
# (Google's document); the correct Muse Spark file exists in their archive folder.
ARCHIVE_FILE_OVERRIDES = {
    "muse-spark": "Muse_Spark_2026_07_21.pdf",
}

# Providers that publish the summary as in-page web content rather than a document
# file. Their rendered-page captures ARE the document versions (the site classifies
# on this flag), not watch surfaces.
INPAGE_DOC_URLS = {
    "https://bria.ai/eu-policy",
    "https://www.domyn.com/summary-of-training-data/domyn-large",
    "https://huggingface.co/spaces/hfmlsoc/smollm3-eu-data-transparency",
}

# JS-heavy pages that serve only an app shell to plain HTTP clients: capture the
# rendered DOM via headless Chromium (capture.fetch_rendered). Matched by URL prefix.
RENDER_URL_PREFIXES = (
    "https://bria.ai/eu-policy",
    "https://trust.anthropic.com/",
    "https://transparency.meta.com/",
    "https://bfl.ai/transparency",
    "https://thinkingmachines.ai/training-data-documentation",
    "https://huggingface.co/spaces/hfmlsoc/smollm3-eu-data-transparency",
    "https://www.domyn.com/summary-of-training-data/",
    "https://docs.cohere.com/docs/command-a-plus",
)


TRACKING_PARAMS = ("msockid", "fbclid", "gclid", "utm_source", "utm_medium",
                   "utm_campaign", "utm_term", "utm_content")


def normalize_url(url: str) -> str:
    url = url.strip()
    # strip analytics/tracking params: they are operator-session artifacts, not part
    # of the canonical document location, and would be baked into evidence records
    if "?" in url:
        base, _, qs = url.partition("?")
        kept = [kv for kv in qs.split("&")
                if kv.split("=")[0].lower() not in TRACKING_PARAMS]
        url = base + ("?" + "&".join(kept) if kept else "")
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    # HF /blob/ pages are HTML viewers; /resolve/ serves the document itself
    if "huggingface.co/" in url and "/blob/" in url:
        url = url.replace("/blob/", "/resolve/")
    return url

# Manually verified direct document URLs, added when AIAL's link differs
# (e.g. AIAL links a portal/hub; these point at the document itself).
# Provider-objection flags (docs/runbooks.md): source id -> short public reason.
# A restricted source's version pages publish structured facts + provenance only —
# no document bytes, no extracted text. Registry rebuilds preserve the flag because
# it lives here, not in the generated sources.json.
RESTRICTED_SOURCES = {}

EXTRA_TARGETS = {
    "gpt-5-6-luna": [
        ("provider-live", "https://cdn.openai.com/pdf/gpt-5-6-luna-eu-ai-act-public-summary-of-training-content.pdf"),
    ],
    "gemini-3-pro": [
        ("provider-live", "https://storage.googleapis.com/transparencyreport/report-downloads/pdf-report-ii_2026-7-2_2026-7-2_en_v1.pdf"),
    ],
    "grok-4-5": [
        ("provider-live", "https://media.x.ai/v1/website/public-summary-of-training-content-for-grok-4.5_8jul2026.docx-fc25014a.pdf"),
    ],
    "nova-2-lite": [
        ("provider-live", "https://docs.aws.amazon.com/ai/responsible-ai/nova-2-lite/samples/nova-2-lite-training-data-summary.zip"),
        ("provider-page", "https://docs.aws.amazon.com/ai/responsible-ai/nova-2-lite/overview.html"),
    ],
    "phi-4": [
        ("provider-live", "https://huggingface.co/microsoft/phi-4/raw/main/data_summary_card.md"),
    ],
    # Found by this project 11 Aug 2026; AIAL has since adopted both URLs (their
    # metadata now carries them) — kept as fallbacks. GPT Image 2 is
    # filed under the product name "ChatGPT Images 2.0"; MAI Cyber's file breaks
    # Microsoft's naming pattern ("-Data-Card.pdf" vs "-Data-Summary.pdf").
    "gpt-image-2": [
        ("provider-live", "https://cdn.openai.com/pdf/chatgpt-images-2-0-eu-ai-act-public-summary-of-training-content.pdf"),
    ],
    "mai-cyber-1-flash": [
        ("provider-live", "https://microsoft.ai/pdf/MAI-Cyber-1-Flash-Data-Card.pdf"),
    ],
    # Locations verified by manual browser check, 17 Aug 2026:
    "bria": [
        ("provider-live", "https://bria.ai/eu-policy"),
    ],
    "command-a-plus": [
        ("provider-page", "https://docs.cohere.com/docs/command-a-plus"),
    ],
    "apertus-1-5": [
        ("provider-live", "https://raw.githubusercontent.com/swiss-ai/apertus-legal/main/apertus_1.5/Apertus_1_5_EU_Public_Summary.pdf"),
        ("cop-doc", "https://raw.githubusercontent.com/swiss-ai/apertus-legal/main/apertus_1.5/Apertus_1_5_EU_Code_of_Practice.pdf"),
    ],
    # Direct document URLs mined from rendered page DOMs, 17 Aug 2026:
    "inkling": [
        ("provider-live", "https://thinkingmachines.ai/documents/inkling-public-summary-training-content.pdf"),
    ],
    "inkling-small": [
        ("provider-live", "https://thinkingmachines.ai/documents/inkling-small-public-summary-training-content.pdf"),
    ],
    "flux-3": [
        # content-addressed CDN file linked from bfl.ai/transparency (URL changes on
        # re-upload; the rendered page watch catches the link swap)
        ("provider-live", "https://cdn.sanity.io/files/2gpum2i6/production/f9dbcc8cc160256d7102a365f42be4ae33286c3c.pdf"),
    ],
    "fibo": [
        ("provider-live", "https://drive.google.com/uc?export=download&id=1z3JFPBQCRKdj5F-Qfgp4TaNITWcMZtOi"),
    ],
}

# Tier-0 model-universe discovery: catalog pages watched so a NEW MODEL from a
# tracked provider, or a NEW PROVIDER self-declaring GPAI status, surfaces as a
# change event here without depending on AIAL noticing it first. A change on one of
# these pages feeds the weekly hunts (HUNTING.md).
CATALOG_SOURCES = [
    ("openai/model-catalog", "OpenAI", "Model catalog (developer docs)",
     "https://developers.openai.com/api/docs/models"),
    ("anthropic/model-catalog", "Anthropic", "Model catalog (developer docs)",
     "https://platform.claude.com/docs/en/about-claude/models/overview"),
    ("mistral-ai/model-catalog", "Mistral AI", "AI-governance model list",
     "https://legal.mistral.ai/ai-governance/models"),
    ("cohere/model-catalog", "Cohere", "Model catalog (developer docs)",
     "https://docs.cohere.com/docs/models"),
    ("xai/model-catalog", "xAI", "Model catalog (developer docs)",
     "https://docs.x.ai/developers/models"),
    ("microsoft/model-catalog", "Microsoft", "MAI models index",
     "https://microsoft.ai/models/"),
]

# Regulatory baseline + discovery feeds, independent of any provider.
STANDALONE_SOURCES = [
    {
        "id": "eu-commission/explanatory-notice-and-template",
        "provider": "European Commission",
        "model": "Explanatory Notice + Template C(2025) 8311 final",
        "status": "regulatory",
        "note": "The Commission's official template and explanatory notice that every "
                "provider summary must follow — archived because summaries are judged "
                "against it, and so template changes are themselves on the record. One "
                "capture uses the German regulator BNetzA's official mirror of the "
                "original July 2025 issue (C(2025) 5235) for reissue comparison.",
        "targets": [
            {"kind": "regulatory", "url": "https://ec.europa.eu/newsroom/dae/redirection/document/118480",
             "note": "Official Notice+Template PDF (EN), as served by the EC library page"},
            {"kind": "regulatory", "url": "https://ec.europa.eu/newsroom/dae/redirection/document/118578",
             "note": "Official editable Template DOC"},
            {"kind": "regulatory", "url": "https://www.bundesnetzagentur.de/DE/Fachthemen/Digitales/KI/_functions/EU-Template.pdf?__blob=publicationFile&v=2",
             "note": "BNetzA mirror of the original C(2025) 5235 final of 24.7.2025 (for reissue diffing)"},
        ],
    },
    {
        "id": "eu-commission/template-pages",
        "provider": "European Commission",
        "model": "Template library + FAQ pages",
        "status": "watch",
        "note": "The Commission pages that distribute the official template. Monitored "
                "because a template revision, or the launch of the online form / central "
                "registry the Commission has said it aims to provide, would appear here "
                "first.",
        "targets": [
            {"kind": "watch-page", "url": "https://digital-strategy.ec.europa.eu/en/library/explanatory-notice-and-template-public-summary-training-content-general-purpose-ai-models",
             "note": "Watch: online-form/registry launch would appear here first"},
            {"kind": "watch-page", "url": "https://digital-strategy.ec.europa.eu/en/faqs/template-general-purpose-ai-model-providers-summarise-their-training-content",
             "note": "Commission FAQ on the template"},
        ],
    },
    {
        "id": "microsoft/mai-thinking-1",
        "provider": "Microsoft",
        "model": "MAI-Thinking-1",
        "status": "published",
        "targets": [
            {"kind": "provider-live", "url": "https://microsoft.ai/pdf/MAI-Thinking-1-Data-Summary.pdf",
             "note": "found by Tier-3 hunt 17 Aug 2026; summary v1.0 dated 12 Aug 2026; "
                     "not linked from the model page as of discovery (found via the "
                     "/pdf/ filename convention)"},
        ],
    },
    {
        "id": "microsoft/mai-code-1-1-flash",
        "provider": "Microsoft",
        "model": "MAI-Code-1.1-Flash",
        "status": "published",
        "targets": [
            {"kind": "provider-live", "url": "https://microsoft.ai/pdf/MAI-Code-1.1-Flash-Data-Card.pdf",
             "note": "found by Tier-3 hunt 17 Aug 2026; Data Card v1.0 uploaded 11 Aug 2026"},
        ],
    },
    {
        "id": "microsoft/mai-voice-2",
        "provider": "Microsoft",
        "model": "MAI-Voice-2",
        "status": "missing",
        "targets": [],
        "note": "on microsoft.ai/models index by 17 Aug 2026 with no training-content "
                "summary found; absent from AIAL metadata",
    },
    {
        "id": "microsoft/mai-transcribe-1-5",
        "provider": "Microsoft",
        "model": "MAI-Transcribe-1.5",
        "status": "missing",
        "targets": [],
        "note": "on microsoft.ai/models index by 17 Aug 2026 with no training-content "
                "summary found; absent from AIAL metadata",
    },
    {
        "id": "google/transparency-bucket",
        "provider": "Google",
        "model": "Transparency-report file store (watched for summary drops)",
        "note": "Google serves its Art. 53 summaries as PDFs from this public storage "
                "location. Its machine-readable file listing is captured so a new or "
                "replaced summary file shows up as a change here before anything links "
                "to it.",
        "status": "watch",
        "targets": [
            {"kind": "watch-page",
             "url": "https://storage.googleapis.com/transparencyreport?prefix=report-downloads/",
             "note": "anonymous XML object listing of the bucket serving Google's "
                     "Art. 53 summaries — new summary PDFs appear here as new objects"},
        ],
    },
    # meta/muse-glimmer was a standalone entry while AIAL did not track the model
    # (found by this project 17 Aug 2026). AIAL added its own eval on 18 Aug, so the
    # AIAL-derived source now carries it; meta_hub.py keys captures on the same
    # source id, so nothing changes on the capture side.
    {
        "id": "anthropic/trust-center-bundle",
        "provider": "Anthropic",
        "model": "Anthropic trust-center bundle",
        "status": "watch",
        "targets": [],
        "note": "The bulk download publicly offered on trust.anthropic.com "
                "('Download all documents'). It contains Anthropic's Art. 53(1)(d) "
                "training-data summaries for 7 Claude models in 6 documents (Opus 4.7, "
                "Opus 4.8, Opus 5, Sonnet 5, Mythos Preview, and Mythos 5 + Fable 5 "
                "jointly), alongside their other public trust documents. Captured on a "
                "stable key so the rotating download token never mints versions; inner "
                "per-file SHA-256s give per-document change tracking.",
    },
    {
        "id": "aial/tracker",
        "provider": "AI Accountability Lab (AIAL)",
        "model": "GPAI Training Transparency tracker",
        "status": "watch",
        "note": "AIAL (AI Accountability Lab, Trinity College Dublin) maintains the "
                "tracker whose metadata seeds this ledger's model list and document "
                "locations. Their pages are archived here so this ledger's provenance "
                "chain includes its own upstream source.",
        "targets": [
            {"kind": "watch-page", "url": "https://aial.ie/research/gpai-training-transparency/list_summaries",
             "note": "Discovery feed: new summaries and archive links appear here"},
            {"kind": "watch-page", "url": "https://aial.ie/research/gpai-training-transparency/",
             "note": "Tracker main page (graded/missing counts)"},
        ],
    },
]


def slugify(stem: str) -> str:
    return stem.lower().replace("_", "-").replace(".", "-").replace(" ", "-")


def org_slug(org: str) -> str:
    return slugify(org.strip()) or "unknown"


# Source ids the operator has deliberately retired (id -> dated reason). A retired
# source is never dropped: the refresh carries its last committed entry forward
# flagged "retired", the sweep stops fetching it, and the site keeps its pages and
# permalinks with a "no longer tracked" note. Every other disappearance of a
# tracked id fails the refresh.
RETIRED_SOURCE_IDS: dict = {}


def main(aial_repo: str, out_path=None) -> None:
    repo = Path(aial_repo)
    evals = sorted((repo / "evals").glob("*.yaml"))
    if not evals:
        sys.exit(f"no eval YAMLs found under {repo}/evals")

    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

    sources = []
    for path in evals:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        model = (raw.get("model_name") or path.stem).strip()
        model = MODEL_NAME_OVERRIDES.get(model, model)
        org = (raw.get("organization") or "unknown").strip()
        summary_link = (raw.get("public_summary_link") or "").strip()
        archive_file = (raw.get("archive_file_name") or "").strip()
        slug = slugify(path.stem)
        archive_file = ARCHIVE_FILE_OVERRIDES.get(slug, archive_file)

        targets = []
        seen = set()

        def add(kind: str, url: str, note: str = "") -> None:
            if url and url not in seen:
                seen.add(url)
                t = {"kind": kind, "url": url, **({"note": note} if note else {})}
                if url.startswith(RENDER_URL_PREFIXES):
                    t["render"] = True
                targets.append(t)

        if summary_link:
            normalized = normalize_url(summary_link)
            if not normalized.startswith(DROP_URL_PREFIXES):
                # the note must be literally true: when normalization changed the
                # URL (tracking params stripped, HF viewer rewritten to the
                # direct-download form), say so
                note = ("public_summary_link per AIAL eval metadata"
                        if normalized == summary_link.strip()
                        else "public_summary_link per AIAL eval metadata, "
                             "normalized (tracking tokens stripped; HF viewer "
                             "URLs rewritten to the direct-download form)")
                add("provider-live", normalized, note)
        extra_doc = False
        for kind, url in EXTRA_TARGETS.get(slug, []):
            add(kind, url, "manually verified direct URL")
            if kind == "provider-live":
                extra_doc = True
        if archive_file:
            add("aial-archive", AIAL_ARCHIVE_BASE + archive_file,
                "AIAL write-once snapshot (attribution: aial.ie)")

        sid = f"{org_slug(org)}/{slug}"
        sources.append({
            "id": sid,
            "provider": org,
            "model": model,
            "status": "published" if (archive_file or extra_doc) else "missing",
            **({"restricted": RESTRICTED_SOURCES[sid]}
               if sid in RESTRICTED_SOURCES else {}),
            "targets": targets,
            "aial": {
                "eval_yaml": f"evals/{path.name}",
                "eval_page": AIAL_EVAL_BASE + path.stem + "/",
                "public_summary_date": (raw.get("public_summary_date") or "").strip(),
                "model_publication_date": (raw.get("model_publication_date") or "").strip(),
                "evaluation_date": (raw.get("evaluation_date") or "").strip(),
                "category": (raw.get("category") or "").strip(),
                "archive_file_name": archive_file,
            },
        })

    # STANDALONE ids are hand-authored; guard against a future collision with an
    # AIAL-derived id (which would produce two sources sharing one id)
    aial_ids = {s["id"] for s in sources}
    for s in STANDALONE_SOURCES:
        if s["id"] in aial_ids:
            raise SystemExit(f"standalone source id {s['id']} collides with an AIAL-derived id")
    for cid, prov, label, url in CATALOG_SOURCES:
        sources.append({
            "id": cid, "provider": prov, "model": label, "status": "watch",
            "note": "Model-catalog watch: a new model appearing on this page becomes a "
                    "candidate for the summary hunt, independent of any third-party "
                    "tracker.",
            "targets": [{"kind": "watch-page", "url": url,
                         "note": "provider model catalog (Tier-0 discovery)"}],
        })
    sources.append({
        "id": "eu-commission/gpai-code-of-practice",
        "provider": "European Commission",
        "model": "GPAI Code of Practice page (signatories)",
        "status": "watch",
        "note": "Provider-universe feed: organisations signing the GPAI Code of "
                "Practice self-declare GPAI-provider status. A new signatory here is a "
                "new candidate provider for the hunt, independent of any tracker.",
        "targets": [{"kind": "watch-page",
                     "url": "https://digital-strategy.ec.europa.eu/en/policies/contents-code-gpai",
                     "note": "CoP page incl. signatories (Tier-0 discovery)"}],
    })
    sources.append({
        "id": "osai-index/tracker",
        "provider": "European Open Source AI Index",
        "model": "OSAI index (open-source model releases)",
        "status": "watch",
        "note": "New-entrant feed for open-source GPAI providers (the index AIAL's own "
                "discovery methodology used). Changes here surface new providers and "
                "models for the hunt.",
        "targets": [{"kind": "watch-page", "url": "https://osai-index.eu/",
                     "note": "OSAI index front page (Tier-0 discovery)"}],
    })

    sources.extend(STANDALONE_SOURCES)

    for s in sources:
        for t in s.get("targets", []):
            if t["url"] in INPAGE_DOC_URLS:
                t["inpage"] = True

    # merge fingerprint-confirmed relocations found by site_hunt.py — AFTER standalone
    # sources are appended, so a relocation for a standalone id (e.g. a Microsoft or
    # Google source) is not silently dropped
    reloc_path = Path(__file__).parent / "relocations.json"
    if reloc_path.exists():
        relocations = json.loads(reloc_path.read_text(encoding="utf-8"))
        by_id = {s["id"]: s for s in sources}
        for sid, relos in relocations.items():
            s = by_id.get(sid)
            if not s:
                print(f"  WARNING: relocation for unknown source id {sid}")
                continue
            for r in relos:
                if r["url"] not in {t["url"] for t in s["targets"]}:
                    s["targets"].append({"kind": r["kind"], "url": r["url"],
                                         "note": r["note"]})
                    if s["status"] == "missing":
                        s["status"] = "published"

    # annotate flags LAST so relocation-merged and standalone targets get them too
    for s in sources:
        for t in s.get("targets", []):
            if t["url"].startswith(RENDER_URL_PREFIXES):
                t["render"] = True
            if t["url"] in INPAGE_DOC_URLS:
                t["inpage"] = True

    out = Path(out_path) if out_path else Path(__file__).parent / "sources.json"
    prev_sources = ({s["id"]: s for s in json.loads(out.read_text(encoding="utf-8"))
                     .get("sources", [])} if out.exists() else {})
    new_ids = {s["id"] for s in sources}
    for rid, reason in RETIRED_SOURCE_IDS.items():
        if rid in new_ids:
            for s in sources:
                if s["id"] == rid:
                    s["retired"] = reason
        elif rid in prev_sources:
            carried = dict(prev_sources[rid])
            carried["retired"] = reason
            sources.append(carried)

    status_counts = {}
    for s in sources:
        status_counts[s["status"]] = status_counts.get(s["status"], 0) + 1
    registry = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seed": {"aial_repo": "https://github.com/AIAccountabilityLab/gpai-training-transparency",
                 "aial_head": head},
        "counts": {**status_counts, "total": len(sources)},
        "sources": sources,
    }

    # Fail closed: the refresh must never drop a source the committed registry
    # already tracks — an upstream rename or removal would otherwise unpublish
    # that model's permalinks. The step fails, the committed registry stays in
    # force, and the run reddens so the change is handled deliberately.
    missing = sorted(set(prev_sources) - {s["id"] for s in sources})
    if missing:
        sys.exit(f"refusing to write a registry that drops {len(missing)} tracked "
                 f"source id(s): {', '.join(missing[:8])} — retire them in "
                 f"RETIRED_SOURCE_IDS (with a dated reason) or remap them deliberately")
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"wrote {out} — {status_counts} total={len(sources)}; sha256={digest[:16]}…")
    for s in sources:
        if s["status"] == "missing":
            print(f"  MISSING: {s['provider']}: {s['model']}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
