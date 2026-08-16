"""Shared types for the verification ladder.

**On the deliberate absence of an ERROR status.**

The session plan sketches a VerifyStatus enum with an ERROR member and the
rule "ERROR is a system state, never a lead outcome". This module takes
that rule further than the sketch: it reuses Session 02's persisted
VerifyStatus (app/db/models/enums.py), which has *no* ERROR member at all,
and represents system failure only as a raised VerificationError.

That is a stronger guarantee than a naming convention. An ERROR enum
member is a bad state you can still assign — exactly what the prototype
did when it wrote the string "error" into 1,640 leads and exited 0. With
no such member, and a native Postgres enum backing the column, the
database itself rejects the write. The failure has nowhere to go except
up, into the batch guard that counts it.

UNKNOWN and CATCH_ALL are the opposite case: legitimate, permanent
answers, not failures. A large share of B2B addresses are genuinely
unverifiable (see MailProvider below) and sending policy handles them.
They must never trip an error guard.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime

from app.db.models.enums import VerifyStatus

__all__ = [
    "MXResult",
    "MailProvider",
    "Rung",
    "VerificationResult",
    "VerifyStatus",
]


class MailProvider(enum.StrEnum):
    """Who actually runs the mail for a domain.

    This is not trivia — it decides whether rung 3 can work at all.
    Google Workspace and Microsoft 365 accept RCPT TO for addresses that
    do not exist ("accept-then-bounce"), so probing them produces false
    confidence rather than information. Both resolve to UNKNOWN without a
    probe. CLAUDE.md §10 states this limit plainly and this enum is where
    it is enforced.
    """

    GOOGLE = "google"
    MICROSOFT = "microsoft"
    PROOFPOINT = "proofpoint"
    OTHER = "other"
    NONE = "none"  # no MX records at all

    @property
    def blocks_smtp_verification(self) -> bool:
        return self in (MailProvider.GOOGLE, MailProvider.MICROSOFT)


class Rung(enum.IntEnum):
    """Which rung of the ladder settled an address. Recorded so the yield
    dashboard (Session 19) can show *where* verification is being decided
    — a sudden collapse of rung-3 verdicts into rung-2 UNKNOWNs is what a
    dead prober looks like from the outside.
    """

    SYNTAX = 1
    MX = 2
    SMTP = 3
    BURNER = 4


@dataclass(frozen=True, slots=True)
class MXResult:
    has_mx: bool
    hosts: tuple[str, ...]
    provider: MailProvider


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """One address's settled outcome. `status` can only ever hold a real,
    persistable answer — see this module's docstring.
    """

    address: str
    status: VerifyStatus
    rung: Rung
    provider: MailProvider
    checked_at: datetime
    detail: str | None = None
