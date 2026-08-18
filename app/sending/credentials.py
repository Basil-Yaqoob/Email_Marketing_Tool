"""Credential encryption at rest using Fernet.

Credentials are decrypted only at connection time, never logged, never returned
by an API endpoint. Any model holding credentials has __repr__ overridden to
redact them.
"""

from __future__ import annotations

from cryptography.fernet import Fernet


class CredentialVault:
    """Encrypt and decrypt mailbox credentials using Fernet (AES-128-CBC).

    The key is derived from settings.secret_key, which must be 32 bytes of
    random data suitable for use with Fernet. If your secret_key is a string,
    you must base64-encode it before passing to this class.
    """

    def __init__(self, secret_key: bytes) -> None:
        """Initialize with a base64-encoded key suitable for Fernet.

        Args:
            secret_key: 44-byte base64-encoded string (32 bytes + padding).
                        Can be generated with: Fernet.generate_key()
        """
        self._cipher = Fernet(secret_key)

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt plaintext to ciphertext.

        Args:
            plaintext: The credential (password, token, etc.) as a string.

        Returns:
            Encrypted bytes suitable for storage in a LONGBINARY column.
        """
        return self._cipher.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        """Decrypt ciphertext back to plaintext.

        Args:
            ciphertext: Encrypted bytes from the database.

        Returns:
            The original plaintext as a string.

        Raises:
            cryptography.fernet.InvalidToken: If ciphertext is corrupted or
                                              was encrypted with a different key.
        """
        return self._cipher.decrypt(ciphertext).decode("utf-8")


__all__ = ["CredentialVault"]
