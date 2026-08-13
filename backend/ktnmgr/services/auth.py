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
import secrets
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


class AuthError(Exception):
    """Authentication failed or is not permitted right now."""


@dataclass
class RateLimiter:
    """Fixed-window limiter keyed by client address (§33)."""

    limit: int
    window_seconds: int
    _hits: dict[str, list[float]] = field(default_factory=dict)

    def check(self, key: str) -> None:
        now = time.monotonic()
        recent = [t for t in self._hits.get(key, []) if now - t < self.window_seconds]
        if len(recent) >= self.limit:
            raise AuthError("too many login attempts; try again shortly")
        recent.append(now)
        self._hits[key] = recent

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
                return existing
        except OSError:
            pass
        generated = secrets.token_urlsafe(48)
        try:
            self.secret_path.parent.mkdir(parents=True, exist_ok=True)
            self.secret_path.write_text(generated, encoding="utf-8")
            self.secret_path.chmod(0o600)
        except OSError as exc:
            log.warning("could not persist session secret (%s); sessions reset on restart", exc)
        return generated

    # ------------------------------------------------------------------ users

    def _read_users(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.users_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_users(self, users: dict[str, dict[str, str]]) -> None:
        self.users_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.users_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(users, indent=1), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.users_path)

    @property
    def needs_bootstrap(self) -> bool:
        return not self._read_users()

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
        }
        self._write_users(users)
        log.info("created account %s", username)

    def change_password(self, username: str, current: str, new: str) -> None:
        self.verify(username, current)
        if len(new or "") < MIN_PASSWORD_LENGTH:
            raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        users = self._read_users()
        users[username]["password_hash"] = self.hasher.hash(new)
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
        return self._serializer.dumps({"u": username})

    def read_session(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            payload = self._serializer.loads(token, max_age=self.max_age_seconds)
        except (BadSignature, SignatureExpired):
            return None
        username = payload.get("u") if isinstance(payload, dict) else None
        if not username or username not in self._read_users():
            return None
        return str(username)
