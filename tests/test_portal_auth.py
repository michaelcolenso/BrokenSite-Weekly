from datetime import datetime, timedelta, timezone

from src.portal_auth import generate_portal_token, verify_portal_token


def test_portal_token_roundtrip():
    token = generate_portal_token("test@example.com", "secret", ttl_days=1)
    result = verify_portal_token(token, "secret")
    assert result is not None
    email, expires = result
    assert email == "test@example.com"
    assert expires > datetime.now(timezone.utc)


def test_portal_token_invalid_secret():
    token = generate_portal_token("test@example.com", "secret", ttl_days=1)
    assert verify_portal_token(token, "wrong") is None
