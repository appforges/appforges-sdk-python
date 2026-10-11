"""Verify the existing IAM protocol using original create/rotate secrets.

The signature authenticates raw body bytes, not event headers or freshness.
"""

import hashlib
import hmac
import re
from collections.abc import Sequence


def verify_webhook_signature(body: bytes, signature: str, secrets: Sequence[str]) -> bool:
    """Verify lowercase hex X-Webhook-Signature; support local key rotation."""
    if not isinstance(body, bytes) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return False
    valid = False
    for secret in secrets:
        if not secret:
            continue
        derived = hashlib.sha256(secret.encode()).hexdigest().encode("ascii")
        expected = hmac.new(derived, body, hashlib.sha256).hexdigest()
        valid = hmac.compare_digest(expected, signature) or valid
    return valid
