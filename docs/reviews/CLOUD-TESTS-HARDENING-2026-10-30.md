# Cloud tests-hardening pass, 2026-10-30

Tests only. No product code changed: the diff against main touches only
`tests/`, `frontend/e2e/app.spec.ts` and this file.

The work is on branch `claude/ktn-tests-hardening-x9r8ux` (the session's
designated branch), mirrored to `cloud/tests-hardening`. Both start from main
`8be318d`. No PR was opened and nothing was deployed.

## Status log

- **17:26 UTC, checkpoint 1.** All four areas written and passing locally.
  Pushed as `1d0ee51`.
- **Final, this file.** Mutation controls, coverage, CI and the independent
  review are below. Nothing in the brief is left open.

## Result

| | before (main `8be318d`) | after |
|---|---|---|
| pytest (local, Python 3.14) | 606 passed | 699 passed, 1 skipped |
| Playwright, CI | 62 tests | 80 tests: 75 passed, 5 skipped |
| CI run on the branch | | [41099030501](https://github.com/MechanicalCoderX/ktn-enclosure-manager/actions/runs/41099030501): all 6 jobs green |

The CI jobs are backend, frontend, e2e, TLS smoke, and the amd64 and arm64
image builds. CI ran via `workflow_dispatch`, because `ci.yml` only runs on
pushes to main and on PRs.

The pytest skip is the bug in Finding 1, left skipped on purpose. The 5
Playwright skips are all deliberate project gates: 4 existed before, plus the
new desktop-only self-test.

## Coverage

pytest, line and branch, `--cov=backend/ktnmgr --cov-branch`:

| module | before | after | still missing after |
|---|---|---|---|
| `services/tls_watch.py` | 82% | 96% | `default_restart`, two `leaf_fingerprint` decode failures, the "expired" log branch |
| `config.py` | 97% | 99% | one unrelated branch (341->343) |
| `services/notify.py` | 92% | 92% | state load/save `OSError` paths and `_describe` optionals: outside this brief |
| **total** | **90.57%** (3024/3339) | **91.28%** (3048/3339) | |

`notify.py` does not move, and that is expected. The 3xx and scrubbing lines
were already executed. What was missing was assertions on them, and the
mutation table shows that gap closing (rows D1-D9).

The shell and the frontend have no coverage tool here, so their coverage is
counted by what is exercised.

**`docker-entrypoint.sh` TLS block and `tls-bool.sh`:**

- **Before:** of the 30 spellings in the shared table, 5 went through the real
  entrypoint ("true", "false", "on", unset, plus 4 refusals). The full table
  went through `ktn_bool` alone. The stat-failure fallback (`|| echo
  missing`), the check ordering, TLS-off with an off-tmpfs runtime dir, and
  three marker paths were not exercised.
- **After:** all 30 spellings go through the real script. All of the paths
  listed above are exercised, plus the entrypoint/app marker contract for
  every accepted spelling.

**Theme contrast:**

- **Before:** the healthy dashboard, its tabs and the change-password dialog,
  in 4 theme modes.
- **After:** also the login screen, its plain `.notice` and its
  `.notice.error`, the dashboard's two `.notice.warn` banners, `.notice.error`
  and `.notice.info`, and the degraded, too-hot and unauthenticated badges,
  each in the same 4 modes on both projects.

## What was added

**(a) `frontend/e2e/app.spec.ts`**

- **L1.** `lowContrast()` judges `-webkit-text-fill-color` as well as `color`.
  When the two differ, each is judged on its own and labelled "(color)" or
  "(text-fill)".
- **L2.** A placeholder's own computed opacity is composited onto its text
  colour, and every placeholder's opacity is returned. The `opacity: 1` rule
  in `styles.css` is invisible on Chromium, which already defaults to 1. So
  the theme tests now re-create Firefox's UA default (0.54) inside a CSS
  cascade layer. The app's unlayered rule wins only while it exists, and the
  drive-map test asserts the opacity is `["1"]`.
- **L3.** Two new tests per mode, in Light, Dark, and System with the OS set
  to light and to dark:
  - *Warning, error and info notices and the state badges.* The `/bays` and
    `/chassis` answers are rewritten in the browser to raise both source
    errors, TrueNAS's over-temperature flag and a fan-step divergence. Then
    `/bays` is failed to raise the error banner.
  - *The login screen, its notices and the unauthenticated badge.* An aborted
    sign-out produces the plain notice. A 401 answered in the browser produces
    the error notice; this keeps the server's login rate limit untouched.
    Rewriting `/api/auth/status` produces the unauthenticated badge.

  Each element is first shown to be present, then measured on its own with an
  exact count, so a missing notice cannot pass as "no low contrast".
- A desktop-only self-test of the checker. It injects a text-fill override and
  a dimmed placeholder, and expects both to be flagged.

**(b) Entrypoint.**

- `tests/test_entrypoint_tls.py` gains 8 tests:
  - the whole spelling table through the real script;
  - the entrypoint/app contract for every accepted spelling;
  - the tmpfs check fails closed when the parent is missing;
  - the tmpfs check runs before any read or retry;
  - with TLS off, an off-tmpfs runtime dir is ignored;
  - three marker cases: TLS off discards a caller's marker, an empty caller
    marker becomes 1, and an inherited marker cannot carry a refused start.
- `tests/test_tls_name_case.py` gains 9 direct `check_tls_marker` unit tests:
  the four on/off combinations, an empty marker, "0", and exact-case naming.

**(c) `tests/test_tls_watch.py`** gains 10 tests:

- a revert, unparseable content, or an unreadable source mid-settle each
  restart the settle;
- an unreadable served copy pauses the watcher;
- the "does not parse" and "paused" warnings are logged once each;
- a chain change with the same leaf is still a change;
- the loop survives an exception;
- `start()` and `stop()`, including replacing a finished task.

**(d) `tests/test_notify.py`** gains 10 tests:

- every 2xx is delivery, with 199, 300-399 rejected and *retried*;
- a redirect is not followed (real httpx client);
- a 3xx then 200 is delivered once with the original transition;
- the rejection log names scheme and host only;
- userinfo credentials stay out of both log paths;
- the state file never holds the URL;
- the httpx logger cap is what stops httpx's own line from leaking the URL;
- the skipped bug probe (Finding 1).

Subagents (Sonnet) drafted (b)-(d). I reviewed every diff and cut three of
their tests that no realistic single mutation could fail: a trailing-slash
scrub, a json-style scrub, and a no-PEM-in-marker-message check. I also
rewrote one control so that it guards `configure_logging` and doesn't just
show an httpx behaviour.

## Mutation controls

Each mutation was applied in a throwaway git worktree (never this tree). Then
the new tests were run, and the same test file from main was run as the
"old" column. "Killed" means at least one targeted test failed. Where a
selector covers several tests, it was re-run without `-x` to confirm *each*
one fails.

| id | mutation | target new test(s) | new | old file |
|---|---|---|---|---|
| E1 | `tls-bool.sh` matches `true` case-sensitively | spelling table via entrypoint | killed | survived¹ |
| E1b | entrypoint branches on the raw value, not `ktn_bool` | spelling table via entrypoint | killed | killed |
| E2 | app expects marker value `true`; entrypoint exports `1` | entrypoint/app agree | killed | survived¹ |
| E3 | `stat` failure → `echo tmpfs` (fails open) | missing runtime parent | killed | survived |
| E4 | key readability checked before tmpfs | tmpfs check comes first | killed | survived |
| E5 | tmpfs check hoisted out of the TLS-on branch | TLS off ignores off-tmpfs dir | killed | survived |
| E6 | `unset KTN_TLS_ACTIVE` removed | TLS off discards caller marker | killed | killed |
| E7 | caller's marker trusted (unset removed + `${KTN_TLS_ACTIVE-1}`)² | empty caller marker → 1 | killed | killed |
| E8 | refusal skipped when a marker was inherited | inherited marker can't carry a refused start | killed | killed |
| N1 | `if settings.tls_enabled:` (drop `and not active`) | both on accepted | killed | killed |
| N2 | `if not settings.tls_enabled:` | both off; empty-off; lowercase-off (3) | killed ×3 | killed |
| N3 | on-without-marker refusal removed | on refuses; lowercase-on (2) | killed ×2 | killed |
| N4 | marker-without-setting refusal removed | marker refuses; "0" present (2) | killed ×2 | killed |
| N5 | `active = MARKER in env` | empty marker on/off (2) | killed ×2 | survived |
| N7 | `"0"` treated as no marker | any non-empty value is present | killed | survived |
| N8 | marker name matched case-insensitively | exact name on/off (2) | killed ×2 | survived |
| W1 | equal-hash branch keeps `_pending` | revert during settle | killed | survived |
| W2 | unparseable branch keeps `_pending` | unparseable mid-settle | killed | survived |
| W3 | `_unreadable` keeps `_pending` | unreadable source mid-settle | killed | survived |
| W4 | missing served copy compared as `b""` | served copy unreadable | killed | survived |
| W5 | parse warning not de-duplicated | warned once per content | killed | survived |
| W6 | paused warning not de-duplicated | paused once, resumed noted | killed | survived |
| W7 | compare leaf fingerprints, not file content | chain change, same leaf | killed | survived |
| W8 | no exception guard in `_loop` | loop survives | killed | survived |
| W9 | `start()` always creates a task | start idempotent / stop | killed | survived |
| W10 | `start()` never replaces a finished task | replaces finished task | killed | survived |
| D1 | only 200 is delivery | every 2xx delivered | killed | survived |
| D2 | `< 400` (3xx is delivery) | outside 2xx (8 × 3xx); 302→200 once | killed ×9³ | killed |
| D2b | `100 <=` (1xx is delivery) | outside 2xx [199] | killed | survived |
| D3 | `AsyncClient(..., follow_redirects=True)` | redirect not followed | killed | survived |
| D5 | `_where` returns the whole URL | rejection names scheme+host only | killed | killed |
| D6 | `_where` uses `netloc` (keeps userinfo) | userinfo, rejected + raising (2) | killed ×2 | survived |
| D8 | `_save` writes the URL into the state file | state never holds the URL | killed | survived |
| D9 | `configure_logging` stops capping httpx/httpcore | logging cap guards the URL | killed | killed |
| F1 | `styles.css` loses `::placeholder { opacity: 1 }` (L2) | bay numbers and every tab, 4 modes | killed ×4 | survived |
| F2 | `select, input[type=search] { -webkit-text-fill-color: var(--panel-2) }` (L1) | bay numbers and every tab, 4 modes | killed ×4 | survived |
| F3a | light `--warn` paled to `#d4952b` | L3 notices+badges and login, light modes | killed ×4 | killed (2)⁴ |
| F3b | `.notice.error` coloured `--border` | L3 notices and login, dark modes | killed ×4 | survived |
| F3c | `.notice.info` coloured `--border` | L3 notices, System OS dark | killed | survived |
| F3d | dark (OS-preference block) `--warn` → `#6b5222` | L3 notices and login, System OS dark | killed ×2 | killed (1)⁴ |
| F4 | plain `.notice` coloured `--border` | login screen, System OS light | killed | survived |
| F5 | `.badge.warning` coloured `--border` | L3 notices and login, Light + Dark | killed ×4 | killed⁴ |
| F6 | checker reads `color` only (the old L1 blind spot) | checker self-test | killed | n/a |
| F7 | checker ignores placeholder opacity (the old L2 blind spot) | checker self-test | killed | n/a |
| D10bug | bug probe un-skipped, code as shipped | malformed URL is a failed delivery | **fails** (the bug) | n/a |
| D10fix | candidate fix: also catch `httpx.InvalidURL` (copy only, not applied) | same probe | passes | n/a |

Notes:

1. E1 and E2 survive their own old file, but other old files already killed
   them: E1 by `test_tls_config.py` parity (2 failed) and E2 by
   `test_tls_name_case.py` / `test_tls_serving.py`. The new tests add
   entrypoint-level and contract-level detection, not first detection.
2. E7's first version (pass-through only) **survived**. It was an equivalent
   mutation: the script's earlier `unset` already clears the inherited value.
   The regression this test actually guards is the entrypoint trusting a
   caller's marker, which needs both changes. That mutation is killed.
3. Under D2, the `[199]` case correctly still passes (199 is not 3xx). D2b
   kills it.
4. The old theme test already saw `--warn` and `.badge.warning` somewhere on
   the healthy page, so these colours were partly covered before. The specific
   notices and badges in L3 were never rendered until now.

Playwright mutations were run on the desktop project. The e2e grep is
case-insensitive, so "Dark:" also matches "System, OS dark:".

## Findings

### Finding 1 (low): a malformed webhook URL escapes `_send()`

`httpx.InvalidURL` is not a subclass of `httpx.HTTPError`, so `_send()` in
`backend/ktnmgr/services/notify.py` does not catch it. Its `except (httpx.HTTPError,
OSError)` misses it. A URL such as `https://ntfy.example:notaport/topic`
raises while the request is being built: `InvalidURL: Invalid port:
'notaport'`. That exception escapes `asyncio.gather` and then `evaluate()`.

It is not a crash. `StateService._notify_health_changes` wraps the call and
logs `health notification failed` with a full traceback. But:

- the traceback repeats on every poll that changes anything;
- the retry cooldown is never set, because the failure never reaches the
  `_retry_at` line;
- any enclosures after the failing one are skipped for that pass;
- the `_save()` at the end of `evaluate()` is skipped, so silent transitions
  from that pass are not persisted.

The URL path was not seen in the message; httpx quotes only the bad
component.

The probe `test_a_malformed_url_counts_as_a_failed_delivery` stays skipped,
with the reason. Un-skipped, it fails on the code as shipped (D10bug). It
passes when `httpx.InvalidURL` is added to the except tuple (D10fix, done in
the throwaway copy only). That fix is not applied, per the brief. A
config-time check of `KTN_ALERT_WEBHOOK_URL` would be the other place to
catch it.

### Observations (not bugs)

- **`KTN_TLS_ACTIVE=0` counts as present.** `check_tls_marker` tests for
  presence, not value. The entrypoint only ever exports `1` and unsets the
  caller's value, so this is harmless. The current behaviour is now pinned
  and documented (N7).
- **Local identify-expiry flakes.** On a loaded machine, two pre-existing
  `identify expiry` tests failed once locally, on the main spec too, during
  the baseline run: "expected exactly one bay poll in one poll interval". They
  pass in CI and in quiet local runs. They are sensitive to host load.
- **Playwright browser mismatch, this container only.** Locally,
  `@playwright/test` 1.64.1 wants Chromium build 1248, while the container
  ships 1243. Local runs used an uncommitted config wrapper with
  `executablePath`. CI installs its own browser and is unaffected.

## Independent review

A fresh subagent reviewed the branch diff (`8be318d..1d0ee51`) against the
brief. It was given only the code, not my conclusions or the mutation table.
It ran the affected pytest files (194 passed, 1 skipped), `ruff` and `tsc`.
It did not run Playwright, because my mutation harness was using the e2e
port.

**Verdict: APPROVE WITH NITS.**

- It found no product change.
- It confirmed Finding 1 against httpx 0.28.1 and rated it low. It also
  corrected the probe's docstring: the poll loop does not crash; the
  traceback, the skipped cooldown and the skipped enclosures are the impact.
- It judged the cascade-layer emulation sound: it "correctly makes
  `styles.css:216` `opacity: 1` load-bearing on Chromium".
- It found no rate-limit or shared-state hazard in the new e2e tests.

What I did with its points:

| # | point | disposition |
|---|---|---|
| 1 | this report was a half-finished checkpoint | completed (this file) |
| 2 | probe docstring overstated the bug ("crash the poll loop") | **fixed**: docstring now states the real impact |
| 3 | `test_an_inherited_marker_cannot_carry_a_refused_start` does not fail when only `unset` is deleted | agreed: it is weak. It is killed only by E8, a refusal that trusts an inherited marker. The `unset` is guarded by `test_tls_off_discards_a_caller_supplied_marker` (E6). Kept, and noted here. |
| 4 | the empty-marker test also survives `unset` deletion alone | agreed. It is killed only by the combined trust mutation (E7, note 2). |
| 5 | `raising_client` echoes the configured URL, so it cannot catch httpx normalising it | agreed. Real httpx transport errors here do not quote the URL at all (checked). The test pins the exact-match scrub, and the rejected-path test pins `hostname` over `netloc` (D6). |
| 6 | state-file test is near-tautological | kept: it is cheap, and D8 shows it fails if the URL is ever persisted |
| 7 | comment "not the 30 s left" is inaccurate | **fixed** |
| 8 | the loop test monkeypatches `asyncio.sleep` process-wide | kept: it is scoped by `monkeypatch` and restored after the test. Injecting a sleep would mean changing product code. |
| 9 | the text-fill comment named only Chromium and Safari | **fixed**: now says "browsers" |
| 11 | `.notice.info` relies on the chassis cache being warm (30 s poll, 15 s wait) | same exposure as the existing chassis test, and it was green in CI on both projects. Noted. |
| 12 | `SPELLINGS` is imported from another test module (breaks under `--import-mode=importlib`) | kept: CI uses the default prepend mode, and this keeps one table rather than two copies |
| 13 | "Review 10-29 L1/L2/L3" is not in the repo | it is the review this brief cites. Kept as a pointer. |
| 14 | gaps: first-run login screen not measured; `_unusable_hash` never reset (bad A, then good, then bad A again gives no second warning); json-style 3xx | not covered. The bootstrap screen only exists once per server. The `_unusable_hash` behaviour is a log-only quirk, recorded here as an observation, not a bug. json-style 3xx takes the same branch. |
| 10, 15 | L1/L2 approach OK; style OK, banner dashes uneven | no action |

## Not done

Nothing from the brief is outstanding. Things deliberately not done:

- No product fixes. Finding 1 is left as a skipped test.
- No PR and no deploy.
- The reviewer's gap items in row 14 are not covered.
