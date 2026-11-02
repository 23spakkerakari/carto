"""CSRF synchronizer tokens (spec 14.1).

One random token per session, stored in the session row, returned by ``GET /me`` and required
in the ``X-CSRF-Token`` header of every ``POST``, ``PATCH``, ``PUT`` and ``DELETE``. The cookie
is ``SameSite=Lax`` as well, so a cross-site form post never carries the session; the token is
the second, independent check. Comparison is constant-time.
"""

from __future__ import annotations

import hmac
import secrets
from typing import Final

__all__ = ["CSRF_HEADER", "UNSAFE_METHODS", "csrf_matches", "new_csrf_token"]

CSRF_HEADER: Final = "x-csrf-token"
UNSAFE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TOKEN_BYTES: Final = 32


def new_csrf_token() -> str:
    """32 random bytes, URL-safe base64 without padding (43 characters)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def csrf_matches(expected: str, presented: str | None) -> bool:
    if not presented or not expected:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))
