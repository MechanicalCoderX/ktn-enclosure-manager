"""Local authentication (spec §33).

"It is only on the LAN" is explicitly not accepted as authentication, so the
app has real local accounts: Argon2id password hashing, signed HttpOnly
session cookies, session expiry, and login rate limiting.

There is no default password and none is ever generated or printed. On first
run the app has no accounts and only the bootstrap endpoint is available; the
administrator sets the password in the browser.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

log = logging.getLogger(__name__)

SESSION_COOKIE = "ktn_session"
MIN_PASSWORD_LENGTH = 12

#: Names an account may not take.
#:
#: ``anonymous`` is the string the API layer uses to mean "no session", and it
#: compares against it to decide whether to refuse the IDENT write and the
#: password change. An account actually called ``anonymous`` therefore collided
#: with the sentinel and was refused both - measured, not theorised. It fails
#: closed, so it denied a legitimate user rather than admitting an illegitimate
#: one, but reserving the name removes the ambiguity entirely.
RESERVED_USERNAMES = frozenset({"anonymous"})

#: Characters an account name may contain.
#:
#: Deliberately an allow-list. Usernames are written into the application log
#: and into every audit line, so a name containing a newline can forge log
#: entries - and the audit log is the record that is meant to establish what
#: actually happened.
USERNAME_RE = re.compile(r"[A-Za-z0-9._@-]{1,64}")

ACCOUNT_EXISTS = "an administrator account already exists"

#: One lock per account file, shared by every AuthService in this process that
#: points at it.
#:
#: Every account change is a read-modify-write of one JSON file, and the
#: routes that make them are plain ``def`` endpoints that FastAPI runs on a
#: threadpool - so two of them really do run at once. Unserialised, two
#: first-run bootstraps both saw "no accounts", both answered ``{"ok": true}``
#: and the second silently replaced the first administrator; concurrent
#: revocations lost epoch bumps; and a login's opportunistic rehash could write
#: back the account list it read before a password change, undoing it.
#:
#: Keyed by path rather than held per instance so that two services on the
#: same file (tests build several) still exclude each other.
#:
#: In-process only, deliberately: the web app is the only writer of this file
#: and runs as a single uvicorn process (the entrypoint passes no --workers,
#: nothing in the image or the app templates sets WEB_CONCURRENCY - which
#: uvicorn also reads - and the privileged helper never imports this module).
#: A second process writing it is not a supported configuration - the login
#: rate limiter is per-process too - and even then the unique temp file below
#: keeps every write whole, so the worst case is last-writer-wins, never a
#: torn file.
_account_locks: dict[str, threading.Lock] = {}
_account_locks_guard = threading.Lock()

#: A leftover account temp file is removed at startup only once it is at least
#: this old. Every write renames its temp file into place within milliseconds,
#: and in this process none can be in flight during the sweep (it holds the
#: account lock), so the age guard exists for a second process on the same
#: directory - unsupported, but it must never lose a write to the sweep.
STALE_TEMP_AGE_SECONDS = 60.0

#: The random part of a tempfile.mkstemp name: 8 characters from this set
#: (CPython's tempfile._RandomNameSequence). If a future Python changed it, the
#: sweep would stop matching and remove nothing - a test pins it.
_MKSTEMP_RANDOM = r"[a-z0-9_]{8}"


def _account_lock(path: Path) -> threading.Lock:
    key = os.path.realpath(path)
    with _account_locks_guard:
        lock = _account_locks.get(key)
        if lock is None:
            lock = _account_locks[key] = threading.Lock()
        return lock


def _fsync_directory(directory: Path) -> None:
    """Make a rename inside ``directory`` durable. Best effort.

    Without it a power cut shortly after the replace can bring back the
    previous account file - the previous password, the previous session
    epoch. Not every filesystem lets a directory be fsync'ed, and failing the
    write over it would be worse than the risk it addresses.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
            # The filesystem does not support fsync on a directory: expected
            # on some, and nothing the operator can act on.
            log.debug("could not fsync %s: %s", directory, exc)
        else:
            # Anything else (EIO above all) is a storage fault worth seeing.
            # Still not raised: the new file is already in place and whole.
            log.warning(
                "could not fsync %s after replacing the account file; the change "
                "may not survive a power loss: %s",
                directory,
                exc,
            )
    finally:
        os.close(fd)


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


class UserRecord(TypedDict, total=False):
    """One account in users.json, as written by create_user.

    ``total=False`` because the file is read back without schema validation
    and accounts created by older releases lack fields: ``session_epoch``
    did not always exist (see _epoch_of), and the code that reads
    ``password_hash`` already treats its absence as invalid credentials.
    """

    password_hash: str
    created_at: str
    session_epoch: int


class AuthError(Exception):
    """Authentication failed or is not permitted right now."""


@dataclass
class RateLimiter:
    """Fixed-window limiter keyed by client address (§33)."""

    limit: int
    window_seconds: int
    _hits: dict[str, list[float]] = field(default_factory=dict)
    _last_prune: float = 0.0
    #: The routes that call this limiter are plain ``def`` endpoints, so
    #: FastAPI runs them on a threadpool (~40 workers). ``check`` is a
    #: read-modify-write on ``_hits``; unlocked, parallel requests each read
    #: the same "4 attempts" list and all conclude there is room, so a burst
    #: of concurrent logins exceeds the configured limit - which defeats the
    #: one control standing between a password and a brute force. The critical
    #: section is a few dict operations (microseconds); the Argon2 work that
    #: actually costs time happens outside it, in the caller.
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, key: str) -> None:
        """Record an attempt for ``key``, refusing once the window is full.

        Note on deployment: ``key`` is the peer address. Behind a reverse proxy
        every client shares one address, so one attacker would lock out all
        users. This app is meant to be reached directly; if you front it with a
        proxy, rate limit there instead.
        """
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            recent = [t for t in self._hits.get(key, []) if now - t < self.window_seconds]
            if len(recent) >= self.limit:
                # Not "login attempts": the same limiter now also guards the
                # password-verifying change-password endpoint.
                raise AuthError("too many attempts; try again shortly")
            recent.append(now)
            self._hits[key] = recent

    def _prune(self, now: float) -> None:
        """Drop addresses whose attempts have all aged out. Caller holds _lock.

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
        with self._lock:
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
        #: Held across every read-modify-write of the account file; see
        #: _account_locks. Plain reads do not take it: the file is only ever
        #: swapped in whole, so a reader sees the old version or the new one.
        self._accounts_lock = _account_lock(self.users_path)
        self.secret_path = Path(secret_path)
        self.max_age_seconds = max_age_seconds
        self.hasher = PasswordHasher()
        self.limiter = RateLimiter(limit=rate_limit, window_seconds=rate_window)
        self._serializer = URLSafeTimedSerializer(
            session_secret or self._load_or_create_secret(), salt="ktn-session"
        )
        self._sweep_stale_temp_files()

    def _sweep_stale_temp_files(self) -> None:
        """Remove account temp files a crash left behind. Best effort.

        A process killed between creating its temp file and renaming it into
        place leaves a ``users.json.<random>.tmp``, and releases before the
        unique temp file left a fixed-name ``users.tmp``. Nothing reads
        either, but each holds the password hashes of its moment - including
        a hash that was changed *because* it leaked - and backups of the data
        directory carry them on. So at startup, under the account lock:

        - only names that match exactly are touched: the mkstemp pattern
          _write_users uses, or the legacy ``users.tmp``;
        - only regular files: never a symlink (not followed, not removed), a
          directory or anything else;
        - only once at least STALE_TEMP_AGE_SECONDS old.

        A failure is logged and never stops the app starting.
        """
        directory = self.users_path.parent
        own_temp = re.compile(
            re.escape(f"{self.users_path.name}.") + _MKSTEMP_RANDOM + re.escape(".tmp")
        )
        legacy = self.users_path.with_suffix(".tmp").name
        with self._accounts_lock:
            try:
                entries = list(os.scandir(directory))
            except FileNotFoundError:
                return  # first run: no data directory yet, nothing to sweep
            except OSError as exc:
                log.warning("could not list %s to remove stale temp files: %s", directory, exc)
                return
            now = time.time()
            for entry in entries:
                name = entry.name
                if name == self.users_path.name:
                    continue
                if name != legacy and not own_temp.fullmatch(name):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                if now - info.st_mtime < STALE_TEMP_AGE_SECONDS:
                    continue
                try:
                    os.unlink(entry.path)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    log.warning("could not remove stale account temp file %s: %s", name, exc)
                    continue
                log.info("removed stale account temp file %s", name)

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

    def _read_users(self) -> dict[str, UserRecord]:
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

    def _write_users(self, users: dict[str, UserRecord]) -> None:
        """Replace the account file atomically and durably, never world-readable.

        The caller holds ``self._accounts_lock``: this is the write half of a
        read-modify-write.

        - The temp file is unique to this write. Every writer used to share one
          ``users.tmp``: two at once truncated each other's half-written copy,
          the copy renamed into place could still be written to after it had
          become the live file, and the second rename failed outright. Readers
          then saw invalid JSON, which fails closed - everyone locked out.
        - It is 0600 from creation (mkstemp), pinned with fchmod whatever the
          umask: writing first and tightening afterwards leaves the password
          hashes readable to every local user for the width of that window.
        - It is fsync'ed before the rename, so a crash cannot swap in an empty
          or truncated file - which would also fail closed.
        - It is removed again if anything fails before the rename, because it
          holds the same hashes as the real file. A crash cannot run that
          cleanup; the next startup does (_sweep_stale_temp_files).
        """
        directory = self.users_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(users, indent=1).encode("utf-8")
        fd, name = tempfile.mkstemp(dir=directory, prefix=f"{self.users_path.name}.", suffix=".tmp")
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.users_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _fsync_directory(directory)

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
        """Create the first administrator. Refuses once any account exists.

        Checked twice. This first check keeps the existing answer for a closed
        bootstrap, whatever else is wrong with the request. The second one
        decides: it runs under the account lock immediately before the write,
        so two bootstraps that both got past this one - the Argon2 hash between
        check and write is tens of milliseconds wide - cannot both succeed. The
        loser gets the same refusal as any bootstrap once an account exists.
        """
        if not self.needs_bootstrap:
            raise AuthError(ACCOUNT_EXISTS)
        self._create_user(username, password, only_if_first=True)

    def create_user(self, username: str, password: str) -> None:
        self._create_user(username, password, only_if_first=False)

    def _create_user(self, username: str, password: str, *, only_if_first: bool) -> None:
        username = (username or "").strip()
        if not USERNAME_RE.fullmatch(username or ""):
            # An allow-list, not a length-and-isascii check. The previous test
            # accepted control characters - "ad\nmin" passed - and the username
            # is written straight into the application log and the audit line
            # (`audit user=%s ...`). A newline there lets an account name forge
            # whole log entries, which undermines the one record that is
            # supposed to say what happened.
            raise AuthError(
                "invalid username: use 1-64 characters from a-z, A-Z, 0-9, "
                "dot, underscore, hyphen or @"
            )
        if username.lower() in RESERVED_USERNAMES:
            # See RESERVED_USERNAMES: this name is the sentinel the API layer
            # uses for an unauthenticated caller, so an account holding it
            # would be refused the very operations it is entitled to.
            raise AuthError(f"the username {username!r} is reserved")
        if len(password or "") < MIN_PASSWORD_LENGTH:
            raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        # Hashed before the lock is taken: Argon2 is the slow part and touches
        # no shared state.
        password_hash = self.hasher.hash(password)
        with self._accounts_lock:
            if only_if_first and not self.needs_bootstrap:
                raise AuthError(ACCOUNT_EXISTS)
            users = self._read_users()
            users[username] = {
                "password_hash": password_hash,
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

        All of it runs under the account lock, verification included, because
        "replace the password if it is still ``current``" has to be one step.
        It used to read the account list, spend its Argon2 time, then write
        back what it had read: revocations landing in between were discarded,
        and a revocation or login rehash that had read earlier could write the
        old password back over this change. Account changes are rare, so
        holding the lock across the hashing costs nothing that matters; plain
        sign-ins never take it.
        """
        with self._accounts_lock:
            users, record = self._check_password(username, current)
            if len(new or "") < MIN_PASSWORD_LENGTH:
                raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
            # Checked here and not only in the browser: a password change made
            # because the old one was exposed has to actually change it, and
            # the UI is not a control.
            if new == current:
                raise AuthError("new password must differ from the current one")
            record["password_hash"] = self.hasher.hash(new)
            record["session_epoch"] = self._epoch_of(record) + 1
            self._write_users(users)

    @staticmethod
    def _epoch_of(record: UserRecord) -> int:
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
        with self._accounts_lock:
            users = self._read_users()
            if username not in users:
                raise AuthError("no such account")
            users[username]["session_epoch"] = self._epoch_of(users[username]) + 1
            self._write_users(users)

    def verify(self, username: str, password: str) -> str:
        """Check a password; return the username. Raises AuthError.

        For a sign-in use login(), which also issues the session: a session
        issued separately after this returns reads the account again and can
        pick up a password change that landed in between.
        """
        self._verify_record(username, password)
        return username

    def login(self, username: str, password: str) -> tuple[str, str]:
        """Verify a password and issue a session; return (username, token).

        The session carries the epoch of the record that was verified, not
        one read afterwards. Sign-in used to verify, then issue a session by
        re-reading the account file, so a password change that committed
        during the verify (one Argon2 verify wide) handed the old-password
        sign-in a cookie with the NEW epoch: a session that survived the
        change meant to end it. A successful sign-in resets the rate limiter,
        so someone holding the old password could keep that window open by
        signing in back to back. Now such a sign-in gets a session that is
        already dead, the same as if it had finished just before the change.
        A sign-in racing "sign out everywhere" likewise gets a dead session
        and signs in again. Both fail closed.
        """
        record = self._verify_record(username, password)
        return username, self._session_token(username, self._epoch_of(record))

    def _verify_record(self, username: str, password: str) -> UserRecord:
        """Verify a password, rehash it if due, and return the record verified.

        Takes ``_accounts_lock`` when a rehash is due (see _rehash): never call
        this, verify() or login() with that lock held - it is not re-entrant,
        and the hang would show only after an Argon2 parameter change.
        """
        _, record = self._check_password(username, password)
        verified_hash = record["password_hash"]
        if self.hasher.check_needs_rehash(verified_hash):
            self._rehash(username, password, verified_hash)
        return record

    def _check_password(
        self, username: str, password: str
    ) -> tuple[dict[str, UserRecord], UserRecord]:
        """Verify a password; return the account list and the matching record.

        The record is the one inside the returned list, so a caller holding
        the account lock can change it and write the list back.
        """
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
        return users, record

    def _rehash(self, username: str, password: str, verified_hash: str) -> None:
        """Upgrade a hash made with older Argon2 parameters - without losing a
        concurrent change.

        Takes ``_accounts_lock``. Never call it - or verify(), login() or
        _verify_record(), which reach it - with that lock held: the lock is
        not re-entrant, so the caller hangs, and only once a rehash is due,
        i.e. after an Argon2 parameter change, long after its tests passed. Do
        not make the lock an RLock to "fix" that: the inner write would then
        overwrite the caller's read-modify-write instead of hanging.

        Sign-in does not hold the account lock, or every login would queue
        behind Argon2. So the new hash is computed first, and written only if
        the stored hash is still the one just verified. The rehash used to
        write back the whole account list it had read before verifying: a
        password change that landed in between was undone, the old password
        and the old session epoch with it.
        """
        new_hash = self.hasher.hash(password)
        with self._accounts_lock:
            users = self._read_users()
            record = users.get(username)
            if record is None or record.get("password_hash") != verified_hash:
                return
            record["password_hash"] = new_hash
            self._write_users(users)

    # --------------------------------------------------------------- sessions

    def issue_session(self, username: str) -> str:
        """A session for ``username`` at its current epoch, read now.

        Not for sign-in: see login(), which binds the session to the record
        it verified.
        """
        record = self._read_users().get(username, {})
        return self._session_token(username, self._epoch_of(record))

    def _session_token(self, username: str, epoch: int) -> str:
        return self._serializer.dumps({"u": username, "e": epoch})

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
