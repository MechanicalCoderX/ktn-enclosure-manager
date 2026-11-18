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
.venv/bin/ruff check backend helper tests && .venv/bin/mypy --config-file backend/pyproject.toml backend/ktnmgr helper
cd frontend && npm run typecheck
```

The suite runs entirely against captured fixtures in `tests/fixtures/`.
Regenerate the synthetic sysfs tree with `python3 tests/build_sysfs_fixture.py`.

## Dependency lock

`backend/pyproject.toml` declares the runtime dependencies as ranges; dev
installs (`pip install -e 'backend[dev]'`) resolve from those. The image
installs `backend/requirements.txt` instead: every version pinned, every file
hash-checked (`pip install --require-hashes`), so two builds of one commit ship
the same libraries. `backend/requirements.in` is a copy of pyproject's list,
because Dependabot recompiles a lock only from a `.in` file.

After changing `[project].dependencies`, copy the change into
`requirements.in` and recompile, from `backend/`:

```bash
uv pip compile --universal --python-version 3.15 --generate-hashes \
    requirements.in -o requirements.txt
```

- `--python-version` is the image's Python (`FROM python:3.15-slim` in the
  Dockerfile); uv targets it without that interpreter installed.
- `--universal` and `--generate-hashes` keep one lock valid for both published
  architectures: the hashes cover every file of each pinned version, arm64
  wheels included.
- Re-running keeps existing pins. Add `--upgrade-package <name>` to move one,
  or `--upgrade` to move everything.

Declare runtime packages by name, never through an extra: the image's
`pip check` ignores extras, so a package an extra brings could drop out of the
lock unnoticed. That is why pyproject names `uvloop` and `httptools` instead
of `uvicorn[standard]`; the extra's other packages are not used (`watchfiles`
is for `--reload`, PyYAML for a YAML `--log-config`).

The image installs the lock with `--only-binary :all:`, so no pinned
dependency is built from source there: building one runs a build backend that
no hash covers. Every pin therefore needs a wheel for the image's Python on
amd64 and arm64. If a new pin has only a source distribution for that Python,
the image build fails; pick a version that has wheels, or do without the
dependency, rather than removing the flag. The project itself is still built
in the image, by setuptools, which pip fetches unhashed.

`tests/test_image_build_inputs.py` fails if the two lists differ, if a pin
falls outside pyproject's range, if the lock's Python differs from the
image's, if a runtime dependency uses an extra, or if the lock install allows
source builds. `scripts/check-lock.sh` (run in CI, or locally with `uv` on PATH)
recompiles the lock with its own pins held and fails if the body differs, so
a hand-edited pin or hash, or a lock compiled with other options, is caught.

CI installs the lock first and then the project constrained to it, so the
tests, E2E and `pip-audit` run on the versions the image ships. A local
`pip install -e 'backend[dev]'` still resolves pyproject's ranges to the
newest releases; to match CI, install `backend/requirements.txt` with
`--require-hashes` first.

Dependabot (`uv` ecosystem, `/backend`) recompiles with the same options, read
back from the lock header. It chooses the version of each direct dependency
under the lowest Python that `requires-python` allows, so that floor is kept
equal to the image's Python (3.15); a test fails if the two drift apart. A
local install therefore needs Python 3.15 or later.

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
