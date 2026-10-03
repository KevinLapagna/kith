"""Configuration: environment in, frozen settings out.

Nothing here reads a config file, because the container's environment is the source
of truth (Quadlet `EnvironmentFile=`). Tests construct `Settings` directly, which is
why this is a plain dataclass with a `from_env` constructor rather than a singleton.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "Kith"
APP_VERSION = "0.9.0"

# Bumped by hand whenever a migration is added; tests assert it matches the highest
# migration on disk, so the two can never drift apart silently.
SCHEMA_VERSION = 5

_TRUTHY = {"1", "true", "yes", "on"}
_QUOTES = "\"'"


def _unquote(value: str) -> str:
    """Strip one layer of surrounding quotes from an env value.

    The env file is read by whichever parser the deployment path uses, and they
    disagree: systemd's ``EnvironmentFile`` strips surrounding quotes, while podman's
    ``--env-file`` — which is what Quadlet's ``EnvironmentFile=`` becomes — hands the
    container ``'scrypt$16384$…'`` with the quote still attached. A scrypt hash *has*
    to be quoted in the file because it contains ``$`` characters, so the app
    normalises the value instead of depending on which parser ran. Verified on the
    host: without this, ``check-password`` reports MISMATCH for a hash that verifies
    perfectly on its own.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
        return value[1:-1].strip()
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    db_path: Path
    host: str = "0.0.0.0"
    port: int = 8080
    auth_enabled: bool = False
    password_hash: str | None = None
    session_secret: str = ""
    session_secret_generated: bool = False
    session_days: int = 30
    secure_cookies: bool = False
    max_upload_mb: int = 25
    display_tz: str = "Europe/Zurich"
    log_level: str = "info"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        data_dir = Path(env.get("KITH_DATA_DIR", "/data"))
        db_path = Path(env.get("KITH_DB", str(data_dir / "kith.sqlite")))

        auth_enabled = env.get("KITH_AUTH", "off").strip().lower() in _TRUTHY
        secret = (env.get("KITH_SESSION_SECRET") or "").strip()
        generated = False
        if auth_enabled and not secret:
            # A missing secret must not stop the app from booting — but sessions should
            # also not silently die on every restart, so generate one and log loudly
            # (main.py warns). Set KITH_SESSION_SECRET to make logins durable.
            secret, generated = secrets.token_urlsafe(32), True

        return cls(
            data_dir=data_dir,
            db_path=db_path,
            host=env.get("KITH_HOST", "0.0.0.0"),
            port=int(env.get("KITH_PORT", "8080")),
            auth_enabled=auth_enabled,
            password_hash=_unquote(env.get("KITH_PASSWORD_HASH") or "") or None,
            session_secret=secret,
            session_secret_generated=generated,
            session_days=int(env.get("KITH_SESSION_DAYS", "30")),
            secure_cookies=env.get("KITH_SECURE_COOKIES", "off").strip().lower() in _TRUTHY,
            max_upload_mb=int(env.get("KITH_MAX_UPLOAD_MB", "25")),
            display_tz=env.get("TZ", "Europe/Zurich"),
            log_level=env.get("KITH_LOG_LEVEL", "info").strip().lower(),
        )
