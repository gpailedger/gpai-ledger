"""Tier-0 discovery: new model releases from tracked organizations on HuggingFace.

The HF API is keyless and machine-readable — this is a deterministic new-model feed
independent of any third-party tracker. Models created within the window are written
to reports/org-watch-latest.md as hunt candidates (the weekly hunts pick them up).
This script only reports; it never adds registry sources — a candidate becomes a
tracked source after a hunt verifies what it is.

Run weekly (hunt.yml): python crawler/org_watch.py [--days N]
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import capture as cap

ROOT = Path(__file__).resolve().parent.parent

# Organizations whose releases are candidates: every provider in the corpus with an
# HF presence, plus majors known to publish GPAI models.
HF_ORGS = [
    "openai", "meta-llama", "mistralai", "CohereLabs", "ibm-granite", "microsoft",
    "google", "deepseek-ai", "black-forest-labs", "swiss-ai", "speakleash",
    "HuggingFaceTB", "Writer", "ServiceNow-AI", "xai-org", "Qwen", "moonshotai",
    "briaai", "stabilityai", "aleph-alpha",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [f"# HF org watch — {now}", "",
             f"New models created in the last {args.days} days by tracked orgs "
             f"(candidates for the summary hunt; not yet tracked sources).", ""]
    found = 0
    for org in HF_ORGS:
        try:
            r = requests.get("https://huggingface.co/api/models",
                             params={"author": org, "sort": "createdAt",
                                     "direction": "-1", "limit": "20"},
                             headers=cap.HEADERS, timeout=30)
            r.raise_for_status()
            models = r.json()
        except Exception as exc:  # noqa: BLE001
            lines.append(f"- {org}: API error {exc!r}")
            continue
        if not isinstance(models, list):
            lines.append(f"- {org}: unexpected API response shape; skipped")
            continue
        fresh = []
        for m in models:
            if not isinstance(m, dict) or "id" not in m:
                continue
            created = m.get("createdAt") or ""

            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt >= cutoff:
                fresh.append((m["id"], created[:10], m.get("downloads", 0)))
        for mid, created, dl in fresh:
            lines.append(f"- **{mid}** (created {created}, {dl:,} downloads)")
            found += 1
    if not found:
        lines.append("No new models in the window — nothing to hunt from this feed.")
    lines.append("")

    out = ROOT / "reports" / "org-watch-latest.md"
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"org-watch: {found} new model(s) across {len(HF_ORGS)} orgs -> {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
