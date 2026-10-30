# Cloud tests-hardening pass, 2026-10-30

Tests only. No product code changed. Branch `claude/ktn-tests-hardening-x9r8ux`,
also pushed as `cloud/tests-hardening`, both from main `8be318d`.

## Status at 17:26 UTC (first checkpoint)

Done:
- (a) `frontend/e2e/app.spec.ts`: L1 (text-fill), L2 (placeholder opacity,
  composited and guarded) and L3 (login screen, `.notice.warn/.error/.info`,
  the degraded / too-hot / unauthenticated badges, in Light, Dark and System
  under both OS preferences). Local run: theme contrast 25 passed, 1 skipped
  (a desktop-only self-test, skipped on mobile by design).
- (b) `tests/test_entrypoint_tls.py`, `tests/test_tls_name_case.py`: entrypoint
  spelling table, tmpfs check, KTN_TLS_ACTIVE marker.
- (c) `tests/test_tls_watch.py`: renewal hash and settle bookkeeping.
- (d) `tests/test_notify.py`: 2xx/3xx boundary, redirects, URL scrubbing.
- Local pytest: 699 passed, 1 skipped (baseline 606 passed). The skip is a
  real bug (see Findings, to be written up).

Not done yet: mutation controls, coverage after, CI run on the branch,
independent review, final report.
