"""Settings + credential tests.

The interesting cases are all about the env file: Quadlet hands it to the container via
podman's `--env-file`, which does *not* strip surrounding quotes, while systemd's own
`EnvironmentFile` does. A scrypt hash must be quoted because it contains `$`, so the
two parsers disagree about the value the app finally sees — the app has to normalise.
"""

from __future__ import annotations

from app import auth, config


def _settings(**overrides: str) -> config.Settings:
    env = {"KITH_DATA_DIR": "/tmp/kith-config-test"}
    env.update(overrides)
    return config.Settings.from_env(env)


def test_hash_round_trips() -> None:
    digest = auth.hash_password("correct horse")
    assert auth.verify_password(digest, "correct horse") is True
    assert auth.verify_password(digest, "correct horsey") is False


def test_unquoted_hash_is_read_unchanged() -> None:
    digest = auth.hash_password("pw")
    assert _settings(KITH_PASSWORD_HASH=digest).password_hash == digest


def test_single_quoted_hash_is_unquoted_and_still_verifies() -> None:
    digest = auth.hash_password("pw")
    loaded = _settings(KITH_PASSWORD_HASH=f"'{digest}'").password_hash
    assert loaded == digest
    assert loaded is not None and auth.verify_password(loaded, "pw") is True


def test_double_quoted_hash_is_unquoted_too() -> None:
    digest = auth.hash_password("pw")
    assert _settings(KITH_PASSWORD_HASH=f'"{digest}"').password_hash == digest


def test_a_lone_quote_is_left_alone() -> None:
    # Only a matching pair is stripped; a half-quoted value stays visible as broken
    # rather than being silently "fixed" into something that might authenticate.
    assert _settings(KITH_PASSWORD_HASH="'abc").password_hash == "'abc"


def test_absent_hash_is_none_rather_than_an_empty_string() -> None:
    assert _settings().password_hash is None
    assert _settings(KITH_PASSWORD_HASH="").password_hash is None
    assert _settings(KITH_PASSWORD_HASH="''").password_hash is None


def test_auth_off_by_default_and_when_switched_on_a_secret_is_generated() -> None:
    assert _settings().auth_enabled is False

    on = _settings(KITH_AUTH="on")
    assert on.auth_enabled is True
    assert on.session_secret and on.session_secret_generated is True

    configured = _settings(KITH_AUTH="on", KITH_SESSION_SECRET="preset")
    assert configured.session_secret == "preset"
    assert configured.session_secret_generated is False
