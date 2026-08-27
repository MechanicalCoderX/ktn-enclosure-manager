"""Application settings.

Secrets come from the environment or a 0600 .env file and are held as
``SecretStr`` so they cannot be printed by accident. Nothing here is ever
serialised to the browser (spec §21).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
