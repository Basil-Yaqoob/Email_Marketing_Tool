from app.agents.copy.agents import DEFAULT_MAX_REVISIONS, CopywritingAgent
from app.agents.copy.dossier import ANGLE_LABELS, ANGLE_ORDER, build_brief, eligible_angles
from app.agents.copy.offer import OfferConfig, ProductOffer, ProofPoint, SecondaryOffer, Sender
from app.agents.copy.quote_classification import QuoteClass, classify_quote
from app.agents.copy.types import CompetitorInfo, CopyLead, MessageResult

__all__ = [
    "ANGLE_LABELS",
    "ANGLE_ORDER",
    "DEFAULT_MAX_REVISIONS",
    "CompetitorInfo",
    "CopyLead",
    "CopywritingAgent",
    "MessageResult",
    "OfferConfig",
    "ProductOffer",
    "ProofPoint",
    "QuoteClass",
    "SecondaryOffer",
    "Sender",
    "build_brief",
    "classify_quote",
    "eligible_angles",
]
