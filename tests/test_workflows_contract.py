"""Contract tests for every GitHub workflow: pure YAML/text assertions on
supply-chain pinning, token confinement, publish/health gates, and cross-file
entrypoint existence. Nothing is executed and no network is touched.

NAMES is derived from the directory, so a new workflow is covered by the generic
tests the moment it lands; the explicit set below then fails until someone has
decided what the new one is, which is how decisions.yml came to be covered.

YAML 1.1 quirk: `on:` parses to the boolean key True under yaml.safe_load —
every access to the trigger block goes through that key.
"""
import re
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF_DIR = ROOT / ".github" / "workflows"
NAMES = sorted(p.name for p in WF_DIR.glob("*.yml"))
# adding a workflow must be a decision, not an accident
EXPECTED = {"ledger.yml", "hunt.yml", "verify.yml", "decisions.yml"}

# the eight continue-on-error sweep steps the red-flag gate must re-surface
SWEEP_STEP_IDS = ["registry", "capture", "metahub", "derived", "waybackretry",
                  "otsupgrade", "drift", "verify"]
PUBLISH_GATE_IDS = ["sitebuild", "sitelint", "commit", "verify"]


def raw(name: str) -> str:
    return (WF_DIR / name).read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def doc(name: str) -> dict:
    return yaml.safe_load(raw(name))


def all_steps(wf: dict):
    for job in wf["jobs"].values():
        yield from job.get("steps", [])


def step_by_id(wf: dict, sid: str) -> dict:
    matches = [s for s in all_steps(wf) if s.get("id") == sid]
    assert matches, f"no step with id {sid!r}"
    return matches[0]


def step_by_uses_prefix(wf: dict, prefix: str) -> dict:
    matches = [s for s in all_steps(wf) if str(s.get("uses", "")).startswith(prefix)]
    assert matches, f"no step whose uses starts with {prefix!r}"
    return matches[0]


# --- parse + pinning (all three files) ---

@pytest.mark.parametrize("name", NAMES)
def test_workflow_parses_with_jobs_and_triggers(name):
    wf = doc(name)
    assert isinstance(wf, dict) and isinstance(wf["jobs"], dict) and wf["jobs"]
    assert True in wf  # `on:` is the boolean True key in YAML 1.1
    triggers = wf[True]
    # anything on a schedule must also be runnable by hand — that is how a missed
    # cron is recovered. An event-driven workflow has nothing to dispatch WITH:
    # it needs the event's context (decisions.yml needs the issue comment).
    if "schedule" in triggers:
        assert "workflow_dispatch" in triggers
    else:
        assert triggers, "a workflow with no trigger can never run"


@pytest.mark.parametrize("name", NAMES)
def test_every_uses_is_pinned_to_40_hex_commit_sha(name):
    uses = [str(s["uses"]) for s in all_steps(doc(name)) if "uses" in s]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", ref), \
            f"{name}: not SHA-pinned: {ref}"


@pytest.mark.parametrize("name", NAMES)
def test_every_checkout_disables_persist_credentials(name):
    checkouts = [s for s in all_steps(doc(name))
                 if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts
    for s in checkouts:
        assert s["with"]["persist-credentials"] is False


# --- ledger.yml: token confinement + parachute ---

def test_ledger_commit_step_confines_push_token():
    s = step_by_id(doc("ledger.yml"), "commit")
    assert s["env"]["PUSH_TOKEN"] == "${{ github.token }}"
    # the token reaches git only via the env var, never inlined in the script
    assert "github.token" not in s["run"]
    seturl = [ln for ln in s["run"].splitlines()
              if "git remote set-url origin" in ln]
    assert len(seturl) >= 2
    assert "x-access-token:${PUSH_TOKEN}@" in seturl[0]
    # reset afterwards: the last set-url strips the credential again
    assert "x-access-token" not in seturl[-1] and "PUSH_TOKEN" not in seturl[-1]


def test_ledger_parachute_bundle_step_runs_whenever_the_commit_did_not_succeed():
    # a failed push AND a commit step skipped by cancellation/timeout both leave
    # the day's evidence only on the runner: the parachute must fire for both
    wf = doc("ledger.yml")
    bundles = [s for s in all_steps(wf)
               if "git bundle create" in s.get("run", "")]
    assert len(bundles) == 1
    assert bundles[0]["if"] == "always() && steps.commit.outcome != 'success'"
    assert "git add -A data" in bundles[0]["run"]   # uncommitted captures enter the bundle


def test_ledger_parachute_upload_is_pinned_and_errors_on_missing_bundle():
    s = step_by_uses_prefix(doc("ledger.yml"), "actions/upload-artifact@")
    assert re.fullmatch(r"actions/upload-artifact@[0-9a-f]{40}", s["uses"])
    assert s["if"] == "always() && steps.commit.outcome != 'success'"
    assert s["with"]["if-no-files-found"] == "error"


def test_ledger_commit_step_strips_the_token_on_every_exit_path():
    s = step_by_id(doc("ledger.yml"), "commit")
    exits = [ln for ln in s["run"].splitlines() if "exit 1" in ln]
    assert exits
    for ln in exits:
        assert "git remote set-url origin" in ln and "x-access-token" not in ln, ln


def test_ledger_commit_stages_only_corpus_paths():
    s = step_by_id(doc("ledger.yml"), "commit")
    adds = [ln.strip() for ln in s["run"].splitlines() if ln.strip().startswith("git add")]
    assert adds == ["git add -A data crawler/sources.json crawler/relocations.json reports"]


def test_ledger_job_permissions_are_least_privilege():
    wf = doc("ledger.yml")
    assert wf["jobs"]["sweep"]["permissions"] == {"contents": "write"}
    assert wf["jobs"]["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert "permissions" not in wf   # no workflow-wide grant


# --- ledger.yml: publish + health gates ---

@pytest.mark.parametrize("prefix", ["actions/configure-pages@",
                                    "actions/upload-pages-artifact@"])
def test_ledger_publish_step_requires_all_four_success_outcomes(prefix):
    s = step_by_uses_prefix(doc("ledger.yml"), prefix)
    cond = s["if"]
    assert "||" not in cond  # all-of gate, no escape hatch
    for sid in PUBLISH_GATE_IDS:
        assert f"steps.{sid}.outcome == 'success'" in cond, \
            f"{prefix} if-gate misses {sid}"


def test_ledger_red_flag_step_names_every_sweep_step_id():
    wf = doc("ledger.yml")
    gates = [s for s in all_steps(wf)
             if "exit 1" in s.get("run", "") and "steps.capture.outcome" in s.get("run", "")]
    assert len(gates) == 1
    for sid in SWEEP_STEP_IDS:
        assert f"steps.{sid}.outcome" in gates[0]["run"], \
            f"red-flag gate misses {sid}"


def test_ledger_sweep_steps_all_continue_on_error():
    wf = doc("ledger.yml")
    for sid in SWEEP_STEP_IDS:
        assert step_by_id(wf, sid).get("continue-on-error") is True, \
            f"step {sid} would abort the sweep and strand evidence"


def test_ledger_pytest_install_is_version_pinned():
    installs = [s for s in all_steps(doc("ledger.yml"))
                if "pip install" in s.get("run", "") and "pytest" in s.get("run", "")]
    assert installs
    for s in installs:
        assert re.search(r"pytest==\d+(\.\d+)+", s["run"])


def test_ledger_deploy_job_gated_on_sweep_site_ok():
    wf = doc("ledger.yml")
    deploy = wf["jobs"]["deploy"]
    assert deploy["needs"] == "sweep"
    assert "needs.sweep.outputs.site_ok == 'success'" in deploy["if"]


def test_ledger_site_ok_output_wired_to_pages_artifact_outcome():
    wf = doc("ledger.yml")
    out = wf["jobs"]["sweep"]["outputs"]["site_ok"]
    assert "steps.pagesartifact.outcome" in out
    # and that id belongs to the upload-pages-artifact step, not some other step
    s = step_by_id(wf, "pagesartifact")
    assert s["uses"].startswith("actions/upload-pages-artifact@")


# --- hunt.yml ---

def test_hunt_shares_the_ledger_sweep_concurrency_group():
    hunt, ledger = doc("hunt.yml"), doc("ledger.yml")
    assert hunt["concurrency"]["group"] == "ledger-sweep"
    assert hunt["concurrency"]["group"] == ledger["concurrency"]["group"]
    # sharing the group must queue, not cancel, an in-flight sweep commit
    assert hunt["concurrency"]["cancel-in-progress"] is False


def test_hunt_job_has_timeout_minutes():
    job = doc("hunt.yml")["jobs"]["hunt"]
    assert isinstance(job["timeout-minutes"], int) and job["timeout-minutes"] > 0


# --- verify.yml ---

def test_verify_permissions_are_contents_read_only():
    wf = doc("verify.yml")
    perm_maps = [wf[k] for k in ("permissions",) if k in wf]
    perm_maps += [j["permissions"] for j in wf["jobs"].values() if "permissions" in j]
    assert {"contents": "read"} in perm_maps
    for pm in perm_maps:
        assert all(v == "read" for v in pm.values()), f"write grant found: {pm}"


def test_verify_actionlint_download_is_checksum_verified():
    steps = [s for s in all_steps(doc("verify.yml"))
             if "actionlint" in s.get("run", "")]
    assert steps
    run = steps[0]["run"]
    assert re.search(r"[0-9a-f]{64}\s+\S+.*\|\s*sha256sum -c", run)
    # the checksum line must guard the tarball that actually gets extracted
    assert "curl" in run and "tar -xzf" in run


def test_verify_has_action_pin_self_check_step():
    steps = [s for s in all_steps(doc("verify.yml"))
             if "@[0-9a-f]{40}" in s.get("run", "")]
    assert len(steps) == 1
    assert ".github/workflows" in steps[0]["run"]
    assert "exit 1" in steps[0]["run"]


# --- dependency lock ---

@pytest.mark.parametrize("name", NAMES)
def test_every_requirements_install_uses_the_constraints_lock(name):
    installs = [s["run"] for s in all_steps(doc(name))
                if "pip install" in s.get("run", "") and "crawler/requirements.txt" in s["run"]]
    assert installs, f"{name}: no requirements install step"
    for run in installs:
        assert "-c crawler/constraints.txt" in run, f"{name}: unconstrained install"


def test_constraints_lock_is_pinned_and_disjoint_from_requirements():
    pin = re.compile(r"^([A-Za-z0-9_.-]+)==[0-9][^\s]*$")

    def names(path):
        out = set()
        for ln in (ROOT / "crawler" / path).read_text(encoding="utf-8").splitlines():
            if ln.strip() and not ln.startswith("#"):
                m = pin.match(ln.strip())
                assert m, f"{path}: unpinned line {ln!r}"
                out.add(m.group(1).lower().replace("_", "-"))
        return out
    req, con = names("requirements.txt"), names("constraints.txt")
    assert req and con and not (req & con)


# --- cross-file: every scripted entrypoint exists ---

def test_every_python_entrypoint_in_workflows_exists_on_disk():
    entrypoints = set()
    for name in NAMES:
        entrypoints.update(re.findall(r"python\s+(\S+\.py)", raw(name)))
    # regex sanity: the sweep, hunt and verify entrypoints must all be present
    assert {"crawler/run_capture.py", "crawler/site_hunt.py",
            "crawler/verify_corpus.py", "site/build.py"} <= entrypoints
    for ep in sorted(entrypoints):
        assert (ROOT / ep).is_file(), f"workflow runs missing script: {ep}"


def test_every_workflow_is_accounted_for():
    assert set(NAMES) == EXPECTED, (
        "a workflow was added or removed — decide what it is and update EXPECTED")


def test_the_decision_queue_obeys_only_the_owner():
    # anyone may comment on a public repository; the job must not even start
    cond = str(doc("decisions.yml")["jobs"]["record"]["if"])
    assert "author_association == 'OWNER'" in cond
    assert "'decision'" in cond and "labels" in cond
    assert "pull_request == null" in cond


def test_the_decision_queue_checks_the_association_in_code_too():
    dec = (ROOT / "crawler" / "decisions.py").read_text(encoding="utf-8")
    assert 'TRUSTED_ASSOCIATIONS = ("OWNER",)' in dec
    assert "association.upper() not in TRUSTED_ASSOCIATIONS" in dec


def test_the_decision_queue_holds_no_broader_permission_than_it_needs():
    perms = doc("decisions.yml")["jobs"]["record"]["permissions"]
    assert perms == {"contents": "write", "issues": "write"}

