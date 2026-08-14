"""Local authentication (spec §33).

"It is only on the LAN" is explicitly not accepted as authentication, so the
app has real local accounts: Argon2id password hashing, signed HttpOnly
session cookies, session expiry, and login rate limiting.

There is no default password and none is ever generated or printed. On first
run the app has no accounts and only the bootstrap endpoint is available; the
administrator sets the password in the browser.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

log = logging.getLogger(__name__)

SESSION_COOKIE = "ktn_session"
MIN_PASSWORD_LENGTH = 12


def _tighten(path: Path) -> None:
    """Best-effort narrowing of a file created before 0600 was enforced."""
    try:
        if path.exists() and stat.S_IMODE(path.stat().st_mode) != 0o600:
            path.chmod(0o600)
            log.info("tightened permissions on %s", path)
    except OSError as exc:
        log.warning("could not tighten permissions on %s: %s", path, exc)


def _write_private(path: Path, text: str) -> None:
    """Write a file that is 0600 from the moment it exists.

    ``write_text`` then ``chmod`` is not equivalent: it creates the file with
    the process umask - usually world-readable - and only narrows it
    afterwards. For the session signing key and the password-hash file that
    window is enough to lose both.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    # Explicit, in case the file already existed with looser permissions:
    # O_CREAT's mode applies only when creating.
    os.chmod(path, 0o600)


class AuthError(Exception):
    """Authentication failed or is not permitted right now."""


@dataclass
class RateLimiter:
    """Fixed-window limiter keyed by client address (§33)."""

    limit: int
    window_seconds: int
    _hits: dict[str, list[float]] = field(default_factory=dict)
    _last_prune: float = 0.0

    def check(self, key: str) -> None:
        """Record an attempt for ``key``, refusing once the window is full.

        Note on deployment: ``key`` is the peer address. Behind a reverse proxy
        every client shares one address, so one attacker would lock out all
        users. This app is meant to be reached directly; if you front it with a
        proxy, rate limit there instead.
        """
        now = time.monotonic()
        self._prune(now)
        recent = [t for t in self._hits.get(key, []) if now - t < self.window_seconds]
        if len(recent) >= self.limit:
            raise AuthError("too many login attempts; try again shortly")
        recent.append(now)
        self._hits[key] = recent

    def _prune(self, now: float) -> None:
        """Drop addresses whose attempts have all aged out.

        Without this the dict grows one entry per distinct source address for
        the process lifetime - a slow memory leak that a scanner could drive.
        """
        if now - self._last_prune < self.window_seconds:
            return
        self._last_prune = now
        self._hits = {
            key: hits
            for key, hits in self._hits.items()
            if any(now - t < self.window_seconds for t in hits)
        }

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


class AuthService:
    def __init__(
        self,
        users_path: Path,
        secret_path: Path,
        session_secret: str | None,
        max_age_seconds: int,
        rate_limit: int,
        rate_window: int,
    ) -> None:
        self.users_path = Path(users_path)
        self.secret_path = Path(secret_path)
        self.max_age_seconds = max_age_seconds
        self.hasher = PasswordHasher()
        self.limiter = RateLimiter(limit=rate_limit, window_seconds=rate_window)
        self._serializer = URLSafeTimedSerializer(
            session_secret or self._load_or_create_secret(), salt="ktn-session"
        )

    # ---------------------------------------------------------------- secrets

    def _load_or_create_secret(self) -> str:
        """Persist a random signing key so sessions survive a restart.

        Written 0600. If it cannot be persisted the app still runs, but every
        restart invalidates existing sessions - which is safe, just less
        convenient.
        """
        try:
            existing = self.secret_path.read_text(encoding="utf-8").strip()
            if existing:
                # Narrow a key written before this was enforced: the O_CREAT
                # mode applies only at creation, so an upgraded deployment
                # would otherwise keep a world-readable signing key - which
                # forges any session - for as long as it runs.
                _tighten(self.secret_path)
                _tighten(self.users_path)
                return existing
        except OSError:
            pass
        generated = secrets.token_urlsafe(48)
        try:
            self.secret_path.parent.mkdir(parents=True, exist_ok=True)
            _write_private(self.secret_path, generated)
        except OSError as exc:
            log.warning("could not persist session secret (%s); sessions reset on restart", exc)
        return generated

    # ------------------------------------------------------------------ users

    def _read_users(self) -> dict[str, dict[str, str]]:
        """Load the account file.

        An absent file means "no accounts yet" and is the normal first-run
        state. A file that exists but cannot be read or parsed is NOT the same
        thing and must never be reported as such: treating it as empty makes
        ``needs_bootstrap`` true, which reopens the unauthenticated bootstrap
        endpoint and lets anyone on the network claim an administrator account -
        and the first write then overwrites the real accounts. A corrupt file,
        a permissions mistake or a half-finished restore would all hand the app
        away. So it fails closed instead.
        """
        if not self.users_path.exists():
            return {}
        try:
            data = json.loads(self.users_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AuthError(
                f"account file {self.users_path} exists but could not be read; "
                "refusing to treat this as an empty account list"
            ) from exc
        if not isinstance(data, dict):
            raise AuthError(f"account file {self.users_path} is not a JSON object")
        return data

    def _write_users(self, users: dict[str, dict[str, str]]) -> None:
        """Replace the account file atomically, never world-readable.

        The mode is set by os.open rather than a chmod after the fact: writing
        first and tightening afterwards leaves the password hashes readable to
        every local user for the width of that window.
        """
        self.users_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.users_path.with_suffix(".tmp")
        _write_private(tmp, json.dumps(users, indent=1))
        tmp.replace(self.users_path)

    @property
    def needs_bootstrap(self) -> bool:
        """True only when there are demonstrably no accounts.

        An unreadable account file is not "no accounts": it is an error, and
        the safe answer is that bootstrap is closed.
        """
        try:
            return not self._read_users()
        except AuthError:
            log.error("account file unreadable; bootstrap stays closed")
            return False

    def bootstrap(self, username: str, password: str) -> None:
        """Create the first administrator. Refuses once any account exists."""
        if not self.needs_bootstrap:
            raise AuthError("an administrator account already exists")
        self.create_user(username, password)

    def create_user(self, username: str, password: str) -> None:
        username = (username or "").strip()
        if not username or not username.isascii() or len(username) > 64:
            raise AuthError("invalid username")
        if len(password or "") < MIN_PASSWORD_LENGTH:
            raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        users = self._read_users()
        users[username] = {
            "password_hash": self.hasher.hash(password),
            "created_at": datetime.now(UTC).isoformat(),
            # Bumped whenever every existing session for this user must stop
            # being accepted. Carried in the session cookie and compared on
            # each request.
            "session_epoch": 0,
        }
        self._write_users(users)
        log.info("created account %s", username)

    def change_password(self, username: str, current: str, new: str) -> None:
        """Change a password and invalidate every existing session for the user.

        Without the epoch bump, a stolen cookie kept working after the victim
        changed their password - which is the one action a user takes when they
        suspect compromise, so it has to be the action that ends the attacker's
        access.
        """
        self.verify(username, current)
        if len(new or "") < MIN_PASSWORD_LENGTH:
            raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        users = self._read_users()
        users[username]["password_hash"] = self.hasher.hash(new)
        users[username]["session_epoch"] = self._epoch_of(users[username]) + 1
        self._write_users(users)

    @staticmethod
    def _epoch_of(record: dict[str, object]) -> int:
        """Session epoch for a user record.

        Accounts created before this field existed have no epoch; they are
        treated as 0 so an upgrade does not sign everyone out.
        """
        try:
            return int(record.get("session_epoch", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def revoke_sessions(self, username: str) -> None:
        """End every session for a user without changing their password."""
        users = self._read_users()
        if username not in users:
            raise AuthError("no such account")
        users[username]["session_epoch"] = self._epoch_of(users[username]) + 1
        self._write_users(users)

    def verify(self, username: str, password: str) -> str:
        users = self._read_users()
        record = users.get(username or "")
        if record is None:
            # Hash anyway so a missing user and a wrong password cost the same.
            self.hasher.hash(password or "x")
            raise AuthError("invalid credentials")
        try:
            self.hasher.verify(record["password_hash"], password or "")
        except (VerifyMismatchError, InvalidHashError, KeyError) as exc:
            raise AuthError("invalid credentials") from exc

        if self.hasher.check_needs_rehash(record["password_hash"]):
            record["password_hash"] = self.hasher.hash(password)
            users[username] = record
            self._write_users(users)
        return username

    # --------------------------------------------------------------- sessions

    def issue_session(self, username: str) -> str:
        record = self._read_users().get(username, {})
        return self._serializer.dumps({"u": username, "e": self._epoch_of(record)})

    def read_session(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            payload = self._serializer.loads(token, max_age=self.max_age_seconds)
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(payload, dict):
            return None
        username = payload.get("u")
        if not username:
            return None
        try:
            record = self._read_users().get(str(username))
        except AuthError:
            # Cannot confirm the account still exists, so do not accept the
            # session. Fails closed, consistent with needs_bootstrap.
            return None
        if record is None:
            return None
        # A cookie issued before this field existed carries no epoch; treat it
        # as 0 so upgrading does not invalidate current sessions.
        try:
            token_epoch = int(payload.get("e", 0) or 0)
        except (TypeError, ValueError):
            return None
        if token_epoch != self._epoch_of(record):
            return None
        return str(username)
