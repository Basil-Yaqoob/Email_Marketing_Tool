"""What we are selling, and who is sending — ported from `email_writer/offer.py`.

The original hardcoded this as module-level constants the operator edited
by hand before a run. Here it is a constructed value (`OfferConfig`)
instead, for two reasons: this is a self-hosted *product* one campaign
owner configures per campaign, not a script one operator edits in place,
and CLAUDE.md rule 2.4 (no secrets or hardcoded business data in source)
extends naturally to "no one operator's product pitch baked into the
platform's source".

Everything here goes into the prompts near-verbatim. The agents are
forbidden from inventing proof, pricing, trial terms or client names —
anything left blank on `OfferConfig` simply never appears in the copy, so
it stays honest and vaguer rather than confident and false.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Things the agents must never say, regardless of prompt drift. A module
# constant, not per-offer config — this is a platform-level guarantee, not
# something a campaign owner should be able to loosen.
FORBIDDEN_CLAIMS = (
    "any named client, logo or case study that is not in the proof points",
    "any percentage, dollar figure or count that is not in the proof points",
    "any claim to have already used, tested or audited the prospect's phone line",
    "any suggestion the sender is a patient, customer or existing partner",
    "any medical, clinical or regulatory guarantee",
    "compliance claims (HIPAA/GDPR/etc.) unless explicitly listed in the proof points",
)


@dataclass(frozen=True, slots=True)
class Sender:
    name: str = ""
    company: str = ""
    role: str = ""
    # "reply" (recommended for cold outreach — cold prospects don't book)
    # or a booking URL.
    calendar_or_reply: str = "reply"
    phone: str = ""
    website: str = ""


@dataclass(frozen=True, slots=True)
class ProductOffer:
    name: str
    one_liner: str
    # Concrete, checkable mechanics. What makes copy specific instead of
    # hypey, so keep these factual and short.
    mechanics: tuple[str, ...] = ()
    # Commercial terms. Leave any of these empty and the agents will not
    # mention them.
    pricing: str = ""
    trial: str = ""
    setup_time: str = ""


@dataclass(frozen=True, slots=True)
class SecondaryOffer:
    """Mentioned ONLY in the body when the lead's own data makes it
    relevant, never competing with the primary offer for attention.
    """

    key: str
    name: str
    one_liner: str
    relevant_when: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProofPoint:
    claim: str
    verifiable: bool = True


@dataclass(frozen=True, slots=True)
class OfferConfig:
    """The whole offer block injected into every prompt.

    `proof_points` empty means the agents write no proof claims at all,
    which is the correct behaviour until a campaign owner has some.
    """

    sender: Sender
    primary: ProductOffer
    secondary: tuple[SecondaryOffer, ...] = ()
    proof_points: tuple[ProofPoint, ...] = field(default_factory=tuple)

    def sender_ready(self) -> tuple[bool, list[str]]:
        """Warn loudly rather than sending a campaign signed by nobody."""
        missing = [k for k in ("name", "company") if not getattr(self.sender, k).strip()]
        return (not missing), missing

    def render_offer(self) -> str:
        lines = [
            f"PRODUCT: {self.primary.name}",
            f"WHAT IT IS: {self.primary.one_liner}",
            "",
            "HOW IT ACTUALLY WORKS (use these specifics; do not embellish):",
        ]
        lines += [f"  - {m}" for m in self.primary.mechanics]

        commercial = [
            (label, value)
            for label, value in (
                ("pricing", self.primary.pricing),
                ("trial", self.primary.trial),
                ("setup_time", self.primary.setup_time),
            )
            if value
        ]
        if commercial:
            lines += ["", "COMMERCIAL TERMS (only mention if it strengthens the specific email):"]
            lines += [f"  - {k}: {v}" for k, v in commercial]
        else:
            lines += [
                "",
                "COMMERCIAL TERMS: none supplied. Do not invent pricing, "
                "trial length or setup time. Do not imply any.",
            ]

        if self.proof_points:
            lines += ["", "PROOF YOU MAY CITE (verbatim only):"]
            lines += [f"  - {p.claim}" for p in self.proof_points]
        else:
            lines += [
                "",
                "PROOF: none supplied. You may NOT cite results, numbers, "
                "percentages, client names or case studies. Sell the "
                "mechanism and the specific observation about this lead "
                "instead. This is a hard rule.",
            ]

        lines += ["", "SECONDARY SERVICES (at most one, only when relevant):"]
        for s in self.secondary:
            lines.append(f"  - {s.name}: {s.one_liner}")
            lines.append(f"      relevant when: {'; '.join(s.relevant_when)}")

        lines += ["", "NEVER SAY:"]
        lines += [f"  - {c}" for c in FORBIDDEN_CLAIMS]
        return "\n".join(lines)

    def render_sender(self) -> str:
        s = self.sender
        bits = [f"Name: {s.name or '[UNSET]'}", f"Company: {s.company or '[UNSET]'}"]
        if s.role:
            bits.append(f"Role: {s.role}")
        if s.website:
            bits.append(f"Site: {s.website}")
        cta = s.calendar_or_reply or "reply"
        bits.append(
            "CTA style: ask for a reply, never a calendar link"
            if cta == "reply"
            else f"Booking link: {cta}"
        )
        return " | ".join(bits)


__all__ = [
    "FORBIDDEN_CLAIMS",
    "OfferConfig",
    "ProductOffer",
    "ProofPoint",
    "SecondaryOffer",
    "Sender",
]
