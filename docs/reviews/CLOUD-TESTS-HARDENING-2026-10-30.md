# Cloud tests-hardening pass, 2026-10-30

Tests only; no product code changed. Full tables are in
[the details file](CLOUD-TESTS-HARDENING-2026-10-30-details.md).

## 17:26 UTC, checkpoint 1
- Areas (a)-(d) written; local pytest 699 passed, 1 skipped (baseline 606).
  Pushed as `1d0ee51`.
- Not yet done: mutation controls, coverage, CI, independent review.

## Final
- **Tests:** pytest 606 → 699 passed, 1 skipped. Playwright 62 → 80
  (75 passed, 5 skipped project gates).
- **CI:** [41099030501](https://github.com/MechanicalCoderX/ktn-enclosure-manager/actions/runs/41099030501)
  on `1d0ee51`, all 6 jobs green. Final test commit `acab8ac`:
  [41100951653](https://github.com/MechanicalCoderX/ktn-enclosure-manager/actions/runs/41100951653) green.
- **Coverage (line+branch):** 90.57% → 91.28%. `tls_watch` 82→96%,
  `config` 97→99%, `notify` 92→92% (assertions added, no new lines run).
- **Mutations:** 44, plus 2 bug-probe checks. Each new test is killed by one. One
  equivalent mutation (E7) was redone as a real one and is now killed.
- **Finding 1 (low):** `httpx.InvalidURL` escapes `notify._send()`. The probe
  test is skipped; a fix was checked in a throwaway copy only, not applied.
- **Independent review:** APPROVE WITH NITS. Comment nits fixed; the rest are
  dispositioned in the details file.
- **Not done:** no product fixes, no PR, no deploy.
