"""Serialised access to the enclosure processor.

This exists so an IDENT write and its settle read-back cannot be interleaved
with other enclosure traffic. ``set_locate()`` writes the attribute and then
polls it for up to two seconds waiting for the enclosure processor to
acknowledge; without the lock a concurrent reader can observe - and cache, and
show in the UI - a half-applied locate state.

It is an ``flock`` rather than a ``threading.Lock`` because the readers live in
**different processes**: the web process reads sysfs as uid 1000, while
``sg_ses`` is executed by the privileged helper. A lock inside either one
protects nothing.

Historical note, because the comment here used to claim otherwise: this lock
was introduced in 1.2.1 to stop the HBA logging thousands of
``log_info(0x31120434) ... code(0x12)`` (``PL_LOGINFO_CODE_ABORT``) messages a
day, on the theory that concurrent SES access was causing them. That theory
was wrong and the lock changed nothing - the rate was identical afterwards.
The real cause was sg_ses issuing an unsupported REPORT TIMESTAMP command on
every invocation; see ``BASE_ARGS`` in ses.py. The lock is kept for the
write-atomicity reason above, which is genuine and independent.

Failure to take the lock is never fatal: it degrades to unsynchronised access
and logs it (once, or for a refused substitute once an hour per kind), because
losing enclosure reporting is far worse than a torn read of a locate flag.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

#: How the lock is opened. The root helper opens it on every poll, in a
#: directory the web process's gid can write and that has no sticky bit, so
#: the path must be treated as attacker-controlled (see _open_lock()). Adding
#: a sticky bit would not make the kernel refuse a planted link either:
#: fs.protected_symlinks only engages in directories that are sticky AND
#: world-writable, and /run/ktn is 0770/2770.
#:
#: - O_NOFOLLOW: a symlink in the final component fails with ELOOP instead of
#:   being followed - including a dangling one, which O_CREAT would otherwise
#:   create as root wherever it points.
#: - O_NONBLOCK and O_NOCTTY, defence in depth: an open that could wait
#:   returns at once, and a terminal device cannot become the helper's
#:   controlling tty. Linux never blocks an O_RDWR open of a FIFO (POSIX
#:   leaves it undefined), and the web process cannot create device nodes
#:   (no CAP_MKNOD; the tmpfs is mounted nodev).
_OPEN_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY
)

#: open() errors that mean something other than a lock file is at the path:
#: a socket or device, or a directory. ELOOP is classified separately (see
#: _eloop_kind()) because a parent-directory loop raises it too.
_NOT_A_LOCK_FILE = {
    errno.ENXIO: "is a socket or device",
    errno.EISDIR: "is a directory",
}


class UnsafeLockPath(OSError):
    """Something other than a lock file of its own is at the lock path.

    ``kind`` is one of a small fixed set of phrases and is what refusals are
    logged by (see _log_refusal()); ``strerror`` may add detail to it, such as
    the file type or the link count.
    """

    def __init__(self, err: int, kind: str, detail: str | None = None) -> None:
        super().__init__(err, f"{kind} ({detail})" if detail else kind)
        self.kind = kind


#: Generous: a slow sg_ses --join on a five-subenclosure shelf takes well
#: under a second, but a timing-out read holds the lock for its full timeout.
DEFAULT_LOCK_TIMEOUT = 30.0
_POLL_INTERVAL = 0.02

#: Warn once per path, not once per poll - this runs every few seconds.
_warned: set[str] = set()
_warned_guard = threading.Lock()

#: Refusals are logged per (path, kind of thing found), each at most once an
#: interval. A different kind of substitute is reported at once, and one that
#: persists or comes back is reported again the next interval, rather than
#: once per process lifetime. The bound matters because the web process
#: drives the helper's request rate: whatever it plants, and however often it
#: swaps the plant, the root helper writes at most one line per kind (a fixed
#: set of six) per path per interval.
_REFUSAL_LOG_INTERVAL = 3600.0
_refusals_logged: dict[tuple[str, str], float] = {}

#: flock is per-file-descriptor, so a nested acquire on a *second* descriptor
#: in the same thread would deadlock against itself. set_locate() calls
#: slot_dir(), which reads sysfs, so nesting is real and not hypothetical.
_depth = threading.local()


def default_lock_path() -> Path:
    """Where both processes agree to put the lock.

    The container entrypoint points this at ``/run/ktn/enclosure.lock`` - the
    same private tmpfs that holds the helper socket. That is the right home
    for it: the file has to be mode 0666 (see enclosure_access() for why it
    cannot be tightened), and a world-writable file belongs on a tmpfs nobody
    else can see rather than in the user's data dataset, where it would also
    end up in their backups.

    The data directory is the fallback for deployments with no helper, where a
    single process owns the file and the mode is its own business.
    """
    override = os.environ.get("KTN_ENCLOSURE_LOCK")
    if override:
        return Path(override)
    return Path(os.environ.get("KTN_DATA_DIR", "/data")) / "enclosure.lock"


def _warn_once(key: str, message: str, *args: object) -> None:
    with _warned_guard:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(message, *args)


def _log_refusal(path: Path, exc: UnsafeLockPath) -> None:
    """Log a refused lock path at ERROR, rate-limited per (path, kind)."""
    key = (str(path), exc.kind)
    now = time.monotonic()
    with _warned_guard:
        last = _refusals_logged.get(key)
        if last is not None and now - last < _REFUSAL_LOG_INTERVAL:
            return
        _refusals_logged[key] = now
    # euid: the helper and the web process log the same lines to the same
    # stream, and only a refusal by the root helper is the security event.
    log.error(
        "refusing to use %s as the enclosure lock (euid %d): it %s. Enclosure "
        "access is not serialised until that path holds the lock file itself",
        path, os.geteuid(), exc.strerror,
    )


def _eloop_kind(path: Path) -> str:
    """Name what an ELOOP from the lock open means.

    Under O_NOFOLLOW it almost always means the final component is a symlink.
    It can also mean a directory on the way there is a symlink loop, which
    lstat() - also refusing to resolve it - tells apart. That is a
    misconfigured path rather than a plant: /run/ktn is a tmpfs mount point
    under the root-owned /run, so the web process cannot put a link above
    the lock. Only the message differs; both are refused.
    """
    try:
        os.lstat(path)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return "is reached through a symlink loop"
    return "is a symlink"


def _open_lock(path: Path) -> int:
    """Open (creating if absent) the lock file and return its descriptor.

    Raises UnsafeLockPath if the path is anything but a regular file whose
    only name is this one, and OSError for any other failure to open it.

    Why each check exists. The helper is root, and root owns nearly every
    file in the container, so an fchmod that lands on one of them needs no
    capability at all:

    - A symlink would redirect the open, and the fchmod below, to a file of
      the attacker's choosing - or, dangling, make O_CREAT create one as root.
      O_NOFOLLOW refuses it before anything is opened.
    - A hard link is a second name for another file's inode, which the fchmod
      would reach just the same. A lock file has exactly one name.
    - A FIFO, socket or device is not a lock file, and opening a device can
      block or have side effects. O_NONBLOCK and O_NOCTTY keep the open itself
      harmless; fstat() on the descriptor then refuses it before any use.

    fstat() is on the descriptor, not the path, so the checks describe the
    object actually opened, and a swap of the directory entry after the open
    cannot change what they saw. This is the invariant the helper keeps for
    its socket in the same directory (lstat + S_ISSOCK in
    ktn_ident_helper.py), without that check's one-syscall window: a regular
    file can be checked and chmod'ed through its descriptor, a socket's
    directory entry cannot.

    Pre-creating the file as root in the entrypoint would not replace any of
    this with the shipped tmpfs modes (0770/2770, no sticky bit): the web gid
    can unlink and replace the entry at any time after it is created. Only a
    sticky bit as well would stop that, and the catalog cannot express one.
    """
    try:
        fd = os.open(path, _OPEN_FLAGS, 0o666)
    except OSError as exc:
        err = exc.errno or 0
        if err == errno.ELOOP:
            raise UnsafeLockPath(err, _eloop_kind(path)) from exc
        what = _NOT_A_LOCK_FILE.get(err)
        if what is not None:
            raise UnsafeLockPath(err, what) from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeLockPath(
                errno.EINVAL, "is not a regular file", stat.filemode(st.st_mode)
            )
        if st.st_nlink != 1:
            raise UnsafeLockPath(
                errno.EMLINK, "has a link count other than 1", f"{st.st_nlink} links"
            )
        # 0666, deliberately; see enclosure_access(). Only the owner may
        # chmod (the container has no CAP_FOWNER), so a file the other
        # process created is left as its creator set it.
        if st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) != 0o666:
            try:
                os.fchmod(fd, 0o666)
            except OSError:
                pass  # never fatal; the other process will say if it is locked out
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def enclosure_access(
    lock_path: Path | str | None = None, timeout: float = DEFAULT_LOCK_TIMEOUT
) -> Iterator[bool]:
    """Hold the enclosure lock for the duration of the block.

    Yields True if the lock was actually held, False if it degraded to
    unsynchronised access. Re-entrant within a thread.
    """
    if getattr(_depth, "value", 0):
        _depth.value += 1
        try:
            yield True
        finally:
            _depth.value -= 1
        return

    path = Path(lock_path) if lock_path is not None else default_lock_path()
    try:
        # 0666, deliberately, and it belongs on the private /run/ktn tmpfs
        # rather than in the user's data dataset - see default_lock_path().
        #
        # It cannot be tightened. The two processes are root (the helper) and
        # uid 1000 (the web process), and either may create the file first.
        # Root does NOT bypass the permission check here: the container drops
        # every capability except SETUID/SETGID, so it has no CAP_DAC_OVERRIDE
        # and is subject to the mode like anyone else. A 0600 file owned by
        # whichever process happened to start first therefore locks the other
        # one out, which was measured, not theorised - the helper failed with
        # EACCES and the app fell back to unsynchronised access.
        #
        # The mode is safe only because _open_lock() guarantees the 0666 lands
        # on the lock file itself and nothing else; see there.
        fd = _open_lock(path)
    except UnsafeLockPath as exc:
        # Refused, not failed: the web process's gid can write this directory,
        # so this is either a misconfigured path or tampering. Degrade exactly
        # as below - nothing is removed, because the entry is not ours.
        _log_refusal(path, exc)
        degraded = True
    except OSError as exc:
        _warn_once(
            f"open:{path}",
            "cannot open enclosure lock at %s (%s); enclosure access will not be "
            "serialised, so a concurrent read may observe a half-applied locate "
            "state", path, exc,
        )
        degraded = True
    else:
        degraded = False
    # Yielded outside the except blocks, so an exception raised in the
    # caller's with-body is not chained (__context__) to the open failure.
    if degraded:
        yield False
        return

    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    _warn_once(
                        f"timeout:{path}",
                        "enclosure lock at %s not acquired within %ss; proceeding "
                        "unsynchronised", path, timeout,
                    )
                    break
                time.sleep(_POLL_INTERVAL)

        _depth.value = 1
        try:
            yield acquired
        finally:
            _depth.value = 0
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
