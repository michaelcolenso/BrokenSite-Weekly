"""
HMAC-based portal token generation and verification.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_portal_token(email: str, secret: str, ttl_days: int = 30) -> str:
    """Generate a token that encodes email and expiry."""
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    payload = f"{email}|{int(expires_at.timestamp())}"
    signature = _sign(payload, secret)
    token = f"{payload}|{signature}"
    return base64.urlsafe_b64encode(token.encode("utf-8")).decode("utf-8")


def verify_portal_token(token: str, secret: str) -> Optional[Tuple[str, datetime]]:
    """Return (email, expires_at) if valid; otherwise None."""
    try:
        decoded = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        parts = decoded.split("|")
        if len(parts) != 3:
            return None
        email, exp_str, signature = parts
        payload = f"{email}|{exp_str}"
        expected = _sign(payload, secret)
        if not hmac.compare_digest(signature, expected):
            return None
        expires_at = datetime.fromtimestamp(int(exp_str), tz=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            return None
        return email, expires_at
    except Exception:
        return None
