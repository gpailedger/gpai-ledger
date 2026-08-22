import hashlib
import json
import sys
from pathlib import Path

import pytest

# make crawler/ and site/ importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crawler"))
sys.path.insert(0, str(ROOT / "site"))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canon_sha(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def make_ots(digest_hex: str) -> bytes:
    """A real, offline-parseable DetachedTimestampFile whose file digest is
    digest_hex, carrying one pending attestation (no network involved)."""
    from opentimestamps.core.notary import PendingAttestation
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

    ts = Timestamp(bytes.fromhex(digest_hex))
    ts.attestations.add(PendingAttestation(
        "https://alice.btc.calendar.opentimestamps.org"))
    dtf = DetachedTimestampFile(OpSHA256(), ts)
    ctx = BytesSerializationContext()
    dtf.serialize(ctx)
    return ctx.getbytes()


class CorpusBuilder:
    """Builds a synthetic but fully-valid corpus under a tmp data root: capture
    dirs with raw bytes + extracted.txt + manifest + .ots, a consistent
    state.json, and an events.jsonl whose 'new' events match the dirs.
    Call .finish() after adding captures; mutate files afterwards to break
    specific invariants."""

    def __init__(self, data_root: Path):
        self.root = Path(data_root)
        (self.root / "captures").mkdir(parents=True, exist_ok=True)
        self.state = {}
        self.events = []

    def add_capture(self, source_id="prov/model", tslug="provider-live-aaaa1111",
                    ts="20260815T060000Z", raw=b"%PDF-1.4 fixture", ext=".pdf",
                    text="fixture text body", provider="Prov", model="Model",
                    kind="provider-live", url="https://example.org/doc.pdf",
                    with_ots=True, wayback=None, managed=None,
                    extra_manifest=None):
        d = self.root / "captures" / source_id.replace("/", "__") / tslug / ts
        d.mkdir(parents=True, exist_ok=False)
        (d / f"raw{ext}").write_bytes(raw)
        text_sha = None
        if text is not None:
            (d / "extracted.txt").write_text(text, encoding="utf-8", newline="\n")
            text_sha = canon_sha(text)
        raw_sha = sha(raw)
        key = f"{source_id}::{tslug}"
        prior = self.state.get(key, {}).get("last_sha256")
        manifest = {
            "source_id": source_id, "provider": provider, "model": model,
            "target_kind": kind, "sha256": raw_sha, "size_bytes": len(raw),
            "stored_as": f"raw{ext}", "text_sha256": text_sha,
            "extraction_notes": [],
            "http": {"url": url, "final_url": url, "status_code": 200,
                     "content_type": "application/pdf", "etag": None,
                     "last_modified": None, "content_length": str(len(raw)),
                     "fetched_at": "2026-08-15T06:00:00Z"},
            "prior_sha256": prior,
        }
        if wayback is not None:
            manifest["wayback"] = wayback
        if with_ots:
            manifest["ots"] = {"ok": True, "calendars": []}
            (d / f"raw{ext}.ots").write_bytes(make_ots(raw_sha))
        if extra_manifest:
            manifest.update(extra_manifest)
        (d / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8", newline="\n")
        rel = d.relative_to(self.root).as_posix()
        entry = self.state.setdefault(key, {"versions": []})
        entry["last_sha256"] = raw_sha
        entry["last_text_sha256"] = manifest["text_sha256"]
        entry["last_capture"] = rel
        if managed:
            entry["managed"] = managed
        entry["versions"].append({"sha256": raw_sha, "dir": rel})
        self.events.append({"ts": "2026-08-15T06:00:00Z", "source": source_id,
                            "target": tslug, "url": url, "kind": kind,
                            "outcome": "new", "sha256": raw_sha, "dir": rel})
        return d, manifest

    def finish(self):
        (self.root / "state.json").write_text(
            json.dumps(self.state, indent=2), encoding="utf-8", newline="\n")
        with (self.root / "events.jsonl").open("w", encoding="utf-8",
                                               newline="\n") as fh:
            for e in self.events:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        return self.root


@pytest.fixture
def corpus(tmp_path):
    """A CorpusBuilder rooted in a tmp dir; call corpus.finish() when built."""
    return CorpusBuilder(tmp_path / "data")


@pytest.fixture(autouse=True)
def _offline_url_guard(monkeypatch):
    """The suite is offline: the URL guard must never resolve hostnames here
    (its resolution path has dedicated tests with a stubbed resolver)."""
    import capture
    monkeypatch.setattr(capture, "RESOLVE_HOSTS", False)


def load_module(path, name):
    """Import a script by path under a private name (site/build.py, lint.py and
    the crawler entrypoints are scripts, not packages)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
