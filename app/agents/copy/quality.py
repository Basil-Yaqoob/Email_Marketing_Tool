"""Deterministic quality gates — ported from `email_writer/quality.py`.

The LLM critic judges taste. This module judges facts, because a model
asked whether it used an em dash is unreliable and a regex is not.
Anything mechanically checkable is checked here and fed to the critic as
evidence (CLAUDE.md rule 2.5: deterministic checks are not delegated to
the model). **A draft passes only if the model approves it AND the
mechanical scan is clean** — see `agents.CopywritingAgent.write_email`; the
model gets no vote on whether an em dash is present.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

BANNED_WORDS = [
    "revolutionary",
    "revolutionize",
    "cutting-edge",
    "game-changing",
    "seamless",
    "seamlessly",
    "leverage",
    "unlock",
    "elevate",
    "empower",
    "transformative",
    "streamline",
    "robust",
    "innovative",
    "best-in-class",
    "world-class",
    "delve",
    "moreover",
    "furthermore",
    "landscape",
    "realm",
    "testament",
    "tapestry",
    "underscore",
    "pivotal",
    "holistic",
    "synergy",
    "ecosystem",
    "supercharge",
    "turbocharge",
    "unparalleled",
    "bespoke",
    "cutting edge",
]

BANNED_PHRASES = [
    "i hope this email finds you well",
    "i hope this finds you well",
    "hope you're doing well",
    "my name is",
    "i'm reaching out",
    "i am reaching out",
    "i wanted to reach out",
    "i came across",
    "in today's fast-paced",
    "that's where we come in",
    "imagine a world",
    "it's not just",
    "at the end of the day",
    "circle back",
    "touch base",
    "let me know if you have any questions",
    "looking forward to hearing from you",
]

# Body word count bounds and the subject-line gate. The original kept
# these in a shared config module; there is nothing else in this platform
# that needs them, so they live here with the scanner that enforces them.
BODY_WORD_MIN = 55
BODY_WORD_MAX = 135

# Subject lines were the weakest part of the source campaign this was
# ported from: they summarised the pitch ("your award and your phone")
# instead of earning the open, and one template shape repeated across
# every lead. Tighter now, and policed by both the prompt and this
# deterministic check.
SUBJECT_WORD_MAX = 5

# A subject may never contain the product, the problem, or a benefit
# claim. Its only job is to earn the open; the body does the selling.
SUBJECT_BANNED = [
    "ai",
    "a.i.",
    "receptionist",
    "automation",
    "automate",
    "solution",
    "software",
    "platform",
    "tool",
    "service",
    "boost",
    "grow",
    "growth",
    "revenue",
    "roi",
    "profit",
    "increase",
    "improve",
    "optimise",
    "optimize",
    "missed call",
    "missed calls",
    "voicemail",
    "phone system",
    "booking system",
    "opportunity",
    "partnership",
    "proposal",
    "collaboration",
    "quick win",
    "demo",
    "free",
    "offer",
    "discount",
    "save",
    "transform",
    "unlock",
    "introduction",
    "introducing",
    "following up",
    "checking in",
    "touching base",
]

PASS_SCORE = 8

# Characters that mark machine-written text at a glance.
SMART_MAP = {
    "—": "-",  # em dash
    "–": "-",  # en dash
    "‒": "-",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
    " ": " ",
    "​": "",
}

_EMOJI = re.compile("[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff⬀-⯿]")


def sanitize(text: str) -> str:
    """Mechanically strip the tells that never need a judgement call."""
    for bad, good in SMART_MAP.items():
        text = text.replace(bad, good)
    text = _EMOJI.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    # Collapse dash variants that survived normalisation, but stay within a
    # line -- \s+ here would eat the newlines around a "---" separator and
    # weld the subject onto the body.
    text = re.sub(r"[^\S\n]+[‐-―]{1,3}[^\S\n]+", " - ", text)
    return text.strip()


def split_email(raw: str) -> tuple[str, str]:
    """Pull SUBJECT/body out of the copywriter's output format.

    Splits on the raw text first, then sanitizes each part -- sanitizing the
    whole blob first would rewrite the "---" separator before we could use it.
    """
    text = (raw or "").strip()
    subject, body = "", text

    # [^\n]+ not .+? -- a trailing \s* would otherwise run past the line end.
    m = re.search(r"^[ \t]*SUBJECT[ \t]*:[ \t]*([^\n]+)", text, re.I | re.M)
    if m:
        subject = m.group(1).strip()
        body = text[m.end() :]

    body = re.sub(r"^[ \t]*-{3,}[ \t]*$", "", body, count=1, flags=re.M)
    body = re.sub(r"^\s*(BODY|EMAIL)[ \t]*:[ \t]*", "", body.strip(), flags=re.I)
    body = re.sub(r"^```[a-z]*\s*|\s*```$", "", body.strip())

    return sanitize(subject), sanitize(body)


def _signature_block(body: str) -> str:
    """The last few short lines -- where a sign-off lives."""
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    tail = [ln for ln in lines[-4:] if len(ln.split()) <= 4]
    return "\n".join(tail)


# Beat 3 must be an invitation, not a product announcement. The model
# reverts to declarative pitching unless this is blocked mechanically.
DECLARATIVE_PITCH = re.compile(
    r"\b(?:we|i)\s+(?:build|built|make|made|offer|provide|run|have|create|created)\b"
    r"[^.?!]{0,60}?\b(?:ai\s+receptionist|receptionist|ai\s+that|service\s+that|"
    r"system\s+that|tool\s+that|something\s+that|phone\s+system)",
    re.I,
)

# "Reply 'yes' and I'll send you a video" -- engagement bait, not email.
KEYWORD_REPLY_CTA = re.compile(
    r"\breply\s+(?:with\s+)?['\"‘“]?\w+['\"’”]?\s*(?:,|-|–)?\s*"
    r"(?:and|if|to)\b",
    re.I,
)
PROMISED_ASSET = re.compile(
    r"\b(?:i'?ll|i\s+will|we'?ll|we\s+will)\s+send\b[^.?!]{0,60}?"
    r"\b(?:video|one-pager|pdf|sample|breakdown|overview|explanation|estimate|example)",
    re.I,
)

_PS_LINE = re.compile(r"^\s*P\.?\s?S\.?\b", re.I | re.M)

# Words that appear in a large share of business names, so matching on
# them would let a generic email pass. A name counts as "used" only if a
# DISTINCTIVE part of it survives into the body.
_GENERIC_NAME_WORDS = {
    "clinic",
    "clinics",
    "dental",
    "dentistry",
    "dentist",
    "center",
    "centre",
    "practice",
    "medical",
    "health",
    "healthcare",
    "care",
    "group",
    "family",
    "wellness",
    "veterinary",
    "vet",
    "surgery",
    "aesthetics",
    "aesthetic",
    "therapy",
    "physical",
    "occupational",
    "solutions",
    "services",
    "service",
    "institute",
    "associates",
    "specialized",
    "specialist",
    "community",
    "and",
    "the",
    "of",
    "for",
    "llc",
    "ltd",
    "inc",
    "pty",
    "md",
    "dds",
    "dmd",
    "spa",
    "beauty",
    "smile",
    "smiles",
}


def names_company(body: str, company: str) -> bool:
    """Does the body actually name this company?

    Deliberately lenient about form -- an email may legitimately shorten
    "Dr. Melba F. Lewis, MD" to "Dr. Lewis", or "Year One Wellness: Pediatric
    Physical Therapy & Occupational Therapy" to "Year One Wellness". It only
    needs ONE distinctive word from the registered name to survive.
    """
    cleaned = company.replace("&amp;", "&")
    cleaned = cleaned.split("|")[0].split(" - ")[0]
    cleaned = re.sub(r"\(.*?\)", " ", cleaned)

    low_body = body.lower()
    tokens = [t for t in re.split(r"[^A-Za-z0-9']+", cleaned) if t]

    # len>=3 so "Hrs" in "A Dental Care 24 Hrs" counts -- for names built
    # almost entirely from generic words, the short odd token IS the
    # distinctive part. Digits count too ("24 Hrs", "5th Street").
    distinctive = [
        t
        for t in tokens
        if (len(t) >= 3 or any(c.isdigit() for c in t)) and t.lower() not in _GENERIC_NAME_WORDS
    ]
    # Some names are entirely generic ("Medical Village"); fall back to any
    # substantial token rather than demanding the impossible.
    probes = distinctive or [t for t in tokens if len(t) >= 4]

    for t in probes:
        stem = t.lower().rstrip("'s")  # "Mike's" -> "mike"
        if len(stem) >= 3 and stem in low_body:
            return True

    # A leading phrase counts even when every word in it is individually
    # generic -- "Smile Center" is a real shortening of "Smile Center Silicon
    # Valley", and "Craft of Dentistry" of itself. Require >=10 chars so a
    # short generic head like "A Dental" cannot match "a dental practice".
    for n in (2, 3, 4):
        if len(tokens) < n:
            break
        phrase = " ".join(tokens[:n]).lower()
        if len(phrase) >= 10 and phrase in low_body:
            return True

    return not probes  # nothing to check against


def scan(
    subject: str,
    body: str,
    *,
    lead_name: str = "",
    sender_name: str = "",
    sender_company: str = "",
    allow_terms: list[str] | None = None,
    company: str = "",
) -> dict[str, Any]:
    """Mechanical findings. Empty `hard_failures` means nothing objective is wrong.

    allow_terms: real proper nouns already verified elsewhere in the record
    (e.g. a competitor's actual name) that should not trip the marketing-
    buzzword filter just because a business happens to be named "Holistic SP"
    or similar. Same class of fix as the Title-Case-vs-business-name guard
    below -- a real name is not an AI tell.
    """
    low_body = body.lower()
    low_subj = subject.lower()
    joined = f"{low_subj}\n{low_body}"
    allowed_text = " ".join(allow_terms or []).lower()

    found_words = sorted(
        {
            w
            for w in BANNED_WORDS
            if re.search(rf"\b{re.escape(w)}\b", joined)
            and not re.search(rf"\b{re.escape(w)}\b", allowed_text)
        }
    )
    found_phrases = sorted({p for p in BANNED_PHRASES if p in joined})

    words = len(body.split())
    subj_words = len(subject.split())

    dashes = re.findall(r"[‒-―⸺⸻]", subject + body)
    exclam = (subject + body).count("!")
    semis = body.count(";")
    emoji = _EMOJI.findall(subject + body)
    links = re.findall(r"https?://\S+", body)

    # A total absence of contractions is a strong machine tell.
    contractions = len(
        re.findall(
            r"\b\w+'(s|re|ve|ll|d|t|m)\b|\b(isn|aren|won|don|doesn|can|didn)'t\b", body, re.I
        )
    )

    # "Circle Vet JVC" and "DW Family Doctors" are business names and are
    # correctly capitalised. Only flag capitalisation when a function word is
    # capitalised, which is what actual Title Case does.
    function_words = {
        "the",
        "and",
        "your",
        "for",
        "with",
        "about",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "at",
        "is",
        "are",
        "you",
        "our",
    }
    title_case_subject = any(
        w.strip(",.'\"").lower() in function_words and w[:1].isupper()
        for w in subject.split()[1:]  # first word may legitimately be capitalised
    )

    hard: list[str] = []
    soft: list[str] = []

    if dashes:
        hard.append(f"{len(dashes)} em/en dash(es) survived sanitization")
    if emoji:
        hard.append(f"emoji present: {''.join(emoji)}")
    if exclam:
        hard.append(f"{exclam} exclamation mark(s)")
    if found_words:
        hard.append(f"banned vocabulary: {', '.join(found_words)}")
    if found_phrases:
        hard.append(f"banned phrase(s): {'; '.join(found_phrases)}")
    if not subject:
        hard.append("no subject line produced")
    if words > BODY_WORD_MAX:
        hard.append(f"body is {words} words (max {BODY_WORD_MAX})")
    if words < BODY_WORD_MIN:
        hard.append(f"body is {words} words (min {BODY_WORD_MIN})")

    # Signing the email with the RECIPIENT's own name. Seen in testing when the
    # sender block is unset: the writer reaches for the nearest available name,
    # which is the prospect's. Unrecoverable if it ships.
    sig = _signature_block(body)
    first = (lead_name or "").strip().split()[0] if (lead_name or "").strip() else ""
    sender_is_lead = bool(sender_name) and first.lower() == sender_name.strip().split()[0].lower()
    if (
        first
        and len(first) > 2
        and re.search(rf"\b{re.escape(first)}\b", sig, re.I)
        and not sender_is_lead
    ):
        hard.append(f"sign-off contains the RECIPIENT's name ({first!r})")

    # The sender's name and company must appear verbatim (case-insensitive) --
    # not just present somewhere, since a truncated first name without the
    # full name would otherwise slip a broken signature straight through as
    # clean.
    if sender_name and sender_name.strip().lower() not in sig.lower():
        hard.append(
            f"sign-off is missing the sender's full name ({sender_name!r} not found verbatim)"
        )
    if sender_company and sender_company.strip().lower() not in sig.lower():
        hard.append(f"sign-off is missing the sender's company ({sender_company!r} not found)")

    # No P.S. at all. One argument, one ask.
    if _PS_LINE.search(body):
        hard.append("contains a P.S.; the email must carry one argument and one ask")

    # Beat 3 must float the idea, not announce a product.
    m = DECLARATIVE_PITCH.search(body)
    if m:
        hard.append(
            f"declarative product pitch ({m.group(0)[:48]!r}); "
            f"phrase it as 'How about ... ?' instead"
        )

    # Engagement-bait CTA.
    m = KEYWORD_REPLY_CTA.search(body)
    if m:
        hard.append(f"keyword-reply CTA ({m.group(0).strip()!r}); ask a plain question instead")
    m = PROMISED_ASSET.search(body)
    if m:
        hard.append(f"promises to send an asset in exchange for a reply ({m.group(0)[:44]!r})")

    # The company must be named, not just called "your company"/"your clinic".
    if company and not names_company(body, company):
        hard.append(
            f"never names the company ({company[:32]!r}); 'your clinic' alone reads as a template"
        )

    # Beat 3 is a question and beat 5 is a question, so two is expected now.
    if body.count("?") > 2:
        soft.append(f"{body.count('?')} questions; keep it to the offer and the ask")

    # Subject rules are hard, not advisory.
    if subject:
        if subj_words > SUBJECT_WORD_MAX:
            hard.append(f"subject is {subj_words} words (max {SUBJECT_WORD_MAX})")
        hits = [b for b in SUBJECT_BANNED if re.search(rf"\b{re.escape(b)}\b", low_subj)]
        if hits:
            hard.append(f"subject reveals the pitch: {', '.join(hits)}")
        if re.search(r"\byour\b.+\band your\b", low_subj):
            hard.append("subject uses the 'your X and your Y' template shape")
        if ":" in subject:
            hard.append("subject contains a colon; reads as marketing")
        if title_case_subject:
            hard.append("subject is Title Case; use sentence case")
        if subject.endswith("."):
            soft.append("subject ends in a full stop; it is a label, not a sentence")
    if semis:
        soft.append(f"{semis} semicolon(s) in a short sales email")
    if contractions == 0:
        soft.append("no contractions anywhere; reads formal and machine-written")
    if len(links) > 1:
        soft.append(f"{len(links)} links; cold email should carry at most one")

    return {
        "hard_failures": hard,
        "soft_warnings": soft,
        "body_words": words,
        "subject_words": subj_words,
        "contractions": contractions,
        "links": links,
        "clean": not hard,
    }


def render_scan(result: dict[str, Any]) -> str:
    """Findings as text for the critic prompt."""
    if result["clean"] and not result["soft_warnings"]:
        return "Mechanical scan: clean. No banned tokens, dashes, or length issues."
    lines = ["Mechanical scan findings (these are objective, not opinions):"]
    for h in result["hard_failures"]:
        lines.append(f"  HARD: {h}")
    for s in result["soft_warnings"]:
        lines.append(f"  soft: {s}")
    lines.append(
        f"  body={result['body_words']}w subject={result['subject_words']}w "
        f"contractions={result['contractions']}"
    )
    return "\n".join(lines)


__all__ = [
    "BANNED_PHRASES",
    "BANNED_WORDS",
    "BODY_WORD_MAX",
    "BODY_WORD_MIN",
    "PASS_SCORE",
    "SUBJECT_BANNED",
    "SUBJECT_WORD_MAX",
    "names_company",
    "render_scan",
    "sanitize",
    "scan",
    "split_email",
]
