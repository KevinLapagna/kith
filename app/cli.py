"""Operator CLI — the pieces that need a terminal rather than a web page.

Run it inside the running container, where the environment (and therefore the same
`Settings`) is already correct:

    podman exec -it kith python -m app.cli hash-password
    podman exec -it kith python -m app.cli check-password

`hash-password` is the only supported way to produce a credential: the plaintext
password is typed at a prompt, never passed as an argument (arguments are visible in
`ps` and in shell history), and only the scrypt hash is printed.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app import auth, config


def hash_password_prompt() -> str:
    first = getpass.getpass("New Kith password: ")
    if not first:
        raise SystemExit("refusing an empty password")
    if first != getpass.getpass("Repeat: "):
        raise SystemExit("the two entries differ — nothing was written")
    return auth.hash_password(first)


def cmd_hash_password(_: argparse.Namespace) -> int:
    digest = hash_password_prompt()
    print(f"KITH_PASSWORD_HASH='{digest}'")
    print(
        "\n# Put that single-quoted line in ~/.config/kith/kith.env and restart the unit.\n"
        "# The quotes are not decoration: the hash contains $ characters, and both the\n"
        "# systemd and podman env-file parsers are safest with the value quoted.",
        file=sys.stderr,
    )
    return 0


def cmd_check_password(_: argparse.Namespace) -> int:
    """Verify the configured hash works, without touching the database."""
    settings = config.Settings.from_env()
    if not settings.password_hash:
        print("KITH_PASSWORD_HASH is not set", file=sys.stderr)
        return 2
    if settings.auth_enabled is False:
        print("note: KITH_AUTH is off, so the login gate is disabled", file=sys.stderr)
    ok = auth.verify_password(settings.password_hash, getpass.getpass("Password: "))
    print("OK — the configured hash matches" if ok else "MISMATCH")
    return 0 if ok else 1


def cmd_show(_: argparse.Namespace) -> int:
    """Print the effective settings. Never prints the password hash."""
    settings = config.Settings.from_env()
    for name in (
        "data_dir",
        "db_path",
        "host",
        "port",
        "auth_enabled",
        "session_days",
        "secure_cookies",
        "max_upload_mb",
        "display_tz",
        "log_level",
    ):
        print(f"{name:22} {getattr(settings, name)}")
    print(f"{'password_hash':22} {'set' if settings.password_hash else 'NOT SET'}")
    print(
        f"{'session_secret':22} "
        f"{'generated at boot (sessions will not survive a restart)' if settings.session_secret_generated else 'configured'}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hash-password", help="prompt for a password and print its scrypt hash")
    sub.add_parser("check-password", help="verify a password against the configured hash")
    sub.add_parser("show", help="print the effective configuration (never the hash)")
    args = parser.parse_args(argv)
    return {
        "hash-password": cmd_hash_password,
        "check-password": cmd_check_password,
        "show": cmd_show,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
