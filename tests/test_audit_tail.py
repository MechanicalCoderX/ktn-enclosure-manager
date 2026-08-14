"""Audit log tailing and file permissions.

tail() used to read the entire log - up to the 5 MB rotation threshold - and
split every line on every request, to then discard all but the last hundred.
"""

from __future__ import annotations

import stat
from pathlib import Path

from ktnmgr.services.audit import AuditLog


def write_entries(log: AuditLog, count: int) -> None:
    for i in range(count):
        log.record(
            user=f"user{i}",
            enclosure="0xabc",
            operation="IDENT_ON",
            bay=i,
            ses_slot=i,
            result="1",
            verification="success",
        )


def test_tail_returns_newest_first(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.log")
    write_entries(log, 10)

    entries = log.tail(3)
    assert [e.ses_slot for e in entries] == [9, 8, 7]


def test_tail_spanning_more_than_one_window(tmp_path: Path) -> None:
    """Forces the doubling path: more entries than one small window holds."""
    log = AuditLog(tmp_path / "audit.log")
    write_entries(log, 900)

    entries = log.tail(400)
    assert len(entries) == 400
    assert [e.ses_slot for e in entries[:3]] == [899, 898, 897]


def test_tail_on_a_short_file_returns_everything(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.log")
    write_entries(log, 4)
    assert len(log.tail(100)) == 4


def test_tail_is_clamped_to_max_tail(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.log", max_tail=5)
    write_entries(log, 20)
    assert len(log.tail(1000)) == 5


def test_tail_of_a_missing_log_is_empty(tmp_path: Path) -> None:
    assert AuditLog(tmp_path / "nope.log").tail(10) == []


def test_a_torn_line_does_not_break_the_view(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    log = AuditLog(path)
    write_entries(log, 5)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"partial": ')  # a write cut off mid-record

    entries = log.tail(10)
    assert len(entries) == 5


def test_audit_log_is_not_world_readable(tmp_path: Path) -> None:
    """It carries usernames and drive serials."""
    path = tmp_path / "audit.log"
    log = AuditLog(path)
    write_entries(log, 1)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"audit log mode is {mode:o}"
