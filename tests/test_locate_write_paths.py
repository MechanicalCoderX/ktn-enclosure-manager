"""The IDENT write paths the shipped deployment actually uses (spec §26, §31).

Every HTTP-level ident test pins ``ident_method=sysfs`` and the IdentManager
unit tests inject an in-memory FakeWriter, so the components a catalog
deployment routes every IDENT through - ``HelperLocateWriter`` in the web
process, the helper socket protocol, ``SesLocateWriter.write`` with its
lock-held settle loop, and ``HelperSesRunner`` for telemetry - had no coverage
at all. These tests drive them for real: the actual ``IdentHandler`` from
helper/ktn_ident_helper.py served over a real unix socket, the actual
``SysfsEnclosureBackend`` over the captured KTN-STL3 sysfs tree, and the
fixture ``fake-sg_ses`` binary where a subprocess is involved.

The lock assertions probe the flock file itself rather than trusting any
recorded call: flock conflicts across open file *descriptions*, so a fresh
descriptor observes a lock held by the same thread - the property access.py's
re-entrancy guard exists to work around, used here in reverse as an oracle.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import socketserver
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from ktnmgr.enclosure import locate as locate_module
from ktnmgr.enclosure.helper_client import MAX_RESPONSE_BYTES
from ktnmgr.enclosure.locate import DirectLocateWriter, HelperLocateWriter, LocateError
from ktnmgr.enclosure.ses import HelperSesRunner, SesError, SesResult, SesRunner
from ktnmgr.enclosure.sysfs import SysfsEnclosureBackend

FIXTURES = Path(__file__).parent / "fixtures"
SYSFS_FIXTURE = FIXTURES / "sysfs_root"
CAPTURES = FIXTURES / "ktn-stl3"
FAKE_SG_SES = FIXTURES / "fake-sg_ses"

#: The enclosure the captured sysfs tree exposes (fixtures/sysfs_root/.../id).
STL3_ID = "0x50060480aabbcc00"
ENCLOSURE_DIR = Path("class") / "enclosure" / "1:0:15:0"


def _ident_helper_module() -> object:
    """Import the real privileged helper, same idiom as test_ident_audit.py.

    The helper is not a package member - it is shipped as a standalone script -
    so it is reached by path, not by installing it."""
    helper_dir = str(Path(__file__).resolve().parent.parent / "helper")
    if helper_dir not in sys.path:
        sys.path.insert(0, helper_dir)
    import ktn_ident_helper  # type: ignore[import-not-found]

    return ktn_ident_helper


@contextmanager
def _unix_server(socket_path: Path, handler: type) -> Iterator[None]:
    server = socketserver.ThreadingUnixStreamServer(str(socket_path), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def live_helper(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """The REAL IdentHandler served over a real unix socket.

    This is the wiring docker-entrypoint.sh builds, minus root: the handler's
    backend reads the captured sysfs tree (copied so writes stick), its
    SesRunner executes the fixture fake-sg_ses binary, and its writer is the
    sysfs one - the only writer that can *succeed* against a text-replay stub,
    which lets the round-trip tests observe a write actually landing. The
    handler subclass exists so the class attributes never leak onto the shared
    IdentHandler other test modules import.
    """
    helper_mod = _ident_helper_module()
    root = tmp_path / "sys"
    shutil.copytree(SYSFS_FIXTURE, root)
    lock = tmp_path / "enclosure.lock"
    backend = SysfsEnclosureBackend(sysfs_root=root, dev_root=tmp_path / "dev", lock_path=lock)

    class _LiveIdentHandler(helper_mod.IdentHandler):  # type: ignore[attr-defined,misc,name-defined]
        pass

    _LiveIdentHandler.backend = backend
    _LiveIdentHandler.allowlist = set()
    _LiveIdentHandler.ses = SesRunner(binary=str(FAKE_SG_SES), lock_path=lock)
    _LiveIdentHandler.writer = DirectLocateWriter(backend)

    socket_path = tmp_path / "ident.sock"
    with _unix_server(socket_path, _LiveIdentHandler):
        yield SimpleNamespace(socket=socket_path, sysfs=root)


# --------------------------------------------------- HelperLocateWriter (§31)


def test_helper_round_trip_flips_the_fixture_attribute(live_helper: SimpleNamespace) -> None:
    """A full round trip through the socket: the LocateWriter contract says
    write() returns the *observed* state (IdentManager compares it against the
    requested one), so on->True and off->False are both success here - and the
    sysfs attribute on the other side of the privilege boundary must actually
    have changed, not just been reported as changed."""
    writer = HelperLocateWriter(live_helper.socket)
    locate = live_helper.sysfs / ENCLOSURE_DIR / "4" / "locate"

    assert writer.read(STL3_ID, 4) is False
    assert writer.write(STL3_ID, 4, True) is True
    assert locate.read_text().strip() == "1"
    assert writer.read(STL3_ID, 4) is True
    assert writer.write(STL3_ID, 4, False) is False
    assert locate.read_text().strip() == "0"


def test_helper_refusal_surfaces_as_locate_error_with_its_reason(
    live_helper: SimpleNamespace,
) -> None:
    """An ok=false response must become a LocateError carrying the helper's
    own error text - that string is what ends up in the audit trail and the
    UI, so 'IDENT helper refused the request' boilerplate is not enough."""
    writer = HelperLocateWriter(live_helper.socket)

    # Valid id shape (so it passes client-side validation) but not attached.
    with pytest.raises(LocateError, match="enclosure not attached"):
        writer.write("0xdeadbeef", 4, True)

    # Attached enclosure, nonexistent bay: refused by the helper's own
    # SlotNotFoundError mapping, not by anything client-side.
    with pytest.raises(LocateError, match="slot not present"):
        writer.write(STL3_ID, 42, True)


def test_unreachable_helper_socket_becomes_locate_error(tmp_path: Path) -> None:
    """A configured-but-absent socket is what a crashed helper leaves behind;
    the writer must fail with the writers' common exception type, not leak
    HelperUnavailableError to callers that only catch LocateError."""
    writer = HelperLocateWriter(tmp_path / "never-created.sock")
    with pytest.raises(LocateError, match="helper unreachable"):
        writer.write(STL3_ID, 4, True)


class _OversizedResponseHandler(socketserver.StreamRequestHandler):
    """Answers any request with more than MAX_RESPONSE_BYTES before a newline.

    The cap exists because the client reads until newline from the other side
    of a privilege boundary: without it a confused or hostile helper could
    balloon the web process's memory one 64KiB chunk at a time."""

    def handle(self) -> None:
        self.rfile.readline(4096)
        try:
            self.wfile.write(b"x" * (MAX_RESPONSE_BYTES + 2) + b"\n")
        except OSError:
            pass  # the client hung up at the cap - the behaviour under test


def test_oversized_helper_response_is_refused_not_buffered(tmp_path: Path) -> None:
    socket_path = tmp_path / "ident.sock"
    with _unix_server(socket_path, _OversizedResponseHandler):
        writer = HelperLocateWriter(socket_path)
        with pytest.raises(LocateError, match="helper response too large"):
            writer.write(STL3_ID, 4, True)


# ------------------------------------------- SesLocateWriter.write (§26, §31)


def _flock_is_held(lock_path: Path) -> bool:
    """Whether ANY descriptor currently holds the enclosure flock.

    Probing on a fresh descriptor works from inside the very thread that holds
    the lock, because flock conflicts across open file descriptions even
    within one process. Closing the fd releases the probe's own lock in the
    not-held case."""
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def test_lock_probe_control(tmp_path: Path) -> None:
    """Control for the probe itself: an uncontended lock file must read as not
    held, or every lock assertion below would pass vacuously."""
    assert _flock_is_held(tmp_path / "enclosure.lock") is False


class _SettleProbingBackend(SysfsEnclosureBackend):
    """Counts settle polls and records the lock state at each.

    ``stale_reads`` simulates the real shelf: the kernel's cached locate
    attribute refreshes only once the enclosure processor answers (measured
    0.17-0.22s on the KTN-STL3), so the first reads after a command still
    return the previous value."""

    def __init__(self, *args: object, stale_reads: int = 0, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.stale_reads = stale_reads
        self.settle_polls = 0
        self.lock_held_at_poll: list[bool] = []

    def read_locate_at(self, path: Path) -> bool:
        self.settle_polls += 1
        self.lock_held_at_poll.append(_flock_is_held(self.lock_path))
        if self.settle_polls <= self.stale_reads:
            return not super().read_locate_at(path)  # stale: the previous value
        return super().read_locate_at(path)


class _CaptureSes:
    """Replays the KTN-STL3 captures; on IDENT, plays the enclosure processor.

    ``honour_ident`` False models a shelf that accepts the SEND DIAGNOSTIC but
    never applies it - the case the settle loop's timeout contract exists for.
    The lock state is recorded at the moment the command executes, because the
    atomicity guarantee is exactly that command and settle share one hold."""

    def __init__(self, lock_path: Path, wiring: dict[int, Path], honour_ident: bool) -> None:
        self.lock_path = lock_path
        self.wiring = wiring
        self.honour_ident = honour_ident
        self.ident_calls: list[tuple[int, int, bool]] = []
        self.lock_held_at_command: list[bool] = []

    def read_page(self, device: str, page: str) -> SesResult:
        name = {"configuration": "sg_cf.txt", "additional_element_status": "sg_aes.txt"}[page]
        return SesResult(page=page, stdout=(CAPTURES / name).read_text(), returncode=0)

    def set_ident(self, device: str, type_index: int, element_index: int, on: bool) -> None:
        self.lock_held_at_command.append(_flock_is_held(self.lock_path))
        self.ident_calls.append((type_index, element_index, on))
        if self.honour_ident:
            (self.wiring[element_index] / "locate").write_text("1" if on else "0")


def _ses_writer(
    tmp_path: Path, stale_reads: int = 0, honour_ident: bool = True
) -> tuple[locate_module.SesLocateWriter, _SettleProbingBackend, _CaptureSes]:
    """SesLocateWriter over the real backend and the real captured sysfs tree
    (copied so locate writes stick), with only the sg_ses subprocess faked."""
    root = tmp_path / "sys"
    shutil.copytree(SYSFS_FIXTURE, root)
    lock = tmp_path / "enclosure.lock"
    backend = _SettleProbingBackend(
        sysfs_root=root, dev_root=tmp_path / "dev", lock_path=lock, stale_reads=stale_reads
    )
    ref = backend.resolve(STL3_ID)
    # Element index -> bay directory, identity per the real AES capture (the
    # mapping itself is proven against permuted shelves in test_locate_mapping).
    wiring = {n: backend.slot_dir(ref, n) for n in range(15)}
    ses = _CaptureSes(lock, wiring, honour_ident)
    return locate_module.SesLocateWriter(backend, ses), backend, ses  # type: ignore[arg-type]


def test_ses_write_polls_until_the_attribute_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """set_ident returns before the kernel's cached attribute refreshes, so a
    single immediate read-back would report every successful IDENT as a
    verification failure. Three stale reads must be polled through, and the
    settled True returned."""
    monkeypatch.setattr(locate_module, "DEFAULT_SETTLE_POLL", 0.001)
    writer, backend, ses = _ses_writer(tmp_path, stale_reads=3)

    assert writer.write(STL3_ID, 4, True) is True
    assert ses.ident_calls == [(0, 4, True)]
    # The immediate read plus one per stale value plus the settled one.
    assert backend.settle_polls == 4


def test_ses_write_returns_the_stale_observation_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Current contract on settle timeout: return the observed (stale) value,
    do NOT raise. IdentManager.identify() compares the returned state against
    the requested one, raises the verification failure itself and audits it -
    the unsettled observation IS the failure signal."""
    monkeypatch.setattr(locate_module, "DEFAULT_SETTLE_TIMEOUT", 0.05)
    monkeypatch.setattr(locate_module, "DEFAULT_SETTLE_POLL", 0.001)
    writer, backend, ses = _ses_writer(tmp_path, honour_ident=False)

    assert writer.write(STL3_ID, 4, True) is False
    assert ses.ident_calls == [(0, 4, True)]
    assert backend.settle_polls > 1, "gave up without actually polling to the deadline"


def test_flock_is_held_across_command_and_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write-atomicity guarantee on the DEFAULT path: the enclosure lock
    must cover the SES command AND every settle read, or a concurrent slot
    sweep could sample the bay mid-flight and cache a half-applied locate
    state for the UI to render."""
    monkeypatch.setattr(locate_module, "DEFAULT_SETTLE_POLL", 0.001)
    writer, backend, ses = _ses_writer(tmp_path, stale_reads=2)

    assert writer.write(STL3_ID, 4, True) is True
    assert ses.lock_held_at_command == [True]
    assert backend.settle_polls >= 2
    assert backend.lock_held_at_poll == [True] * backend.settle_polls


# ------------------------------------------------------------ HelperSesRunner


def test_helper_ses_reader_serves_the_real_capture(live_helper: SimpleNamespace) -> None:
    """Telemetry through the full production stack: HelperSesRunner -> socket
    -> IdentHandler.ses_read -> SesRunner -> the fake-sg_ses subprocess. The
    stdout must be the capture verbatim - the parsers downstream get exactly
    this string."""
    runner = HelperSesRunner(live_helper.socket)
    assert runner.available() is True

    result = runner.read_page_for(STL3_ID, "configuration")
    assert result.page == "configuration"
    assert result.returncode == 0
    assert result.stdout == (CAPTURES / "sg_cf.txt").read_text()

    version = runner.version()
    assert version is not None and "fixture replay" in version


def test_helper_ses_reader_refuses_non_allowlisted_page_before_any_io(tmp_path: Path) -> None:
    """The page allow-list is enforced on the CLIENT side too. Pointed at a
    socket that does not exist, a refused page must fail on the allow-list;
    had the request reached the socket first, the error would read 'helper
    unreachable' instead."""
    runner = HelperSesRunner(tmp_path / "never-created.sock")
    with pytest.raises(SesError, match="not an allow-listed read-only page"):
        runner.read_page_for(STL3_ID, "control")


def test_helper_ses_refusal_maps_to_ses_error(live_helper: SimpleNamespace) -> None:
    """An ok=false ses_read response must surface as SesError with the
    helper's reason - HelperSesRunner mirrors SesRunner's exception contract
    so callers never need to know which one is in use."""
    runner = HelperSesRunner(live_helper.socket)
    with pytest.raises(SesError, match="enclosure not attached"):
        runner.read_page_for("0xdeadbeef", "configuration")


def test_helper_ses_reader_degrades_when_the_helper_is_gone(tmp_path: Path) -> None:
    """A crashed helper: available() and version() degrade quietly (that is
    what /healthz and the version endpoint render), while an actual read
    raises SesError like any other SES failure."""
    runner = HelperSesRunner(tmp_path / "never-created.sock")
    assert runner.available() is False
    assert runner.version() is None
    with pytest.raises(SesError, match="helper unreachable"):
        runner.read_page_for(STL3_ID, "configuration")
