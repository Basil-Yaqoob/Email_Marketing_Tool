"""Import every model here so Base.metadata is complete before Alembic
autogenerate (or Base.metadata.create_all in tests) runs. A model defined
but not imported anywhere is invisible to migrations — this is the one
place that must import all of them.
"""

from app.db.base import Base
from app.db.models.campaign import Campaign
from app.db.models.company import Company
from app.db.models.domain_pattern import DomainPattern
from app.db.models.email import EmailAddress
from app.db.models.enums import (
    CampaignStatus,
    MailboxProvider,
    MessageStatus,
    ReplyClassification,
    ResolverOutcome,
    RoleClass,
    SeedPlacement,
    SendStatus,
    SubjectType,
    VerifyStatus,
)
from app.db.models.fact import Fact
from app.db.models.hook import Hook
from app.db.models.mailbox import Mailbox
from app.db.models.message import Message
from app.db.models.person import Person
from app.db.models.reply import Reply
from app.db.models.resolver_run import ResolverRun
from app.db.models.seed_test import SeedTest
from app.db.models.send import Send
from app.db.models.suppression import Suppression

__all__ = [
    "Base",
    "Campaign",
    "CampaignStatus",
    "Company",
    "DomainPattern",
    "EmailAddress",
    "Fact",
    "Hook",
    "Mailbox",
    "MailboxProvider",
    "Message",
    "MessageStatus",
    "Person",
    "Reply",
    "ReplyClassification",
    "ResolverOutcome",
    "ResolverRun",
    "RoleClass",
    "SeedPlacement",
    "SeedTest",
    "Send",
    "SendStatus",
    "SubjectType",
    "Suppression",
    "VerifyStatus",
]
