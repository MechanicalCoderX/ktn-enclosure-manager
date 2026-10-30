# Cloud docs and release-readiness pass, 2026-10-30

Branch `cloud/docs-release`, from main `b8663c2`. Docs only; no product code,
no merge, no PR, no deploy.

## Status
- **Docs reconciled with v1.6.1** in README, SECURITY.md, docs/INSTALL-TRUENAS.md,
  ARCHITECTURE.md and CHANGELOG. Every claim was grepped against the code
  (hit list below). One gap found and documented: if `KTN_ALLOWED_HOSTS`
  leaves out the NAS IP, the by-IP fallback for an expired certificate also
  gets `400`.
- **CHANGELOG:** new `### Documentation` block, plus a one-line note that main
  holds #57, #58 and #59, none released yet (likely 1.6.2). The block sits
  between the `[Unreleased]` heading and `### Fixed`. The #57 and #59 bullets
  are untouched. The keepachangelog link in the file header was fixed
  (`/1.1.0/` returned 404, `/en/1.1.0/` returns 200).
- **Checks:** markdown links pass (0 broken). `tests/test_version_consistency.py`
  passes (10/10). The repo has no docs build or linter. `git diff --check` is clean.
- **Dependabot #51 (vite 8.3.4 → 8.3.5):** **safe to merge** (not merged). Its CI passed 9 of 9 checks, and on this branch's base the build, typecheck, audit and e2e all pass. Details below.

## What changed
| File | Change |
|---|---|
| README.md | Config rows for `KTN_TLS_CERTFILE`/`KTN_TLS_KEYFILE` and `KTN_ALLOWED_HOSTS`. The webhook row now gives the delivery rules (2xx only, 3xx a failure, 60 s retry) and says the URL is never logged. New "Alerts never arrive" entry and an **HTTPS lockouts** table with 8 cases. |
| SECURITY.md | The blast-radius note is labelled **accepted risk R5** (a KTN compromise exposes the TrueNAS UI key). New section "Health alerts: the webhook URL is a credential". The renewal restart loop is called a **known limitation, deferred (L2)**: KTN neither detects nor breaks it, and nothing is served while it lasts. |
| docs/INSTALL-TRUENAS.md | The expiry fallback now warns that `KTN_ALLOWED_HOSTS` must list the IP. Adds a pointer to the lockouts table and the deferred-limitation wording on the restart loop. |
| ARCHITECTURE.md | Adds `tls_watch.py` to the module map. `notify.py` is now described as "2xx only" and "URL never logged". |
| CHANGELOG.md | As above. |

HSTS: the docs already said, correctly, that KTN sends no HSTS. The expiry
lockout comes from the TrueNAS UI's own HSTS policy, which covers every port
on that name. The new text keeps that framing.

## Claim → code hit list
| Claim in docs | Evidence (file:line) |
|---|---|
| `KTN_TLS_ENABLED` read by the entrypoint, exact case | docker-entrypoint.sh:72 |
| accepts only `1/true/yes/on`, `0/false/no/off`/empty | docker-entrypoint.sh:73, scripts/tls-bool.sh |
| `KTN_TLS_CERTFILE`/`KEYFILE` required when on | backend/ktnmgr/config.py:168 |
| absolute paths | config.py:170 |
| distinct files | config.py:172 |
| `/run/ktn` must be a tmpfs | docker-entrypoint.sh:89 |
| fail closed: `ERROR: TLS ...`, then the `KTN_TLS_ENABLED=false` hint, then `exit 1` | docker-entrypoint.sh:67-69 |
| invalid or mismatched pair refused | docker-entrypoint.sh:137 |
| case mismatch: refuses when `KTN_TLS_ACTIVE` is not set | config.py:59-61 (`{TLS_ACTIVE_MARKER} is not set`) |
| case mismatch: refuses when it is set but TLS is off | config.py:70 |
| `KTN_TLS_ACTIVE` marker | config.py:31 |
| `KTN_ALLOWED_HOSTS` defaults to empty (all hosts) | config.py:274 |
| loopback always answered | backend/ktnmgr/main.py:187 |
| `400` + `request Host header does not match KTN_ALLOWED_HOSTS` | main.py:209-210 |
| KTN sends no HSTS, TLS on or off | main.py:243 |
| expired cert: no bypass by name, use the IP | services/tls_watch.py:166-168 |
| log warns from 14 days out | tls_watch.py:45 |
| `/healthz` `tls.days_left` | tls_watch.py:149 |
| `renewed TLS certificate detected` | tls_watch.py:252 |
| webhook: only a 2xx counts as delivery | services/notify.py:260 |
| webhook: 3xx not followed, counted as a failure | notify.py:257-259 |
| log lines `health notification rejected: HTTP` / `could not deliver health notification` | notify.py:262, 210 |
| URL scrubbed from error text, endpoint logged as scheme and host | notify.py:234, 270 (`_where`), 283 (`_scrub`) |
| `httpx`/`httpcore` held at WARNING | main.py:63 |
| malformed URL logged by class only (main, unreleased #59) | notify.py:200-214 |
| retry spacing 60 s | notify.py:39; main.py:107 passes no override |
| an unaccepted alert stays uncommitted | notify.py:158 |
| healthcheck follows `KTN_TLS_ENABLED` | docker-healthcheck.py:8 |
| fix script recomputes portals | truenas/fix-custom-app-metadata.py:38 |
| `unless-stopped`, `/run/ktn` tmpfs in the YAML | truenas/install-via-yaml.yaml:72, 140 |

R5 and L2 are labels from the brief. They appear nowhere else in the repo, so
the docs now introduce them; no review file defines them.

## Checks run
- Markdown links: `python3 -I mdlinks.py .` (a scratch script; relative paths
  plus GitHub-slug anchors, code spans and fences skipped). Result: 12 files,
  39 relative links, 0 broken, exit 0. Its self-test on a planted bad anchor
  and a planted missing file reported both, exit 1.
- External links (curl, 10 unique). semver.org 200; keepachangelog fixed to
  200. The 8 github.com links got 403 from this sandbox's proxy, so they are
  unverified here, and none of them is new.
- `PYTHONPATH=backend pytest -q tests/test_version_consistency.py`: 10 passed.
  This is the only test that reads the docs, through the CHANGELOG/README asserts.
- Docs build/lint: none exists (no mkdocs, markdownlint or link-check job in
  `.github/workflows`).

## Dependabot #51
PR head `ba65029` changes only `frontend/package.json` and `package-lock.json`.
The installed versions moved as follows: vite 8.3.4 → 8.3.5, rolldown 1.2.13 → 1.2.15
and @oxc-project/types 0.153.0 → 0.155.0. postcss resolves to 8.5.30 in both.
The test was a scratch copy of this branch's `frontend/` (b8663c2) with the PR's
two files dropped in, against an unchanged copy. The repo checkout was not
touched, and the temporary fetch ref was deleted afterwards.

| Check | base (8.3.4) | PR #51 (8.3.5) |
|---|---|---|
| `npm ci` | exit 0 | exit 0 |
| `npm run typecheck` | exit 0 | exit 0 |
| `npm run build` | exit 0, no warnings | exit 0, no warnings |
| dist | — | CSS, html, icons byte-identical; JS 251,739 → 251,737 B (4 equivalent minifier rewrites), gzip ≈ 77.95 kB both |
| `npm audit` / `--omit=dev` | 0 vulnerabilities | 0 vulnerabilities |
| Playwright e2e (CI's job: `scripts/e2e-server.sh`, desktop + mobile) | 74 passed, 5 skipped, 1 flake* | 75 passed, 5 skipped, exit 0 |
| `--grep "identify expiry" --repeat-each 5` | 40/40 | 40/40 |

\*On mobile, "identify expiry › a later expiry from the server cancels the
pending local stop" failed once on **base** (it saw 2 polls in one interval).
It passed 40/40 on both copies on rerun, so it is a timing flake and was not
introduced by the PR. It may be worth a look as a separate item.

PR CI on `ba65029`: 9/9 check runs green (frontend, backend, e2e, image
amd64/arm64, TLS smoke, CodeQL, analyze python/js-ts). GitHub reports
`mergeable_state: clean`.

Caveats. The PR is 13 commits behind main, but none of those commits touch
`frontend/package*.json`. The merged result was tested as described above,
and a rebase or "Update branch" is optional. Local runs used Node 22.22 and
Python 3.13, while CI uses Node 26 and Python 3.14; CI on the PR covers those.
The e2e run used the preinstalled Chromium 1243 (the project pins 1248).

Logs:
`/tmp/claude-0/-home-user-ktn-enclosure-manager/4b0009fa-e75b-5c21-8a19-abb8251d0b3e/scratchpad/pr51/`
(not committed).
