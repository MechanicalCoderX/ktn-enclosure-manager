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

    # --- storage ----------------------------------------------------------
    data_dir: Path = Path("/data")

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
    #: Optional administrative override for which enclosures to manage (§4).
    enclosure_allowlist: str = ""

    # --- TrueNAS ----------------------------------------------------------
    truenas_url: str = ""
    truenas_api_key: SecretStr = SecretStr("")
    truenas_verify_tls: bool = True
    truenas_ca_bundle: str | None = None

    # --- polling (§29) ----------------------------------------------------
    poll_slots_seconds: float = 5.0
    poll_truenas_seconds: float = 20.0
    poll_ses_seconds: float = 30.0
    poll_smart_seconds: float = 120.0

    # --- auth -------------------------------------------------------------
    login_rate_limit: int = 5
    login_rate_window_seconds: int = 60

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit.log"

    @property
    def users_path(self) -> Path:
        return self.data_dir / "users.json"

    @property
    def ident_state_path(self) -> Path:
        return self.data_dir / "ident-state.json"

    @property
    def secret_path(self) -> Path:
        return self.data_dir / "session-secret"

    def allowed_enclosures(self) -> set[str]:
        return {e.strip().lower() for e in self.enclosure_allowlist.split(",") if e.strip()}


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
