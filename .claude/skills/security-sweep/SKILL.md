---
name: security-sweep
description: Run the tiered security sweep for this repo — Python SAST, dependency advisories, secret scanning, GitHub Actions supply-chain audit, licence provenance, and passive checks on our own site. Use when asked to run a security sweep, check security posture, audit dependencies, scan for secrets, before a release, before the paid FilingBench layer ships, or when someone proposes adding a security tool to CI. Encodes which tools apply to a crawler-plus-static-site with no server and no database, which do not and why, and the bright line against scanning hosts this project does not own.
---

# Security sweep

This project is a Python 3.12 crawler plus a static-site generator: a public
GitHub repo, static files on GitHub Pages, no server, no database, no SQL, no
framework, and **no JavaScript we ship** (the only `<script>` tags are
`type="application/ld+json"` metadata; there is no `<script src=>` anywhere).

Its real attack surface is not a web application. It is:

1. **Unattended CI with write access to a public repo**, fetching
   attacker-controllable bytes from the internet every morning.
2. **Parsers fed hostile input** — PDF, ZIP, OOXML, HTML, YAML, JSON from third
   parties.
3. **A public corpus** that deliberately contains other people's documents and
   URLs, where the cost of a mistake is a licence or privacy problem, not an RCE.

Most of the security-tooling canon does not fit that shape. This skill says so
explicitly rather than running scanners that cannot find anything, because a
sweep that reports "12 tools run, 0 findings" when 8 of them had nothing to scan
teaches the reader to trust a number that means nothing.

---

## 0. The bright line — not negotiable

**Never point an active scanner, fuzzer, path brute-forcer, or exploitation tool
at a host this project does not own.**

This project crawls third parties: `aial.ie`, provider sites (OpenAI, Meta,
Microsoft, Google, Anthropic, Mistral, Cohere, Fastweb, …), `huggingface.co`,
`raw.githubusercontent.com`, `web.archive.org`, OpenTimestamps calendars.
Fetching a published document through the crawler's own guarded path is the
project's purpose. Sending probe payloads, brute-forcing paths, or fuzzing
parameters at any of them is unauthorised access to someone else's systems.

The only domain the operator owns is **www.gpailedger.com** — and even there the
machine is GitHub's Pages edge, not ours. Owning a DNS name does not authorise
scanning GitHub's infrastructure. Against our own site: **passive checks only** —
fetch a page, read the response headers, read the body.

What follows from this, and must not be argued away in a later session:

- Do not install a penetration-testing tool "just to see".
- Do not write a runnable attack invocation into this repo, a runbook, or a
  report — not even commented out.
- If asked to "test the site properly": there is no form, no input, no
  query-string handling and no server-side execution. There is nothing to test
  actively, and the host is not ours.

---

## 1. Ground rules for a run

- **Never modify the repo.** No writes under the project root, no `git` commands
  that write, no installing into the repo's environment. End with
  `git status --porcelain` empty.
- **Isolated venv only.** `python -m venv $SWEEP/venv`, then that venv's
  `Scripts/python.exe -m pip`. Never install into the shared Anaconda
  interpreter.
- `$SWEEP` = a fresh directory under the session scratchpad. Nothing else.
- Report findings as `file:line`. If a tool fails to install or run on this
  host, say exactly that. **Never invent tool output.**

---

## 2. Tier 1 — every run (~3 minutes, pip-installable, no Docker)

```bash
python -m venv "$SWEEP/venv"
"$SWEEP/venv/Scripts/python.exe" -m pip -q install bandit==1.9.4 pip-audit==2.10.1 zizmor==1.30.0
```

### 2.1 Bandit — Python security lint

```bash
"$SWEEP/venv/Scripts/python.exe" -m bandit -q -r crawler site -ll -ii
```

`-ll -ii` (medium+ severity, medium+ confidence) is the only usable setting; the
unfiltered run drowns in `assert` and `try/except/pass`. Runs in ~3 seconds over
7.9k lines. Scan `tests/` separately if at all (`-r tests --skip B101`).

**Triage against the design.** This code deliberately fetches untrusted content
and writes files. A `urlopen` hit is real **only if the call bypasses
`cap.guarded_request`** — that helper applies `_assert_public_http` to the
request, every redirect hop and the final URL, and bounds the body. An XML hit is
real unless the parse refuses DTDs (see §5).

### 2.2 pip-audit — dependency advisories

```bash
"$SWEEP/venv/Scripts/python.exe" -m pip_audit -r crawler/requirements.txt --no-deps -s osv --progress-spinner=off
```

`--no-deps` is correct: `requirements.txt` plus `constraints.txt` already
enumerate the fully resolved set.

**Windows friction, measured — it will recur.** The long form `--service` does
not exist in 2.10.1; use `-s`, or argparse misreads `osv` as a positional.
Auditing `constraints.txt` as one file **hangs on this host** (killed at 5–7
minutes, no output) while the 8-line `requirements.txt` finishes in seconds. On
Linux CI a single invocation is fine.

**Act first on** `cryptography`, `requests`, `urllib3`, `pypdf` — those touch
fetched bytes inside the daily unattended job that holds `contents: write`.

### 2.3 zizmor — workflow token and expansion audit

```bash
"$SWEEP/venv/Scripts/zizmor.exe" --offline .github/workflows/
```

Pure Python wheel, no Rust toolchain, no network with `--offline`. This is the
only tool here that reasons about template expansion, token scope and secret
handling together — which matters more for this project than any SAST rule.

Treat template-injection and excessive-permissions findings on the three cron
workflows holding `contents: write` as real work. Use `--persona=auditor` for a
deeper on-demand pass.

### 2.4 Repo security settings (read-only `gh`)

```bash
gh api repos/gpailedger/gpai-ledger --jq '.security_and_analysis'
gh api -i repos/gpailedger/gpai-ledger/vulnerability-alerts   # 204 = on, 404 = off
gh api --paginate repos/gpailedger/gpai-ledger/secret-scanning/alerts
gh api repos/gpailedger/gpai-ledger/contributors --jq '.[].login'
```

Expected state: `secret_scanning: enabled`, `secret_scanning_push_protection:
enabled`, `dependabot_security_updates: disabled`, and the contributor list
exactly `gpailedger` + `github-actions[bot]`.

**The distinction that matters here:** Dependabot *security updates* open pull
requests, which would add `dependabot[bot]` to the public contributor list — the
project forbids that, deliberately. Dependabot *vulnerability alerts* are a
different feature: they notify and open nothing. Alerts are compatible with the
constraint; updates are not. Do not conflate them.

### 2.5 Working-tree sanity

```bash
git status --porcelain
```

Empty. Review agents and scratch scripts have written JSON into the repo root
before and it reached a commit through `git add -A`. **Stage explicit paths.**

---

## 3. Tier 2 — on demand (slower, or noisier, or needs a decision)

- **Semgrep** — `pip install semgrep`, then `semgrep --config p/python --config
  p/security-audit crawler site`. Broader than Bandit and understands data flow;
  also slower and chattier. Worth running before a release, not every sweep.
- **ScanCode** (`scancode-toolkit`) — licence and origin detection. Unusually
  relevant here: the corpus holds a third party's unlicensed research plus 54
  archived provider documents, and the README makes specific rights claims. Slow;
  sample `data/captures/` rather than scanning all of it, and say what you sampled.
- **actionlint** — already checksum-pinned in `verify.yml`. Complements zizmor:
  actionlint checks workflow correctness, zizmor checks its security.
- **Passive header check on our own site** — plain `curl -I` against
  `www.gpailedger.com`. Most of what a DAST tool would report about static
  hosting is GitHub's configuration, not something this project controls; say
  which findings are actionable by the maintainer and which are not.
- **SonarCloud** — free for public repos, but it is a platform, not a command.
  Over Bandit + Semgrep + zizmor it mostly adds dashboards. Only worth it if
  someone will actually look at them.

---

## 4. Secrets — and the false positive that will bite

Secret scanning is already enabled on the repo with push protection, so GitHub is
the primary control. TruffleHog or `ggshield` add value mainly for history and
for non-provider patterns.

**Before running any secrets scanner, know this:** manifests under
`data/captures/` deliberately record fetch URLs including **expiring signed-URL
tokens** — AWS pre-signed URLs, Meta CDN tokens. They are time-limited access
tokens for public documents, they are part of the evidence, and `README.md` says
secret scanners can allowlist `data/captures/`. Every generic scanner flags them.

The failure mode is not the false positive. It is a future session learning that
"secrets findings in this repo are noise" and waving through a real one. So:

- Allowlist **`data/captures/` specifically** — never a repo-wide suppression.
- A finding **anywhere else** is real until proven otherwise, especially in
  `crawler/`, `.github/`, or git history.
- `local/` is gitignored and holds real keys. Confirm nothing from it has ever
  been committed: `git log --all --full-history -- local/keys`.

---

## 5. Tools that do not apply here — and why

Say this rather than running them. Each line is a checked fact about this repo,
not a general opinion.

| Tool | Why not |
|---|---|
| **Retire.js** | No JavaScript is shipped. Only `<script type="application/ld+json">` metadata; no `<script src=>`; no third-party JS libraries. Nothing to scan. |
| **Brakeman** | Ruby on Rails only. No Ruby in this project. |
| **sqlmap** | No database, no SQL, no query parameters anywhere. |
| **Metasploit** | No service to exploit. A static site and an offline crawler. |
| **w3af** | Web-application scanner; there is no application. |
| **OWASP ZAP / Nikto / Wapiti (active mode)** | Nothing dynamic to scan, and the host is GitHub's. Passive header inspection only — see Tier 2. |
| **OWASP Dependency-Check** | A Java tool needing an NVD API key and a large feed; its Python support is weaker than pip-audit's, which targets the ecosystem directly. Use pip-audit. |
| **GitGuardian** | Overlaps GitHub secret scanning, which is already on. Adds a vendor account for marginal gain here. |

---

## 6. Known state, last verified 1 Sep 2026

All three tools are gates in `verify.yml` (weekly) and exit 0 on the current tree.

- **Bandit:** 0 findings at `-ll -ii`. Two mediums were found and closed at the
  cause, not suppressed: `ElementTree` parsing an OOXML part (real — see below),
  and a raw `urlopen` in the harvest, which now goes through
  `cap.guarded_request` like every other outbound call. One `# nosec B314`
  remains on the XML parse, with the reason in the comment above it.
- **pip-audit:** `No known vulnerabilities found` across the pinned set.
- **zizmor:** `No findings to report` (35 suppressed). The template expansion in
  `decisions.yml` now reaches the shell through `env:` as data.
- **Repo:** secret scanning **on**, push protection **on**, Dependabot
  vulnerability **alerts on**, Dependabot **security updates off**, contributors
  exactly `gpailedger` + `github-actions[bot]`.

### The one real vulnerability this sweep found

`ElementTree` refuses *external* entities — verified, a `SYSTEM` entity raises
`undefined entity` — but it *expands internal ones*, and a billion-laughs DTD
expanded in testing. The OOXML member-size cap bounds the bytes read, not what
they expand to, so a few KB of `.docx` fetched from a third party could exhaust
the unattended runner. `capture.py` now refuses any OOXML part carrying a DTD or
entity declaration; a legitimate part never has one. Three tests cover it: the
bomb, the external entity, and an ordinary document.

The lesson worth keeping: the code carried a comment asserting the parser was
safe. It was half right, and nobody had tested the other half. **Test the claim,
do not read it.**
