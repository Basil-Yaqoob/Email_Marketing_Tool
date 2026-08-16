"""Unit tests for the four-rung verification ladder. No real sockets, no
real DNS — the resolver and SMTP transport are both Protocols, injected as
fakes, so "network is mocked" is structural rather than a monkeypatch.

Tests 17, 18 and 21 are why this session exists:

  test_batch_aborts_past_error_threshold        — the prototype's exact bug
  test_batch_aborts_on_zero_valid_with_no_errors — its subtler variant
  test_error_status_never_written_as_a_lead_outcome — the core distinction

If tests are ever cut, not those.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from app.core.errors import BatchAbortedError, PolicyBlockedError, VerificationError
from app.db.models.enums import VerifyStatus
from app.resolvers.email.verify.burner_probe import BurnerProbeConfig, BurnerProber
from app.resolvers.email.verify.models import MailProvider, Rung
from app.resolvers.email.verify.mx import detect_provider, lookup_mx
from app.resolvers.email.verify.orchestrator import verify_batch, verify_one
from app.resolvers.email.verify.smtp_probe import SMTPProber, SMTPReply
from app.resolvers.email.verify.syntax import check_syntax, is_valid_syntax

FIXED_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _now() -> datetime:
    return FIXED_NOW


class FakeResolver:
    """DnsResolver double. `mx` maps domain -> MX hosts; unlisted domains
    have no MX. `fail_for` raises, standing in for a resolution failure.
    """

    def __init__(
        self,
        mx: dict[str, list[str]] | None = None,
        *,
        fail_for: set[str] | None = None,
    ) -> None:
        self._mx = mx or {}
        self._fail_for = fail_for or set()
        self.lookups: list[str] = []

    async def resolve_mx(self, domain: str) -> list[str]:
        self.lookups.append(domain)
        if domain in self._fail_for:
            raise VerificationError(f"MX lookup failed for {domain}")
        return list(self._mx.get(domain, []))


class FakeTransport:
    """SMTPTransport double.

    `codes_for` maps a recipient-matching rule to a reply code:
      - an exact address -> that code
      - "*catch-all*"    -> code for the random catch-all probe address
    Records every conversation so tests can assert on probe volume.
    """

    def __init__(
        self,
        *,
        default_code: int = 550,
        addresses: dict[str, int] | None = None,
        catch_all_code: int = 550,
        raises: bool = False,
        transient_then: int | None = None,
    ) -> None:
        self._default = default_code
        self._addresses = addresses or {}
        self._catch_all_code = catch_all_code
        self._raises = raises
        self._transient_then = transient_then
        self.conversations: list[tuple[str, tuple[str, ...]]] = []

    async def probe_recipients(
        self,
        host: str,
        *,
        ehlo_hostname: str,
        mail_from: str,
        recipients: Sequence[str],
    ) -> list[SMTPReply]:
        if self._raises:
            raise VerificationError(f"could not connect to {host}")

        attempt_index = len(self.conversations)
        self.conversations.append((host, tuple(recipients)))

        replies: list[SMTPReply] = []
        for position, recipient in enumerate(recipients):
            if recipient in self._addresses:
                code = self._addresses[recipient]
            else:
                # The catch-all probe uses a random local part we can't
                # enumerate, so anything unlisted is treated as it.
                code = self._catch_all_code

            # Greylisting applies only to the *target* recipient (always
            # last). Overriding the catch-all probe too would make the
            # retry look like a catch-all domain and mask the real bug
            # this test is trying to catch.
            is_target = position == len(recipients) - 1
            if self._transient_then is not None and is_target:
                code = 451 if attempt_index == 0 else self._transient_then

            replies.append(SMTPReply(code=code, message=f"{code} test"))
        return replies


def _prober(transport: FakeTransport, **kwargs: object) -> SMTPProber:
    return SMTPProber(
        transport=transport,
        ehlo_hostname="prober.example.test",
        mail_from="probe@probe.example.test",
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Rung 1 — syntax, disposable, role
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "not-an-address",
        "@example.com",
        "jane@",
        "jane@@example.com",
        "jane doe@example.com",
        "jane@example",
        ".jane@example.com",
        "jane.@example.com",
        "jane..doe@example.com",
        "jane@-example.com",
        "jane@example..com",
        "",
    ],
)
def test_syntax_rejects_malformed(address: str) -> None:
    assert is_valid_syntax(address) is False
    assert check_syntax(address) == VerifyStatus.INVALID


@pytest.mark.parametrize(
    "address",
    [
        "jane.smith@example.com",
        "jane+tag@example.co.uk",
        "j@example.com",
        "jane_smith@sub.example.com",
        "jane-smith@example-corp.com",
    ],
)
def test_syntax_accepts_well_formed(address: str) -> None:
    assert is_valid_syntax(address) is True


def test_syntax_rejects_overlong_local_part_and_address() -> None:
    assert is_valid_syntax(f"{'a' * 65}@example.com") is False
    assert is_valid_syntax(f"{'a' * 250}@{'b' * 250}.com") is False


def test_disposable_domain_flagged() -> None:
    assert check_syntax("jane@mailinator.com") == VerifyStatus.INVALID
    assert check_syntax("jane@guerrillamail.com") == VerifyStatus.INVALID
    assert check_syntax("jane@realcompany.example") is None


def test_role_prefix_returns_role_status() -> None:
    assert check_syntax("info@example.com") == VerifyStatus.ROLE
    assert check_syntax("support@example.com") == VerifyStatus.ROLE
    assert check_syntax("jane.smith@example.com") is None


async def test_syntax_settles_without_touching_dns() -> None:
    resolver = FakeResolver()
    result = await verify_one("not-an-address", resolver=resolver, now=_now)

    assert result.status == VerifyStatus.INVALID
    assert result.rung == Rung.SYNTAX
    assert resolver.lookups == [], "rung 1 must settle without a DNS lookup"


# --------------------------------------------------------------------------
# Rung 2 — MX and provider detection
# --------------------------------------------------------------------------


async def test_no_mx_returns_invalid() -> None:
    resolver = FakeResolver({})
    result = await verify_one("jane@nomx.example", resolver=resolver, now=_now)

    assert result.status == VerifyStatus.INVALID
    assert result.rung == Rung.MX
    assert result.provider == MailProvider.NONE


def test_detects_google_workspace_from_mx() -> None:
    assert detect_provider(["aspmx.l.google.com"]) == MailProvider.GOOGLE
    assert detect_provider(["ALT1.ASPMX.L.GOOGLE.COM"]) == MailProvider.GOOGLE
    assert detect_provider(["aspmx2.googlemail.com"]) == MailProvider.GOOGLE


def test_detects_microsoft_365_from_mx() -> None:
    assert detect_provider(["example-com.mail.protection.outlook.com"]) == MailProvider.MICROSOFT
    assert detect_provider(["EXAMPLE.MAIL.PROTECTION.OUTLOOK.COM"]) == MailProvider.MICROSOFT


def test_detects_other_and_none_providers() -> None:
    assert detect_provider(["mx.somehost.net"]) == MailProvider.OTHER
    assert detect_provider(["mx.pphosted.com"]) == MailProvider.PROOFPOINT
    assert detect_provider([]) == MailProvider.NONE


async def test_lookup_mx_normalises_hosts() -> None:
    resolver = FakeResolver({"example.com": ["ASPMX.L.GOOGLE.COM.", " alt1.aspmx.l.google.com "]})
    mx = await lookup_mx("example.com", resolver)

    assert mx.has_mx is True
    assert mx.hosts == ("aspmx.l.google.com", "alt1.aspmx.l.google.com")
    assert mx.provider == MailProvider.GOOGLE


async def test_google_hosted_returns_unknown_without_probing() -> None:
    """The honest limit (CLAUDE.md §10), and it saves a pointless probe:
    Google answers 250 for mailboxes that do not exist.
    """
    resolver = FakeResolver({"example.com": ["aspmx.l.google.com"]})
    transport = FakeTransport(addresses={"jane@example.com": 250})
    prober = _prober(transport)

    result = await verify_one("jane@example.com", resolver=resolver, prober=prober, now=_now)

    assert result.status == VerifyStatus.UNKNOWN
    assert result.rung == Rung.MX
    assert result.provider == MailProvider.GOOGLE
    assert transport.conversations == [], "must not probe a Google-hosted domain"


async def test_microsoft_hosted_returns_unknown_without_probing() -> None:
    resolver = FakeResolver({"example.com": ["example-com.mail.protection.outlook.com"]})
    transport = FakeTransport(addresses={"jane@example.com": 250})
    prober = _prober(transport)

    result = await verify_one("jane@example.com", resolver=resolver, prober=prober, now=_now)

    assert result.status == VerifyStatus.UNKNOWN
    assert result.provider == MailProvider.MICROSOFT
    assert transport.conversations == []


# --------------------------------------------------------------------------
# Rung 3 — SMTP probe
# --------------------------------------------------------------------------


async def test_rcpt_250_returns_valid() -> None:
    transport = FakeTransport(addresses={"jane@example.com": 250}, catch_all_code=550)
    prober = _prober(transport)

    assert await prober.probe("jane@example.com", ["mx.example.com"]) == VerifyStatus.VALID


@pytest.mark.parametrize("code", [550, 551, 553, 554])
async def test_rcpt_550_returns_invalid(code: int) -> None:
    transport = FakeTransport(addresses={"jane@example.com": code}, catch_all_code=550)
    prober = _prober(transport)

    assert await prober.probe("jane@example.com", ["mx.example.com"]) == VerifyStatus.INVALID


async def test_rcpt_4xx_is_retried_not_a_verdict() -> None:
    """Greylisting answers 4xx first and accepts on retry. Treating the
    first answer as INVALID would mark a whole greylisted domain
    undeliverable.
    """
    transport = FakeTransport(
        addresses={"jane@example.com": 250}, catch_all_code=550, transient_then=250
    )
    prober = _prober(transport, max_retries=1)

    status = await prober.probe("jane@example.com", ["mx.example.com"])

    assert status == VerifyStatus.VALID
    assert len(transport.conversations) == 2, "a 4xx must trigger exactly one retry"


async def test_persistent_4xx_is_unknown_not_invalid() -> None:
    transport = FakeTransport(
        addresses={"jane@example.com": 451}, catch_all_code=451, transient_then=451
    )
    prober = _prober(transport, max_retries=1)

    assert await prober.probe("jane@example.com", ["mx.example.com"]) == VerifyStatus.UNKNOWN


async def test_catch_all_detected_via_random_address() -> None:
    transport = FakeTransport(addresses={"jane@example.com": 250}, catch_all_code=250)
    prober = _prober(transport)

    status = await prober.probe("jane@example.com", ["mx.example.com"])

    assert status == VerifyStatus.CATCH_ALL
    _, recipients = transport.conversations[0]
    assert len(recipients) == 2
    assert recipients[1] == "jane@example.com"
    assert recipients[0] != "jane@example.com"
    assert recipients[0].endswith("@example.com")


async def test_catch_all_domain_returns_catch_all_for_all_addresses() -> None:
    transport = FakeTransport(
        addresses={"jane@example.com": 250, "bob@example.com": 250}, catch_all_code=250
    )
    prober = _prober(transport)

    assert await prober.probe("jane@example.com", ["mx.example.com"]) == VerifyStatus.CATCH_ALL
    assert await prober.probe("bob@example.com", ["mx.example.com"]) == VerifyStatus.CATCH_ALL


async def test_catch_all_status_cached_per_domain() -> None:
    """Second address at a known-good domain costs one RCPT, not two."""
    transport = FakeTransport(
        addresses={"jane@example.com": 250, "bob@example.com": 250}, catch_all_code=550
    )
    prober = _prober(transport)

    await prober.probe("jane@example.com", ["mx.example.com"])
    assert prober.catch_all_status("example.com") is False
    first_recipients = transport.conversations[0][1]
    assert len(first_recipients) == 2, "first address probes catch-all too"

    await prober.probe("bob@example.com", ["mx.example.com"])
    second_recipients = transport.conversations[1][1]
    assert second_recipients == ("bob@example.com",), "cached catch-all must skip the extra probe"


async def test_catch_all_cache_short_circuits_entirely() -> None:
    transport = FakeTransport(addresses={"jane@example.com": 250}, catch_all_code=250)
    prober = _prober(transport)

    await prober.probe("jane@example.com", ["mx.example.com"])
    assert prober.catch_all_status("example.com") is True

    await prober.probe("bob@example.com", ["mx.example.com"])
    assert len(transport.conversations) == 1, "a known catch-all domain needs no further probes"


async def test_rate_limited_per_mx_host_not_per_domain() -> None:
    """Tens of thousands of domains share one MX. Limiting per domain
    would let a batch hammer a single server from a single IP.
    """
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    acquired: list[str] = []

    class RecordingLimiter:
        @asynccontextmanager
        async def acquire(self, host: str) -> AsyncIterator[None]:
            acquired.append(host)
            yield

    transport = FakeTransport(
        addresses={"jane@alpha.example": 250, "bob@beta.example": 250}, catch_all_code=550
    )
    prober = _prober(transport, limiter=RecordingLimiter())

    await prober.probe("jane@alpha.example", ["shared-mx.example.net"])
    await prober.probe("bob@beta.example", ["shared-mx.example.net"])

    assert acquired == ["shared-mx.example.net", "shared-mx.example.net"]


async def test_probe_without_mx_hosts_raises() -> None:
    prober = _prober(FakeTransport())
    with pytest.raises(VerificationError, match="without an MX host"):
        await prober.probe("jane@example.com", [])


def test_prober_rejects_unusable_identity() -> None:
    """A bare hostname or a null sender gets the conversation rejected
    before RCPT TO, so both are refused at construction.
    """
    with pytest.raises(ValueError, match="FQDN"):
        SMTPProber(transport=FakeTransport(), ehlo_hostname="localhost", mail_from="p@example.com")
    with pytest.raises(ValueError, match="mail_from"):
        SMTPProber(transport=FakeTransport(), ehlo_hostname="a.example.com", mail_from="")


async def test_prober_unavailable_degrades_to_unknown() -> None:
    """No prober deployed must not crash the pipeline."""
    resolver = FakeResolver({"example.com": ["mx.example.com"]})

    result = await verify_one("jane@example.com", resolver=resolver, prober=None, now=_now)

    assert result.status == VerifyStatus.UNKNOWN
    assert result.rung == Rung.MX
    assert result.detail == "no SMTP prober configured"


async def test_prober_connection_failure_raises_not_returns() -> None:
    """A broken prober is OUR failure and must propagate, not become a
    lead's answer.
    """
    resolver = FakeResolver({"example.com": ["mx.example.com"]})
    prober = _prober(FakeTransport(raises=True))

    with pytest.raises(VerificationError):
        await verify_one("jane@example.com", resolver=resolver, prober=prober, now=_now)


# --------------------------------------------------------------------------
# The guards — why this session exists
# --------------------------------------------------------------------------


async def test_error_status_never_written_as_a_lead_outcome() -> None:
    """The core distinction, enforced structurally rather than by
    convention: VerifyStatus has no ERROR member for a failure to be
    written as, and a failing check raises instead of returning.
    """
    assert not hasattr(VerifyStatus, "ERROR")
    assert "error" not in {status.value for status in VerifyStatus}

    resolver = FakeResolver(fail_for={"broken.example"})
    with pytest.raises(VerificationError):
        await verify_one("jane@broken.example", resolver=resolver, now=_now)

    # And across a batch, errors are counted separately from results —
    # never appearing among them.
    resolver = FakeResolver({"ok.example": ["mx.ok.example"]}, fail_for={"broken.example"})
    transport = FakeTransport(addresses={"jane@ok.example": 250}, catch_all_code=550)
    batch = await verify_batch(
        ["jane@ok.example", "bob@broken.example"],
        resolver=resolver,
        prober=_prober(transport),
        min_checked=100,
        now=_now,
    )

    assert batch.error_count == 1
    assert [r.address for r in batch.results] == ["jane@ok.example"]


async def test_batch_aborts_past_error_threshold() -> None:
    """The prototype's exact bug: 1,640 consecutive failures recorded as
    results, success summary printed, exit 0.
    """
    resolver = FakeResolver(fail_for={"broken.example"})
    addresses = [f"user{i}@broken.example" for i in range(50)]

    with pytest.raises(BatchAbortedError) as exc_info:
        await verify_batch(addresses, resolver=resolver, min_checked=20, now=_now)

    assert "error rate" in str(exc_info.value)
    assert exc_info.value.error_rate > 0.05


async def test_batch_below_min_checked_does_not_abort_early() -> None:
    """A single failure in a tiny batch is not a systemic signal."""
    resolver = FakeResolver({"ok.example": ["mx.ok.example"]}, fail_for={"broken.example"})
    transport = FakeTransport(addresses={"jane@ok.example": 250}, catch_all_code=550)

    batch = await verify_batch(
        ["jane@ok.example", "bob@broken.example"],
        resolver=resolver,
        prober=_prober(transport),
        min_checked=20,
        now=_now,
    )

    assert batch.error_count == 1
    assert batch.count(VerifyStatus.VALID) == 1


async def test_batch_aborts_on_zero_valid_with_no_errors() -> None:
    """The subtler variant: every result looked legitimate, the aggregate
    is still worthless.
    """
    resolver = FakeResolver({})  # every domain has no MX -> all INVALID
    addresses = [f"user{i}@nomx.example" for i in range(25)]

    with pytest.raises(BatchAbortedError) as exc_info:
        await verify_batch(addresses, resolver=resolver, min_checked=20, now=_now)

    assert "zero VALID" in str(exc_info.value)


async def test_unknown_results_do_not_trip_the_error_guard() -> None:
    """UNKNOWN is a legitimate permanent answer, not a failure."""
    resolver = FakeResolver({f"d{i}.example": ["aspmx.l.google.com"] for i in range(25)})
    addresses = [f"user{i}@d{i}.example" for i in range(25)]

    # One VALID keeps the zero-yield guard quiet so this test isolates the
    # error guard, which is the thing under test.
    resolver._mx["ok.example"] = ["mx.ok.example"]
    addresses.append("jane@ok.example")
    transport = FakeTransport(addresses={"jane@ok.example": 250}, catch_all_code=550)

    batch = await verify_batch(
        addresses, resolver=resolver, prober=_prober(transport), min_checked=20, now=_now
    )

    assert batch.error_count == 0
    assert batch.count(VerifyStatus.UNKNOWN) == 25
    assert batch.error_rate == 0.0


async def test_catch_all_results_do_not_trip_the_error_guard() -> None:
    resolver = FakeResolver({f"d{i}.example": ["mx.shared.example"] for i in range(25)})
    resolver._mx["ok.example"] = ["mx.ok.example"]
    addresses = [f"user{i}@d{i}.example" for i in range(25)] + ["jane@ok.example"]

    transport = FakeTransport(addresses={"jane@ok.example": 250}, catch_all_code=250)
    prober = _prober(transport)
    # ok.example must not look like a catch-all, so give it its own answer.
    prober._catch_all["ok.example"] = False

    batch = await verify_batch(
        addresses, resolver=resolver, prober=prober, min_checked=20, now=_now
    )

    assert batch.error_count == 0
    assert batch.count(VerifyStatus.CATCH_ALL) == 25
    assert batch.error_rate == 0.0


async def test_valid_result_feeds_pattern_learning() -> None:
    """Session 09 integration: a confirmed address is exactly the signal
    that turns nine future guesses at that domain into one.
    """
    resolver = FakeResolver({"northgate.example": ["mx.northgate.example"]})
    transport = FakeTransport(addresses={"sarah.chen@northgate.example": 250}, catch_all_code=550)

    batch = await verify_batch(
        ["sarah.chen@northgate.example"],
        resolver=resolver,
        prober=_prober(transport),
        names={"sarah.chen@northgate.example": "Sarah Chen"},
        min_checked=100,
        now=_now,
    )

    assert len(batch.learned) == 1
    learned = batch.learned[0]
    assert learned.pattern == "{first}.{last}"
    assert learned.domain == "northgate.example"
    assert learned.weight == 2, (
        "a verification is stronger than a website mention, weaker than a reply"
    )


async def test_learning_skipped_without_a_name() -> None:
    resolver = FakeResolver({"northgate.example": ["mx.northgate.example"]})
    transport = FakeTransport(addresses={"sarah.chen@northgate.example": 250}, catch_all_code=550)

    batch = await verify_batch(
        ["sarah.chen@northgate.example"],
        resolver=resolver,
        prober=_prober(transport),
        min_checked=100,
        now=_now,
    )

    assert batch.learned == ()
    assert batch.count(VerifyStatus.VALID) == 1


# --------------------------------------------------------------------------
# Rung 4 — burner probe
# --------------------------------------------------------------------------


def test_burner_probe_disabled_by_default() -> None:
    config = BurnerProbeConfig()
    assert config.enabled is False
    assert BurnerProber(config).enabled is False


def test_burner_probe_refuses_a_sending_domain() -> None:
    """Bounces must land on a disposable reputation, never on the domains
    that carry real campaigns. Misconfiguration raises at construction.
    """
    config = BurnerProbeConfig(
        enabled=True,
        probe_domain="Outreach.example",
        sending_domains=frozenset({"outreach.example", "mail.example"}),
    )
    with pytest.raises(PolicyBlockedError, match="sending domain"):
        BurnerProber(config)


def test_burner_probe_requires_a_probe_domain_when_enabled() -> None:
    with pytest.raises(PolicyBlockedError, match="no probe_domain"):
        BurnerProber(BurnerProbeConfig(enabled=True))


# --------------------------------------------------------------------------
# The real adapters — exercised without a socket or a DNS query.
#
# AsyncSMTPTransport's multi-line reply parser and DnsPythonResolver's
# exception mapping are both hand-written, and both are the kind of code
# that fails quietly. An asyncio.StreamReader can be fed bytes directly,
# and dnspython's resolver can be swapped for a fake, so neither needs
# real network to test.
# --------------------------------------------------------------------------


async def _reply_from(raw: bytes) -> SMTPReply:
    import asyncio

    from app.resolvers.email.verify.smtp_probe import AsyncSMTPTransport

    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    return await AsyncSMTPTransport()._read_reply(reader)


async def test_smtp_reply_parses_single_line() -> None:
    reply = await _reply_from(b"250 OK\r\n")
    assert reply.code == 250


async def test_smtp_reply_follows_continuation_lines() -> None:
    """A hyphen in the 4th column means "more to come". Stopping at the
    first line would leave the rest in the buffer and desynchronise every
    subsequent command in the conversation.
    """
    reply = await _reply_from(
        b"250-mx.example.com greets you\r\n250-PIPELINING\r\n250 SIZE 10240000\r\n"
    )
    assert reply.code == 250
    assert "PIPELINING" in reply.message
    assert "SIZE" in reply.message


async def test_smtp_reply_raises_when_connection_closes_mid_reply() -> None:
    with pytest.raises(VerificationError, match="closed the connection"):
        await _reply_from(b"")


async def test_smtp_reply_raises_on_unparseable_code() -> None:
    # No hyphen in the 4th column, so this terminates the reply -- it just
    # doesn't start with a numeric code. ("not-a-code" would be read as a
    # *continuation* line, which is correct and a different case.)
    with pytest.raises(VerificationError, match="unparseable"):
        await _reply_from(b"abc def\r\n")


async def test_smtp_reply_treats_hyphen_in_column_four_as_continuation() -> None:
    """Guards the parser's one genuinely subtle rule against being
    "simplified" into a startswith check later.
    """
    reply = await _reply_from(b"250-first\r\n250 second\r\n")
    assert reply.code == 250
    assert "first" in reply.message
    assert "second" in reply.message


async def test_smtp_conversation_never_sends_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stated invariant of the whole prober: we ask whether the server
    would accept mail, then hang up. Sending DATA would be a delivery.
    """
    import asyncio

    from app.resolvers.email.verify.smtp_probe import AsyncSMTPTransport

    class FakeWriter:
        def __init__(self) -> None:
            self.written: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.written.append(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    reader = asyncio.StreamReader()
    reader.feed_data(
        b"220 mx.example.com ESMTP\r\n"  # greeting
        b"250-mx.example.com\r\n250 SIZE 10240000\r\n"  # EHLO
        b"250 OK\r\n"  # MAIL FROM
        b"250 Accepted\r\n"  # RCPT TO
    )
    reader.feed_eof()
    writer = FakeWriter()

    async def fake_open_connection(host: str, port: int) -> tuple[object, object]:
        assert port == 25
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    replies = await AsyncSMTPTransport().probe_recipients(
        "mx.example.com",
        ehlo_hostname="prober.example.test",
        mail_from="probe@probe.example.test",
        recipients=["jane@example.com"],
    )

    assert [r.code for r in replies] == [250]

    sent = b"".join(writer.written).decode()
    assert "EHLO prober.example.test" in sent
    assert "MAIL FROM:<probe@probe.example.test>" in sent
    assert "RCPT TO:<jane@example.com>" in sent
    assert "QUIT" in sent
    assert "DATA" not in sent, "the prober must never send DATA — that would be a delivery"


async def test_smtp_transport_connection_failure_raises_verification_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.resolvers.email.verify.smtp_probe import AsyncSMTPTransport

    async def refuse(host: str, port: int) -> tuple[object, object]:
        raise OSError("connection refused")

    monkeypatch.setattr(asyncio, "open_connection", refuse)

    with pytest.raises(VerificationError, match="could not connect"):
        await AsyncSMTPTransport().probe_recipients(
            "mx.example.com",
            ehlo_hostname="prober.example.test",
            mail_from="probe@probe.example.test",
            recipients=["jane@example.com"],
        )


class _FakeDnsAnswer:
    def __init__(self, hosts: list[str]) -> None:
        self._hosts = hosts

    def __iter__(self):  # type: ignore[no-untyped-def]
        for host in self._hosts:
            yield type("MX", (), {"exchange": host})()


def _patch_dns(monkeypatch: pytest.MonkeyPatch, *, result: object) -> None:
    import dns.asyncresolver

    class FakeResolverClass:
        def __init__(self) -> None:
            self.timeout = 0.0
            self.lifetime = 0.0

        async def resolve(self, domain: str, rdtype: str) -> object:
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(dns.asyncresolver, "Resolver", FakeResolverClass)


async def test_dns_resolver_returns_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.resolvers.email.verify.mx import DnsPythonResolver

    _patch_dns(
        monkeypatch, result=_FakeDnsAnswer(["ASPMX.L.GOOGLE.COM.", "alt1.aspmx.l.google.com"])
    )

    hosts = await DnsPythonResolver().resolve_mx("example.com")
    assert hosts == ["aspmx.l.google.com", "alt1.aspmx.l.google.com"]


@pytest.mark.parametrize("exception_name", ["NXDOMAIN", "NoAnswer"])
async def test_dns_nxdomain_and_no_answer_are_answers_not_errors(
    monkeypatch: pytest.MonkeyPatch, exception_name: str
) -> None:
    """A domain that does not exist, or has no MX, accepts no mail. That
    is an *answer* (leading to INVALID), not a system failure — treating
    it as an error would trip the batch guard on perfectly normal data.
    """
    import dns.resolver

    from app.resolvers.email.verify.mx import DnsPythonResolver

    _patch_dns(monkeypatch, result=getattr(dns.resolver, exception_name)())

    assert await DnsPythonResolver().resolve_mx("nope.example") == []


async def test_dns_resolution_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout or servfail is OUR side failing, and must propagate so
    the batch guard can count it.
    """
    import dns.exception

    from app.resolvers.email.verify.mx import DnsPythonResolver

    _patch_dns(monkeypatch, result=dns.exception.Timeout())

    with pytest.raises(VerificationError, match="MX lookup failed"):
        await DnsPythonResolver().resolve_mx("slow.example")


def test_burner_probe_accepts_a_genuinely_disposable_domain() -> None:
    prober = BurnerProber(
        BurnerProbeConfig(
            enabled=True,
            probe_domain="burner.example",
            sending_domains=frozenset({"outreach.example"}),
        )
    )
    assert prober.enabled is True
    assert prober.probe_domain == "burner.example"
