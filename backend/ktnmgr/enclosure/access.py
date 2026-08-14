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
and warns once, because losing enclosure reporting is far worse than a torn
read of a locate flag.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

#: Generous: a slow sg_ses --join on a five-subenclosure shelf takes well
#: under a second, but a timing-out read holds the lock for its full timeout.
DEFAULT_LOCK_TIMEOUT = 30.0
_POLL_INTERVAL = 0.02

#: Warn once per path, not once per poll - this runs every few seconds.
_warned: set[str] = set()
_warned_guard = threading.Lock()

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
    fd: int | None = None
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
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o666)
        try:
            os.fchmod(fd, 0o666)
        except OSError:
            pass  # not the owner; whoever created it already set the mode
    except OSError as exc:
        _warn_once(
            f"open:{path}",
            "cannot create enclosure lock at %s (%s); enclosure access will not be "
            "serialised, which may produce SCSI abort messages in the kernel log",
            path, exc,
        )
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
