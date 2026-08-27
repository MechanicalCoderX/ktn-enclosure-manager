"""The privileged helper's dispatcher, exercised at its wire boundary.

handle()/_dispatch() in helper/ktn_ident_helper.py are the last line of
defence: the helper explicitly does not trust the web process to have
validated anything (spec §31), so its refusals must hold against exactly the
bytes a compromised client could put on the socket - not merely in unit tests
of the validators it happens to call. These tests therefore start the REAL
IdentHandler on a real unix socket, wired to the captured KTN-STL3 sysfs
fixture (§42), and speak to it as a client: hostile ops, non-allowlisted
pages, malformed and oversized frames, ids and slots off the §43 hostile-input
list, and a client that connects and never speaks. Round-trip tests double as
the control for every refusal: the same server answering ok=true proves a
refusal came from the boundary, not from a server that was broken all along.
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
from ktnmgr.enclosure.helper_client import send
from ktnmgr.enclosure.locate import build_local_locate_writer
from ktnmgr.enclosure.ses import SesRunner
from ktnmgr.enclosure.sysfs import SysfsEnclosureBackend

# The helper is a standalone script, not a package: put its directory on
# sys.path and import it the same way its own __main__ runs it (the module
# then inserts backend/ itself, so the ktnmgr imports above stay valid even
# when the package is not installed).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "helper"))

import ktn_ident_helper

FIXTURES = Path(__file__).parent / "fixtures"
#: The captured shelf's logical id, as /sys/class/enclosure/<x>/id exposes it.
ENCLOSURE_ID = "0x50060480aabbcc00"
#: The captured shelf's sysfs directory inside the fixture tree.
ENCLOSURE_DIR = "class/enclosure/1:0:15:0"


class RunningHelper(NamedTuple):
    socket_path: Path
    sysfs: Path


@pytest.fixture
def running_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[RunningHelper]:
    """The real IdentHandler serving on a real unix socket.

    Wired exactly as main() wires it - class attributes, because socketserver
    instantiates a fresh handler per connection and passes it nothing - but
    against a per-test copy of the fixture tree so locate writes land in a
    world the test owns. Differences from main(), both deliberate:

    * ``dev_root`` is relocated into tmp_path (main() leaves it at /dev) so
      the resolved sg device can never name a real device node; the fake
      sg_ses ignores its device argument anyway.
    * ``ident_method`` is "sysfs", because the fake sg_ses replays read pages
      only and deliberately refuses ``--set`` - and a sysfs writer lets the
      test observe the write land in the fixture file, not just be echoed.
    """
    sysfs = tmp_path / "sys"
    shutil.copytree(FIXTURES / "sysfs_root", sysfs)
    lock_path = tmp_path / "enclosure.lock"

    backend = SysfsEnclosureBackend(
        sysfs_root=sysfs, dev_root=tmp_path / "dev", lock_path=lock_path
    )
    ses = SesRunner(binary=str(FIXTURES / "fake-sg_ses"), lock_path=lock_path)
    handler = ktn_ident_helper.IdentHandler
    # raising=False: these are declared as annotations only; main() is what
    # normally materialises them. monkeypatch restores/removes them afterwards
    # so no other test inherits this test's wiring through the shared class.
    monkeypatch.setattr(handler, "backend", backend, raising=False)
    monkeypatch.setattr(handler, "allowlist", set(), raising=False)
    monkeypatch.setattr(handler, "ses", ses, raising=False)
    monkeypatch.setattr(
        handler, "writer", build_local_locate_writer(backend, ses, "sysfs"), raising=False
    )

    socket_path = tmp_path / "ident.sock"
    server = ktn_ident_helper.IdentServer(str(socket_path), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield RunningHelper(socket_path=socket_path, sysfs=sysfs)
    finally:
        server.shutdown()
        server.server_close()


def _raw_exchange(socket_path: Path, frame: bytes) -> dict[str, object]:
    """One raw frame in, one JSON line out.

    Deliberately NOT helper_client.send(): that client json-encodes a dict, so
    it cannot express a malformed or non-UTF-8 frame at all. These tests probe
    the helper's parsing of arbitrary bytes, which requires a dumb pipe.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5.0)
        client.connect(str(socket_path))
        client.sendall(frame)
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode("utf-8"))


def _assert_still_serviceable(socket_path: Path) -> None:
    """The control after every rejection: the same server must still answer.

    A dispatcher that died processing hostile input would make the rejection
    assertion pass vacuously (connection refused also looks like "no ok").
    """
    response = send(socket_path, {"op": "ses_version"}, timeout=5.0)
    assert response["ok"] is True


# ---------------------------------------------------------------------------
# Operation allow-list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op",
    [
        "ses_write",
        "set_fault",
        "IDENTIFY_ON",  # the allow-list must be exact, not case-folded
        "identify_on ",  # nor whitespace-tolerant
        "",
        None,  # a frame with no "op" key at all
    ],
)
def test_an_op_outside_the_allowed_set_is_refused(
    running_helper: RunningHelper, op: str | None
) -> None:
    payload: dict[str, object] = {"enclosure_id": ENCLOSURE_ID, "slot": 0}
    if op is not None:
        payload["op"] = op
    response = send(running_helper.socket_path, payload, timeout=5.0)
    assert response["ok"] is False
    assert "unsupported operation" in str(response["error"])


# ---------------------------------------------------------------------------
# ses_read page allow-list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "page",
    [
        "control",
        "--set=ident",  # an argv fragment must not be accepted as a page name
        "cf",  # the literal sg_ses page code is not a semantic name either
        None,
    ],
)
def test_ses_read_refuses_a_non_allowlisted_page(
    running_helper: RunningHelper, page: str | None
) -> None:
    response = send(
        running_helper.socket_path,
        {"op": "ses_read", "enclosure_id": ENCLOSURE_ID, "page": page},
        timeout=5.0,
    )
    assert response["ok"] is False
    assert "allow-listed" in str(response["error"])


def test_ses_read_validates_the_enclosure_id_too(running_helper: RunningHelper) -> None:
    """A valid page must not carry a hostile id past the gate."""
    response = send(
        running_helper.socket_path,
        {"op": "ses_read", "enclosure_id": "/dev/sg0", "page": "configuration"},
        timeout=5.0,
    )
    assert response["ok"] is False
    assert "not a valid logical identifier" in str(response["error"])


def test_ses_read_refuses_an_enclosure_with_no_sg_device(
    running_helper: RunningHelper,
) -> None:
    """No sg node, no read: the device is resolved, never defaulted.

    resolve() re-scans sysfs per request (§37), so deleting scsi_generic from
    the fixture models an enclosure whose sg node vanished after attach - the
    dispatcher must refuse rather than fall back to any remembered path.
    """
    shutil.rmtree(running_helper.sysfs / ENCLOSURE_DIR / "device" / "scsi_generic")
    response = send(
        running_helper.socket_path,
        {"op": "ses_read", "enclosure_id": ENCLOSURE_ID, "page": "configuration"},
        timeout=5.0,
    )
    assert response["ok"] is False
    assert "no sg device" in str(response["error"])


def test_a_valid_ses_read_round_trips(running_helper: RunningHelper) -> None:
    """Control for the refusals above: resolve + allow-listed read succeeds."""
    response = send(
        running_helper.socket_path,
        {"op": "ses_read", "enclosure_id": ENCLOSURE_ID, "page": "configuration"},
        timeout=5.0,
    )
    assert response["ok"] is True
    assert "Configuration diagnostic page" in str(response["output"])


def test_ses_version_is_answered(running_helper: RunningHelper) -> None:
    response = send(running_helper.socket_path, {"op": "ses_version"}, timeout=5.0)
    assert response["ok"] is True
    assert "fixture replay" in str(response["version"])


# ---------------------------------------------------------------------------
# Request validation (§43 hostile-input list) and helper-side allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enclosure_id", "slot", "expected"),
    [
        ("../../etc/passwd", 0, "not a valid logical identifier"),
        ("/dev/sg0", 0, "not a valid logical identifier"),
        (f"{ENCLOSURE_ID}; reboot", 0, "not a valid logical identifier"),
        (7, 0, "must be a string"),
        (None, 0, "must be a string"),
        (ENCLOSURE_ID, "7", "must be an integer"),
        (ENCLOSURE_ID, True, "must be an integer"),  # bool is an int; still refused
        (ENCLOSURE_ID, None, "must be an integer"),
        (ENCLOSURE_ID, -1, "out of range"),
        (ENCLOSURE_ID, 1024, "out of range"),
    ],
)
def test_identify_refuses_ids_and_slots_failing_validation(
    running_helper: RunningHelper,
    enclosure_id: object,
    slot: object,
    expected: str,
) -> None:
    response = send(
        running_helper.socket_path,
        {"op": "identify_on", "enclosure_id": enclosure_id, "slot": slot},
        timeout=5.0,
    )
    assert response["ok"] is False
    assert expected in str(response["error"])


def test_a_valid_but_unattached_enclosure_is_refused(
    running_helper: RunningHelper,
) -> None:
    """Passing validation is not enough: the id must resolve to real sysfs."""
    response = send(
        running_helper.socket_path,
        {"op": "identify_read", "enclosure_id": "0x0123456789abcdef", "slot": 0},
        timeout=5.0,
    )
    assert response["ok"] is False
    assert "enclosure not attached" in str(response["error"])


def test_a_slot_absent_from_the_enclosure_is_refused(
    running_helper: RunningHelper,
) -> None:
    """In range for the validator (0..1023) but not a bay this shelf has."""
    response = send(
        running_helper.socket_path,
        {"op": "identify_read", "enclosure_id": ENCLOSURE_ID, "slot": 500},
        timeout=5.0,
    )
    assert response["ok"] is False
    assert "slot not present" in str(response["error"])


def test_the_helper_enforces_its_own_allowlist(
    running_helper: RunningHelper, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--allow is a helper-side boundary; the client never sees it.

    Both the IDENT path and the SES read path must cross the same gate: the
    read path also resolves a device from the id, so an id outside the
    allowlist is exactly as refusable there.
    """
    monkeypatch.setattr(
        ktn_ident_helper.IdentHandler, "allowlist", {"0x000000000000dead"}
    )
    for payload in (
        {"op": "identify_on", "enclosure_id": ENCLOSURE_ID, "slot": 0},
        {"op": "ses_read", "enclosure_id": ENCLOSURE_ID, "page": "configuration"},
    ):
        response = send(running_helper.socket_path, payload, timeout=5.0)
        assert response["ok"] is False
        assert "allowlist" in str(response["error"])


# ---------------------------------------------------------------------------
# Frame handling: malformed, oversized, and idle clients
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        pytest.param(b"this is not json\n", "malformed request", id="not-json"),
        pytest.param(b"\xff\xfe{\n", "malformed request", id="invalid-utf8"),
        pytest.param(b"[1, 2, 3]\n", "request must be an object", id="array"),
        pytest.param(b'"identify_on"\n', "request must be an object", id="bare-string"),
        # A bare newline parses as {} by design (empty request, empty answer
        # would leak nothing) and then fails the op check like any other.
        pytest.param(b"\n", "unsupported operation", id="empty-line"),
    ],
)
def test_a_malformed_frame_is_rejected_without_killing_the_server(
    running_helper: RunningHelper, frame: bytes, expected: str
) -> None:
    response = _raw_exchange(running_helper.socket_path, frame)
    assert response["ok"] is False
    assert expected in str(response["error"])
    _assert_still_serviceable(running_helper.socket_path)


def test_an_oversized_frame_is_rejected_without_killing_the_server(
    running_helper: RunningHelper,
) -> None:
    """readline(MAX_REQUEST_BYTES) truncates; the fragment must never parse.

    The request is a VALID operation padded past the cap: truncation lands
    mid-padding, so if the dispatcher ever saw the fragment as JSON it would
    execute a real op from an oversized frame. json.dumps preserves key order,
    which is what pins the truncation point inside the pad value.
    """
    request = {
        "op": "identify_read",
        "enclosure_id": ENCLOSURE_ID,
        "slot": 0,
        "pad": "x" * (2 * ktn_ident_helper.MAX_REQUEST_BYTES),
    }
    frame = (json.dumps(request) + "\n").encode("utf-8")
    assert len(frame) > ktn_ident_helper.MAX_REQUEST_BYTES  # premise of the test

    response = _raw_exchange(running_helper.socket_path, frame)
    assert response["ok"] is False
    assert "malformed request" in str(response["error"])
    _assert_still_serviceable(running_helper.socket_path)


def test_an_idle_client_is_hung_up_on_and_the_server_stays_serviceable(
    running_helper: RunningHelper, monkeypatch: pytest.MonkeyPatch
) -> None:
    """connect-and-stall must not pin a thread in the root process.

    The class-level timeout is applied per connection at setup(), so patching
    it after the server is already listening still governs new connections -
    shrunk here only to keep the test fast, the mechanism is the shipped one.
    """
    monkeypatch.setattr(ktn_ident_helper.IdentHandler, "timeout", 0.3)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as idle:
        # The client-side timeout is the failure detector: if the helper's
        # idle handling regressed, recv would still be blocking at 5s and
        # raise, rather than returning the EOF of a server-initiated close.
        idle.settimeout(5.0)
        idle.connect(str(running_helper.socket_path))
        assert idle.recv(4096) == b"", "helper wrote a response to an empty request"

    _assert_still_serviceable(running_helper.socket_path)


# ---------------------------------------------------------------------------
# The happy path against the fixture backend
# ---------------------------------------------------------------------------


def test_identify_read_round_trips_against_the_fixture(
    running_helper: RunningHelper,
) -> None:
    """The reported state must come from sysfs, not from anything cached."""
    payload = {"op": "identify_read", "enclosure_id": ENCLOSURE_ID, "slot": 4}

    response = send(running_helper.socket_path, payload, timeout=5.0)
    assert response == {"ok": True, "locate": False}

    # Flip the attribute behind the helper's back - as the kernel would when
    # the enclosure processor reports the LED lit - and read again.
    (running_helper.sysfs / ENCLOSURE_DIR / "4" / "locate").write_text("1")
    response = send(running_helper.socket_path, payload, timeout=5.0)
    assert response == {"ok": True, "locate": True}


def test_identify_on_and_off_write_the_fixture_and_verify(
    running_helper: RunningHelper,
) -> None:
    """The write must land in sysfs and the answer must be the read-back."""
    locate = running_helper.sysfs / ENCLOSURE_DIR / "3" / "locate"
    assert locate.read_text().strip() == "0"  # premise: the bay starts dark

    response = send(
        running_helper.socket_path,
        {"op": "identify_on", "enclosure_id": ENCLOSURE_ID, "slot": 3},
        timeout=5.0,
    )
    assert response == {"ok": True, "locate": True}
    assert locate.read_text() == "1"

    response = send(
        running_helper.socket_path,
        {"op": "identify_off", "enclosure_id": ENCLOSURE_ID, "slot": 3},
        timeout=5.0,
    )
    assert response == {"ok": True, "locate": False}
    assert locate.read_text() == "0"
