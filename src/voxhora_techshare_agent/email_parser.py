"""Parse TechShareProsecutor@traviscountytx.gov notification emails.

Two event types observed in production (samples captured 2026-05-25 night):

  1. "DME made discoverable" — structured key/value body + filename list:

         Hi Richard,

         CID: 2459920
         Defendant Name: WATTS, JARRET JONATHAN
         Incident Number: C1CR25208232
         Case Number: C1CR25208232
         Charge(s):
         1 - A001 - ASSAULT CAUSES BODILY INJURY FAMILY VIOLENCE 13990031
         Court: COUNTY COURT AT LAW NO. 9
         The following DME was made discoverable:
         Axon_Fleet_3_Front_Camera_Video_2026-01-05_1452.mp4 plus 9 more.

  2. "Plea Offer updated" — short single-line:

         Hi Richard,

         Case C1CR26203830 - An extended Plea Offer has been updated.

Returns a TechShareEvent dataclass. Caller dispatches by event_type.
"""

from __future__ import annotations

import re
from typing import Iterable

from .models import TechShareEvent


# Cause-number patterns observed in Travis County:
#   C1CR<digits>  — County Court (misdemeanor), e.g. C1CR26203830
#   D1DC<digits>  — District Court (felony),    e.g. D1DC25303761
# Others (J1JV juvenile, etc.) not observed but the regex tolerates them.
_CAUSE_RE = re.compile(r"\b([CD]\d[A-Z]{2,3}\d{6,12})\b")

# "DME was made discoverable" trigger phrase
_DME_TRIGGER_RE = re.compile(r"DME\s+(was|were)\s+made\s+discoverable", re.IGNORECASE)

# Plea-offer trigger phrase
_PLEA_TRIGGER_RE = re.compile(r"Plea\s+Offer\s+has\s+been\s+updated", re.IGNORECASE)

# Defendant name extractor — "Defendant Name: LAST, FIRST MIDDLE"
_DEFENDANT_RE = re.compile(r"Defendant\s+Name\s*:\s*([^\r\n]+)", re.IGNORECASE)

# Filename list — after "The following DME was made discoverable:" line,
# pull anything ending in a file extension up until end-of-list or "plus N more"
_FILENAME_RE = re.compile(
    r"([A-Za-z0-9._\-]+\.(?:pdf|mp4|m4a|mp3|wav|jpg|jpeg|png|tiff|m4v|mov|wmv|avi|zip))",
    re.IGNORECASE,
)


def parse_email_body(body: str, subject: str | None = None) -> TechShareEvent:
    """Parse a TechShareProsecutor email body.

    Always returns a TechShareEvent. If the body is unrecognized, returns
    event_type="unknown" with whatever cause number could be salvaged.
    Caller decides how to handle unknown events (typically: log, green-flag,
    move on).
    """
    cause_match = _CAUSE_RE.search(body)
    cause_number = cause_match.group(1) if cause_match else ""

    excerpt = body.strip().replace("\r", "").replace("\n", " | ")[:200]

    if _PLEA_TRIGGER_RE.search(body):
        return TechShareEvent(
            event_type="plea_offer_updated",
            cause_number=cause_number,
            raw_subject=subject,
            raw_body_excerpt=excerpt,
        )

    if _DME_TRIGGER_RE.search(body):
        defendant_match = _DEFENDANT_RE.search(body)
        defendant = defendant_match.group(1).strip() if defendant_match else None

        # Pull filenames out of the post-trigger section
        trigger_pos = _DME_TRIGGER_RE.search(body).end()
        tail = body[trigger_pos : trigger_pos + 2000]
        filenames = tuple(dict.fromkeys(_FILENAME_RE.findall(tail)))  # dedup, preserve order

        return TechShareEvent(
            event_type="dme_discoverable",
            cause_number=cause_number,
            defendant_name=defendant,
            filenames=filenames,
            raw_subject=subject,
            raw_body_excerpt=excerpt,
        )

    return TechShareEvent(
        event_type="unknown",
        cause_number=cause_number,
        raw_subject=subject,
        raw_body_excerpt=excerpt,
    )


def extract_all_cause_numbers(text: str) -> list[str]:
    """Utility: find every cause-number-like token in a string.

    Useful for backfill mode where we want to enumerate cases mentioned
    across many emails without re-parsing each one.
    """
    return list(dict.fromkeys(m.group(1) for m in _CAUSE_RE.finditer(text)))
