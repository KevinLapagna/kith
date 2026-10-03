"""The login gate: one password, one signed cookie, no user table.

Design decisions worth knowing before you change anything here:

  * **scrypt from the standard library.** bcrypt/argon2 would each add a compiled
    dependency to the image for no benefit at this scale. scrypt is memory-hard and
    already in `hashlib`.
  * **The session cookie is a signed expiry, not a server-side session table.** One user,
    no logout-everywhere, nothing to clean up, and a container restart does not log you out.
  * **Every comparison is `hmac.compare_digest`** — password verification and signature
    checks alike, so neither leaks information through timing.
  * **The password never appears in the environment in plaintext.** `kith.env` holds a
    scrypt *hash*; `kith hash-password` prints one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import quote

COOKIE_NAME = "kith_session"

# 2**14 * 8 * 256 = ~33 MB of memory and ~50 ms per verification: strong, still snappy.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    """Return a self-describing hash: scrypt$n$r$p$salt$key (all params stored, so raising
    the cost later keeps old hashes verifiable)."""
    if not password:
        raise ValueError("password must not be empty")
    salt = os.urandom(16)
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=DKLEN, maxmem=n * r * 256
    )
    return f"scrypt${n}${r}${p}${salt.hex()}${key.hex()}"


def verify_password(stored: str, password: str) -> bool:
    try:
        scheme, n_s, r_s, p_s, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(key_hex)
    except (ValueError, AttributeError):
        return False
    if not password:
        return False
    candidate = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected),
        maxmem=n * r * 256,
    )
    return hmac.compare_digest(expected, candidate)


def _b64(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _unb64(raw: bytes) -> bytes:
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


def issue_token(secret: str, *, days: int) -> str:
    """A signed, expiring token: base64(payload).base64(hmac). No state on the server."""
    payload = {"exp": int(time.time()) + days * 86_400, "nonce": secrets.token_hex(8)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return (body + b"." + _b64(signature)).decode("ascii")


def verify_token(secret: str, token: str | None) -> bool:
    if not token:
        return False
    token_bytes = token.encode("ascii", "ignore")
    body, _, signature = token_bytes.partition(b".")
    if not body or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    try:
        provided = _unb64(signature)
    except (ValueError, base64.binascii.Error):
        return False
    if not hmac.compare_digest(expected, provided):
        return False
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, base64.binascii.Error):
        return False
    return int(payload.get("exp", 0)) > time.time()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def cookie_header(token: str, *, days: int, secure: bool) -> str:
    """The Set-Cookie value. HttpOnly always; Secure only when we are actually on HTTPS,
    because setting Secure on a plain-HTTP loopback host would silently break login."""
    parts = [
        f"{COOKIE_NAME}={quote(token)}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={days * 86_400}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header() -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
