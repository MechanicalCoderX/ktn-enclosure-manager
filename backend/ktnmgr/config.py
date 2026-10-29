"""Application settings.

Secrets come from the environment or a 0600 .env file and are held as
``SecretStr`` so they cannot be printed by accident. Nothing here is ever
serialised to the browser (spec §21).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Where the container entrypoint puts the copies uvicorn actually serves. It
#: sits on the private /run/ktn tmpfs so the key copy only ever lives in RAM.
TLS_RUNTIME_DIR = Path("/run/ktn/tls")

#: The only spellings KTN_TLS_ENABLED accepts. scripts/tls-bool.sh implements
#: the same table for the entrypoint, and a test runs both over the same
#: inputs: if they drifted, the entrypoint could start uvicorn without TLS
#: while the app believed it was on (or the reverse).
TLS_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})
TLS_BOOL_FALSE = frozenset({"0", "false", "no", "off", ""})

#: Exported as "1" by docker-entrypoint.sh only once it has built uvicorn's
#: --ssl-* arguments, and unset by it otherwise. See check_tls_marker.
TLS_ACTIVE_MARKER = "KTN_TLS_ACTIVE"


class TlsMismatchError(RuntimeError):
    """The app's KTN_TLS_ENABLED disagrees with what the entrypoint started."""


def check_tls_marker(settings: Settings, environ: Mapping[str, str] | None = None) -> None:
    """Refuse to start unless the app and the entrypoint agree on TLS.

    Why: pydantic-settings matches environment variable NAMES
    case-insensitively, so ``ktn_tls_enabled=true`` turns TLS on here. The
    shell entrypoint and the HEALTHCHECK read the exact name
    ``KTN_TLS_ENABLED``, so for them TLS stays off - and uvicorn would serve
    plain HTTP while ``/healthz`` claimed ``tls.enabled: true``. The VALUE
    spelling is shared (scripts/tls-bool.sh); the name cannot be, so this
    checks the outcome instead: the entrypoint sets the marker only after it
    really set TLS up, and the marker and the setting must match.

    The marker is read by its exact name, like everything the entrypoint
    reads. With TLS unset - plain installs, the test suite, ``python -m
    uvicorn`` in development - both sides are off and nothing changes. Anyone
    running uvicorn by hand with ``--ssl-*`` and KTN_TLS_ENABLED=true sets
    KTN_TLS_ACTIVE=1 too, as tests/tls_certs.run_server does.
    """
    env = os.environ if environ is None else environ
    active = env.get(TLS_ACTIVE_MARKER, "") != ""
    if settings.tls_enabled and not active:
        raise TlsMismatchError(
            "TLS is on in the app's settings (KTN_TLS_ENABLED), but the container "
            f"entrypoint did not start TLS ({TLS_ACTIVE_MARKER} is not set), so uvicorn "
            "would be serving plain HTTP. The entrypoint reads variable names in exact "
            "upper case, while the app's settings accept any case: spell them "
            "KTN_TLS_ENABLED, KTN_TLS_CERTFILE and KTN_TLS_KEYFILE. Refusing to start "
            "rather than serve cleartext. (Running uvicorn by hand with --ssl-*? Set "
            f"{TLS_ACTIVE_MARKER}=1 as well.)"
        )
    if active and not settings.tls_enabled:
        raise TlsMismatchError(
            f"The container entrypoint started TLS ({TLS_ACTIVE_MARKER} is set), but TLS "
            "is off in the app's settings. Most likely a second spelling of the name in "
            "a different case (for example ktn_tls_enabled=false) is overriding "
            "KTN_TLS_ENABLED, because the app's settings ignore case. Refusing to start: "
            "with TLS off here no renewal watcher would run, and a renewed certificate "
            "would never be installed. Remove the other spelling."
        )


def parse_tls_bool(value: str) -> bool:
    """KTN_TLS_ENABLED's spelling rules; ValueError on anything else.

    Narrower than pydantic's own bool parsing on purpose (no 't', 'y', 'f'
    or 'n'), so a shell ``case`` can match it exactly. ASCII only, because
    ``str.lower()`` maps a few non-ASCII letters onto ASCII ones and the shell
    would not.
    """
    if value.isascii():
        lowered = value.lower()
        if lowered in TLS_BOOL_TRUE:
            return True
        if lowered in TLS_BOOL_FALSE:
            return False
    raise ValueError(
        "KTN_TLS_ENABLED must be one of 1/true/yes/on or 0/false/no/off (or empty)"
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KTN_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- server -----------------------------------------------------------
    host: str = "0.0.0.0"  # noqa: S104 - container-internal; published port is chosen by compose
    port: int = 8420
    session_secret: SecretStr = Field(
        default=SecretStr(""),
        description="Signing key for session cookies; generated on first run if unset.",
    )
    session_max_age_seconds: int = 8 * 3600
    forwarded_allow_ips: str = Field(
        default="127.0.0.1",
        description=(
            "Proxy addresses whose X-Forwarded-* headers uvicorn honours "
            "(comma-separated; '*' trusts any). The session cookie's Secure "
            "flag derives from the request scheme, and uvicorn's default only "
            "believes X-Forwarded-Proto from 127.0.0.1 - so behind an external "
            "TLS proxy the cookie was never marked Secure. Set this to the "
            "proxy's address to fix that. Name ONLY the actual proxy: a "
            "trusted peer also controls X-Forwarded-For, which is the address "
            "the login rate limiter keys on, so '*' (or a proxy other clients "
            "can reach) lets a direct client rotate fabricated addresses past "
            "the limiter. The default is uvicorn's own, so an unset "
            "deployment behaves exactly as before. Consumed by the container "
            "entrypoint (--forwarded-allow-ips), not by the app."
        ),
    )

    # --- TLS (opt-in; off by default) -------------------------------------
    #: Serve HTTPS directly from uvicorn. Off by default, so an install that
    #: sets nothing keeps serving plain HTTP exactly as before. When on, the
    #: container entrypoint copies the two files below onto the /run/ktn
    #: tmpfs and refuses to start if they are missing, unparsable or do not
    #: match - it never falls back to plain HTTP.
    tls_enabled: bool = False
    #: The mounted full-chain certificate PEM (the source, e.g. TrueNAS's
    #: /etc/certificates/<name>.crt bound read-only). Watched for renewal.
    tls_certfile: Path | None = None
    #: The mounted private key. Only the root entrypoint ever opens this path
    #: (uid 1000 cannot); uvicorn loads the tmpfs copy once, at startup, and
    #: the app's own code never opens either key file.
    tls_keyfile: Path | None = None
    #: How often the mounted certificate is compared with the served copy.
    #: 0 disables the watcher; otherwise at least 60.
    tls_reload_check_seconds: int = 3600
    #: How long a changed certificate must stay unchanged before the app
    #: restarts to pick it up (TrueNAS writes the cert, then the key).
    tls_reload_settle_seconds: int = 60
    #: Internal: where the served copies live. Only tests change it.
    tls_runtime_dir: Path = TLS_RUNTIME_DIR

    @field_validator("tls_enabled", mode="before")
    @classmethod
    def _tls_enabled_spelling(cls, value: Any) -> Any:
        return parse_tls_bool(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _tls_rules(self) -> Settings:
        if self.tls_reload_check_seconds != 0 and self.tls_reload_check_seconds < 60:
            raise ValueError("KTN_TLS_RELOAD_CHECK_SECONDS must be 0 (off) or at least 60")
        if self.tls_reload_settle_seconds < 5:
            raise ValueError("KTN_TLS_RELOAD_SETTLE_SECONDS must be at least 5")
        if not self.tls_enabled:
            return self
        cert, key = self.tls_certfile, self.tls_keyfile
        for name, value in (("CERTFILE", cert), ("KEYFILE", key)):
            if value is None:
                raise ValueError(f"KTN_TLS_ENABLED is true but KTN_TLS_{name} is not set")
            if not value.is_absolute():
                raise ValueError(f"KTN_TLS_{name} must be an absolute path")
        if os.path.normpath(str(cert)) == os.path.normpath(str(key)):
            raise ValueError("KTN_TLS_CERTFILE and KTN_TLS_KEYFILE must be different files")
        return self

    @property
    def tls_served_certfile(self) -> Path:
        return self.tls_runtime_dir / "cert.pem"

    @property
    def tls_served_keyfile(self) -> Path:
        return self.tls_runtime_dir / "key.pem"

    # --- storage ----------------------------------------------------------
    data_dir: Path = Path("/data")
    enclosure_lock: Path | None = Field(
        default=None,
        description=(
            "Cross-process lock serialising enclosure access. Set by the "
            "container entrypoint to a path on the private /run/ktn tmpfs; "
            "defaults to <data_dir>/enclosure.lock. The web process and the "
            "privileged helper must agree on it."
        ),
    )

    # --- hardware ---------------------------------------------------------
    sysfs_root: Path = Path("/sys")
    dev_root: Path = Path("/dev")
    sg_ses_binary: str | None = None
    ident_helper_socket: Path | None = Field(
        default=None,
        description=(
            "Unix socket of the privileged IDENT helper. When set, the web process "
            "never writes sysfs itself and needs no elevated privilege (§31)."
        ),
    )
    ident_method: str = Field(
        default="auto",
        description=(
            "How the IDENT LED is driven: 'ses' issues a SCSI command (works under "
            "the default container AppArmor profile), 'sysfs' writes the locate "
            "attribute (needs a writable /sys and apparmor=unconfined), 'auto' "
            "prefers ses and falls back to sysfs."
        ),
    )
    #: Optional administrative override for which enclosures to manage (§4).
    enclosure_allowlist: str = ""

    # --- TrueNAS ----------------------------------------------------------
    truenas_url: str = ""
    truenas_api_key: SecretStr = SecretStr("")
    truenas_verify_tls: bool = True
    truenas_ca_bundle: str | None = None
    truenas_rest_fallback: bool = Field(
        default=False,
        description=(
            "Fall back to the legacy REST /api/v2.0 surface when the JSON-RPC "
            "WebSocket is unreachable. Off by default: role-scoped keys are "
            "refused wholesale by REST (403) while working over JSON-RPC, so "
            "on the recommended key this fallback can only produce a false "
            "'rejected the API key' error - and REST is removed in TrueNAS "
            "26.04. Enable only on a deployment using a full-access key."
        ),
    )

    # --- notifications ----------------------------------------------------
    alert_webhook_url: str = Field(
        default="",
        description=(
            "Where to send a message when a bay's health changes. An ntfy topic "
            "URL, or any endpoint that accepts a POST. Empty disables alerting."
        ),
    )
    alert_style: str = Field(
        default="ntfy",
        description=(
            "'ntfy' posts a plain-text body with Title/Priority/Tags headers; "
            "'json' posts a JSON object."
        ),
    )
    alert_on_recovery: bool = True

    # --- polling (§29) ----------------------------------------------------
    poll_slots_seconds: float = 5.0
    poll_truenas_seconds: float = 20.0
    poll_ses_seconds: float = 30.0
    poll_smart_seconds: float = 120.0

    # --- auth -------------------------------------------------------------
    login_rate_limit: int = 5
    login_rate_window_seconds: int = 60

    #: Host names this app answers to (comma-separated; empty allows all).
    #:
    #: This is the DNS-rebinding defence for the opt-in anonymous modes. A
    #: malicious page can repoint its own hostname's DNS at this app and then
    #: read API responses cross-origin, because the browser believes it is
    #: still same-origin with the attacker's site; the giveaway is the Host
    #: header, which still names the attacker's domain. With authentication on
    #: this is moot (the request carries no session cookie for a foreign
    #: hostname and gets 401), so the default stays allow-all rather than
    #: breaking every existing deployment's IP-plus-port bookmark. Matching is
    #: port-insensitive: rebinding cannot change the port, and the port a
    #: browser sends depends on how the app was published.
    allowed_hosts: str = ""

    #: Require a local account to view anything. Default on.
    #:
    #: Turning this off makes the app an open read-only dashboard, which is the
    #: norm for this category on TrueNAS - scrutiny, glances, homepage and
    #: speedtest-tracker all serve disk telemetry with no credentials, and
    #: scrutiny publishes the same class of data (serial, WWN, SMART) that this
    #: app does. It is offered because that norm is real, not because the data
    #: is uninteresting.
    auth_required: bool = True

    #: Allow the IDENT LED write without authentication.
    #:
    #: Deliberately separate from auth_required, and off even when
    #: authentication is disabled. Every comparable open dashboard is strictly
    #: read-only; this app can actuate hardware. The LED is non-destructive -
    #: there is no code path to power a drive off, reset a PHY or touch a fault
    #: LED - but a write reachable by anyone on the network should be a
    #: decision someone made on purpose, not a side effect of opening the
    #: dashboard.
    allow_anonymous_ident: bool = False

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit.log"

    @property
    def users_path(self) -> Path:
        return self.data_dir / "users.json"

    @property
    def notify_state_path(self) -> Path:
        return self.data_dir / "notify-state.json"

    @property
    def ident_state_path(self) -> Path:
        return self.data_dir / "ident-state.json"

    @property
    def secret_path(self) -> Path:
        return self.data_dir / "session-secret"

    @property
    def enclosure_lock_path(self) -> Path:
        """Cross-process lock serialising all access to the enclosure.

        The container entrypoint sets ``KTN_ENCLOSURE_LOCK`` to a path on the
        private ``/run/ktn`` tmpfs, because the file must be world-writable
        (see enclosure/access.py) and that does not belong in the user's data
        dataset. The data directory is the fallback for deployments with no
        privileged helper, where one process owns the file.

        Both processes must resolve the same path or they will not exclude
        each other at all.
        """
        return self.enclosure_lock or (self.data_dir / "enclosure.lock")

    def allowed_enclosures(self) -> set[str]:
        return {e.strip().lower() for e in self.enclosure_allowlist.split(",") if e.strip()}


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
