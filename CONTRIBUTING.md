# Contributing

## Ground rules

This application runs against live storage. Two rules matter more than style:

1. **The only write is IDENT.** Any change that adds a second kind of write —
   fault LEDs, device power, PHY reset, firmware, SES control pages — is out of
   scope for this project. `tests/test_security.py` asserts the `sg_ses`
   allow-list contains no mutating option; keep it that way.
2. **Run it against hardware before believing it.** Several defects in this
   codebase passed every static check and were only caught by execution — the
   `locate` read-back timing, the SG_IO cgroup permission, the AppArmor denial,
   and the `--join` bracket semantics. Fixtures are necessary, not sufficient.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e 'backend[dev]'
cd frontend && npm install
```

## Tests

```bash
PYTHONPATH=backend .venv/bin/python -m pytest tests/ -q   # backend, no hardware
cd frontend && npx playwright test                        # E2E, real backend + fixtures
.venv/bin/ruff check backend && .venv/bin/mypy backend
cd frontend && npm run typecheck
```

The suite runs entirely against captured fixtures in `tests/fixtures/`.
Regenerate the synthetic sysfs tree with `python3 tests/build_sysfs_fixture.py`.

## Adding support for another enclosure

Discovery, mapping and IDENT are generic; they should need no changes. The
chassis parser reads element labels from the device's own configuration page
rather than assuming a layout. If your shelf needs special handling, capture
fixtures first:

```bash
sg_ses -p cf /dev/sgN  > tests/fixtures/<model>/sg_cf.txt
sg_ses --join /dev/sgN > tests/fixtures/<model>/sg_join_unfiltered.txt
```

Then add a test that asserts against them, and only then change the parser.

## Style

- Comments explain *why*, especially where behaviour is counter-intuitive or was
  established by measurement. Do not remove those comments; they are the record
  of what the hardware actually does.
- Keep the semantic boundary intact: no endpoint may accept a path, a command,
  an argv element, or an `sg_ses` argument.
- Never log, print, or serialise a credential.
