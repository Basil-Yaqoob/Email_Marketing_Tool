"""RFC 8058 one-click unsubscribe header generation with signed tokens.

One-click unsubscribe requires BOTH headers:
- List-Unsubscribe: <https://host/u/{token}>, <mailto:unsub@domain?subject={token}>
- List-Unsubscribe-Post: List-Unsubscribe=One-Click

Tokens are signed so they cannot be tampered with. A double POST is idempotent.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime


class UnsubscribeToken:
    """A signed, single-purpose unsubscribe token."""

    def __init__(self, send_id: str, created_at: datetime | None = None):
        """Create a new unsubscribe token.

        Args:
            send_id: The ID of the Send row this unsubscribe corresponds to
            created_at: When the token was created (defaults to now)
        """
        self.send_id = send_id
        self.created_at = created_at or datetime.now(UTC)

    def sign(self, secret_key: str) -> str:
        """Generate the signed token string.

        Returns:
            A token in format: {send_id}:{timestamp}:{signature}
        """
        timestamp = str(int(self.created_at.timestamp()))
        message = f"{self.send_id}:{timestamp}"
        signature = hmac.new(
            secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{message}:{signature}"

    @classmethod
    def verify(cls, token: str, secret_key: str) -> UnsubscribeToken | None:
        """Verify a token and return the UnsubscribeToken if valid.

        Returns None if the token is invalid or tampered with.
        """
        parts = token.split(":")
        if len(parts) != 3:
            return None

        send_id, timestamp_str, signature = parts

        try:
            timestamp = int(timestamp_str)
            created_at = datetime.fromtimestamp(timestamp, UTC)
        except (ValueError, OSError):
            return None

        # Verify signature
        message = f"{send_id}:{timestamp_str}"
        expected_signature = hmac.new(
            secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_signature):
            return None

        return cls(send_id, created_at)


def generate_unsubscribe_headers(
    send_id: str,
    mailbox_email: str,
    unsubscribe_host: str,
    secret_key: str,
) -> dict[str, str]:
    """Generate RFC 8058 one-click unsubscribe headers.

    Args:
        send_id: The ID of the Send row
        mailbox_email: The mailbox's email address (for mailto fallback)
        unsubscribe_host: The hostname where the unsubscribe endpoint lives
                         (e.g. "https://example.com")
        secret_key: The secret key to sign tokens with

    Returns:
        Dictionary with "List-Unsubscribe" and "List-Unsubscribe-Post" headers
    """
    token = UnsubscribeToken(send_id)
    signed_token = token.sign(secret_key)

    # Construct the one-click unsubscribe URL
    unsubscribe_url = f"{unsubscribe_host}/u/{signed_token}"

    # Construct the mailto fallback
    mailto = f"mailto:unsub@example.com?subject={signed_token}"

    # Both headers are required for one-click to be recognized
    return {
        "List-Unsubscribe": f"<{unsubscribe_url}>, <{mailto}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


__all__ = ["UnsubscribeToken", "generate_unsubscribe_headers"]
