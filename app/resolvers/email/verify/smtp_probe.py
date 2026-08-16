"""Rung 3 — the owned SMTP RCPT TO prober.

This is the rung that replaces the metered vendor whose credits ran out
and took the whole prototype down with them. It needs outbound port 25,
which most clouds block: GCP permanently and with no exceptions, Azure on
subscriptions created after Nov 2017, AWS until you file a request.
Hetzner, OVH and Contabo unblock on request — one of those, about $5/mo,
is the prober host. See doc/PROBER-DEPLOYMENT.md.

The conversation is deliberately incomplete: connect, EHLO, MAIL FROM,
RCPT TO, read the code, QUIT. **DATA is never sent** — we ask whether the
server would accept mail and then hang up, which is the verification
primitive, not a delivery.

Two things that look like details and are not:

*Catch-all detection runs first.* A domain that accepts everything answers
250 to any RCPT TO, so a 250 there means nothing. We ask about a random
address we know cannot exist; if that is accepted, every address at the
domain is CATCH_ALL. The result is cached per domain because it rarely
changes, and because re-deriving it per address would double the probe
volume against servers that already dislike us.

*Rate limiting is per destination MX host, not per target domain.* Tens of
thousands of domains share Google's MX. Limiting per domain would let a
batch hammer one server from one IP, which is how a prober IP gets
blocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from typing import Protocol

from app.core.errors import VerificationError
from app.core.logging import get_logger
from app.db.models.enums import VerifyStatus

log = get_logger(__name__)

SMTP_PORT = 25
DEFAULT_TIMEOUT = 10.0

# 250/251 accept. 550/551/553/554 are unambiguous rejections. 4xx is a
# transient refusal (greylisting, load shedding) and is explicitly NOT a
# verdict -- treating it as INVALID is how a greylisted domain's entire
# staff gets marked undeliverable.
ACCEPT_CODES = frozenset({250, 251})
REJECT_CODES = frozenset({550, 551, 553, 554})


@dataclass(frozen=True, slots=True)
class SMTPReply:
    code: int
    message: str


class SMTPTransport(Protocol):
    """One SMTP conversation, returning a reply per recipient.

    Recipients are asked for within a single connection so catch-all
    detection and the real check share one handshake rather than two.
    """

    async def probe_recipients(
        self,
        host: str,
        *,
        ehlo_hostname: str,
        mail_from: str,
        recipients: Sequence[str],
    ) -> list[SMTPReply]: ...


class HostRateLimiter(Protocol):
    def acquire(self, host: str) -> AbstractAsyncContextManager[None]: ...


class AsyncSMTPTransport:
    """Real SMTP over asyncio streams.

    Written against raw streams rather than a client library because the
    conversation must stop short of DATA and we want the exact reply codes
    with no reconnection or retry logic underneath us. Multi-line replies
    ("250-PIPELINING" then "250 SIZE ...") are parsed explicitly.
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, port: int = SMTP_PORT) -> None:
        self._timeout = timeout
        self._port = port

    async def probe_recipients(
        self,
        host: str,
        *,
        ehlo_hostname: str,
        mail_from: str,
        recipients: Sequence[str],
    ) -> list[SMTPReply]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, self._port), timeout=self._timeout
            )
        except (OSError, TimeoutError) as exc:
            raise VerificationError(f"could not connect to {host}:{self._port}: {exc}") from exc

        try:
            await self._read_reply(reader)  # greeting
            await self._command(reader, writer, f"EHLO {ehlo_hostname}")
            await self._command(reader, writer, f"MAIL FROM:<{mail_from}>")

            replies: list[SMTPReply] = []
            for recipient in recipients:
                replies.append(await self._command(reader, writer, f"RCPT TO:<{recipient}>"))

            # Deliberately never DATA. Say goodbye politely; a dropped
            # connection looks like abuse to the far end.
            writer.write(b"QUIT\r\n")
            await writer.drain()
            return replies
        except (OSError, TimeoutError) as exc:
            raise VerificationError(f"SMTP conversation with {host} failed: {exc}") from exc
        finally:
            writer.close()
            # Close-time races are not our problem: the verdict is already
            # read, and the far end hanging up first is normal after QUIT.
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _command(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, line: str
    ) -> SMTPReply:
        writer.write(f"{line}\r\n".encode())
        await writer.drain()
        return await self._read_reply(reader)

    async def _read_reply(self, reader: asyncio.StreamReader) -> SMTPReply:
        """Reads one complete reply, following continuation lines.

        SMTP marks continuations with a hyphen in the 4th column
        ("250-..."), and the final line with a space ("250 ...").
        """
        lines: list[str] = []
        while True:
            raw = await asyncio.wait_for(reader.readline(), timeout=self._timeout)
            if not raw:
                raise VerificationError("server closed the connection mid-reply")
            decoded = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            lines.append(decoded)
            if len(decoded) < 4 or decoded[3] != "-":
                break

        final = lines[-1]
        try:
            code = int(final[:3])
        except ValueError as exc:
            raise VerificationError(f"unparseable SMTP reply: {final!r}") from exc
        return SMTPReply(code=code, message=" ".join(lines))


class SMTPProber:
    def __init__(
        self,
        *,
        transport: SMTPTransport,
        ehlo_hostname: str,
        mail_from: str,
        limiter: HostRateLimiter | None = None,
        max_retries: int = 1,
    ) -> None:
        if not ehlo_hostname or "." not in ehlo_hostname:
            raise ValueError(
                "ehlo_hostname must be a real FQDN with a matching PTR record — "
                "servers reject the conversation before RCPT TO otherwise"
            )
        if not mail_from or "@" not in mail_from:
            # Never MAIL FROM:<> — many servers treat the null sender
            # specially and answer misleadingly.
            raise ValueError("mail_from must be a real address on the probe domain, not empty")

        self._transport = transport
        self._ehlo_hostname = ehlo_hostname
        self._mail_from = mail_from
        self._limiter = limiter
        self._max_retries = max_retries
        self._catch_all: dict[str, bool] = {}

    def catch_all_status(self, domain: str) -> bool | None:
        """Cached catch-all verdict for a domain, or None if unprobed."""
        return self._catch_all.get(domain.lower())

    async def probe(self, address: str, mx_hosts: Sequence[str]) -> VerifyStatus:
        if not mx_hosts:
            raise VerificationError(f"cannot probe {address} without an MX host")

        domain = address.rpartition("@")[2].lower()
        host = mx_hosts[0]

        known_catch_all = self._catch_all.get(domain)
        if known_catch_all is True:
            return VerifyStatus.CATCH_ALL

        # Only ask the catch-all question when we don't already know the
        # answer -- that is what makes the second address at a domain cost
        # one RCPT instead of two.
        probe_catch_all = known_catch_all is None
        recipients = [f"{uuid.uuid4().hex}@{domain}", address] if probe_catch_all else [address]

        replies = await self._converse(host, recipients)
        if len(replies) != len(recipients):
            raise VerificationError(
                f"{host} returned {len(replies)} replies for {len(recipients)} recipients"
            )

        if probe_catch_all:
            is_catch_all = replies[0].code in ACCEPT_CODES
            self._catch_all[domain] = is_catch_all
            if is_catch_all:
                log.info("verify.catch_all_detected", domain=domain, mx_host=host)
                return VerifyStatus.CATCH_ALL

        return self._interpret(replies[-1])

    async def _converse(self, host: str, recipients: Sequence[str]) -> list[SMTPReply]:
        """One conversation, retried on a transient (4xx) reply for the
        final recipient — greylisting answers 4xx first and succeeds on a
        later attempt.
        """
        attempts = self._max_retries + 1
        replies: list[SMTPReply] = []
        for attempt in range(1, attempts + 1):
            guard = self._limiter.acquire(host) if self._limiter is not None else nullcontext()
            async with guard:
                replies = await self._transport.probe_recipients(
                    host,
                    ehlo_hostname=self._ehlo_hostname,
                    mail_from=self._mail_from,
                    recipients=recipients,
                )
            if not replies:
                raise VerificationError(f"{host} returned no replies")
            if not self._is_transient(replies[-1]) or attempt == attempts:
                return replies
            log.info("verify.greylisted_retry", mx_host=host, attempt=attempt)
        return replies

    @staticmethod
    def _is_transient(reply: SMTPReply) -> bool:
        return 400 <= reply.code < 500

    @staticmethod
    def _interpret(reply: SMTPReply) -> VerifyStatus:
        if reply.code in ACCEPT_CODES:
            return VerifyStatus.VALID
        if reply.code in REJECT_CODES:
            return VerifyStatus.INVALID
        if 400 <= reply.code < 500:
            # Still transient after retries. Not a verdict, and emphatically
            # not an error on our side -- the address stays unknown.
            return VerifyStatus.UNKNOWN
        if 500 <= reply.code < 600:
            return VerifyStatus.INVALID
        return VerifyStatus.UNKNOWN
