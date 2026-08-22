"""Repository layer. Business logic imports from here, never a model or
raw SQL directly — see CLAUDE.md §6.

One repository per table. Every write path takes a Pydantic boundary model
rather than a bare dict or a SQLAlchemy instance, so a caller cannot smuggle
an unvalidated field across a layer.
"""

from app.db.repositories.campaign_repository import (
    CampaignCreate,
    CampaignRepository,
    FunnelCounts,
)
from app.db.repositories.company_repository import (
    CompanyCreate,
    CompanyRepository,
    UpsertResult,
)
from app.db.repositories.domain_pattern_repository import DomainPatternRepository
from app.db.repositories.email_repository import (
    EmailAddressCreate,
    EmailAddressRepository,
)
from app.db.repositories.fact_repository import FactCreate, FactRepository
from app.db.repositories.hook_repository import HookRepository
from app.db.repositories.mailbox_repository import (
    MailboxCreate,
    MailboxCredentials,
    MailboxRepository,
    MailboxView,
)
from app.db.repositories.message_repository import MessageRepository
from app.db.repositories.person_repository import PersonCreate, PersonRepository
from app.db.repositories.reply_repository import ReplyCreate, ReplyRepository
from app.db.repositories.resolver_run_repository import (
    ResolverRunCreate,
    ResolverRunRepository,
)
from app.db.repositories.seed_test_repository import SeedTestCreate, SeedTestRepository
from app.db.repositories.send_repository import SendCreate, SendRepository
from app.db.repositories.suppression_repository import (
    SuppressionCreate,
    SuppressionRepository,
)

__all__ = [
    "CampaignCreate",
    "CampaignRepository",
    "CompanyCreate",
    "CompanyRepository",
    "DomainPatternRepository",
    "EmailAddressCreate",
    "EmailAddressRepository",
    "FactCreate",
    "FactRepository",
    "FunnelCounts",
    "HookRepository",
    "MailboxCreate",
    "MailboxCredentials",
    "MailboxRepository",
    "MailboxView",
    "MessageRepository",
    "PersonCreate",
    "PersonRepository",
    "ReplyCreate",
    "ReplyRepository",
    "ResolverRunCreate",
    "ResolverRunRepository",
    "SeedTestCreate",
    "SeedTestRepository",
    "SendCreate",
    "SendRepository",
    "SuppressionCreate",
    "SuppressionRepository",
    "UpsertResult",
]
