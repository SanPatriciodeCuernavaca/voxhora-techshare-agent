"""Dataclasses for TechShare API responses + parsed events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class CaseDetail:
    """Parsed from /api/proxy case-detail response.

    Wraps the Collection+JSON `items[0]` envelope. Data fields are normalized
    to attribute access; link relations are exposed as a dict for the agent
    to walk.
    """

    case_id: str | None
    case_number: str  # e.g. "C1CR26203830" or "D1DC23207931"
    cause_number: str  # often same as case_number for TC
    status: str | None
    trn: str | None
    defendant_name: str
    defendant_dob: str | None
    defendant_cid: str | None
    defendant_sex: str | None
    defendant_race: str | None
    defendant_ethnicity: str | None
    defendant_custody_status: str | None
    custody_agency: str | None
    prosecutor_name: str | None
    intake_attorney_name: str | None
    court_name: str | None
    court_setting: str | None
    is_archived: bool
    is_juvenile_system: bool
    total_dme_size: str | None  # human string like "1.28 GB" — not parsed numerically
    attachment_owner_id: str | None
    links: dict[str, str] = field(default_factory=dict)  # rel → href

    @classmethod
    def from_collection_json(cls, payload: dict[str, Any]) -> CaseDetail:
        item = payload["collection"]["items"][0]
        data = {f["name"]: f.get("value") for f in item.get("data", [])}
        # Collection+JSON allows links without href (action stubs). Filter them out.
        links = {l["rel"]: l["href"] for l in item.get("links", []) if l.get("rel") and l.get("href")}
        return cls(
            case_id=data.get("case-id"),
            case_number=data.get("case-number", ""),
            cause_number=data.get("cause-number", ""),
            status=data.get("status"),
            trn=data.get("trn"),
            defendant_name=data.get("defendant-name", ""),
            defendant_dob=data.get("defendant-dob"),
            defendant_cid=data.get("defendant-cid"),
            defendant_sex=data.get("defendant-sex"),
            defendant_race=data.get("defendant-race"),
            defendant_ethnicity=data.get("defendant-ethnicity"),
            defendant_custody_status=data.get("defendant-custody-status"),
            custody_agency=data.get("custody-agency"),
            prosecutor_name=data.get("prosecutor-name"),
            intake_attorney_name=data.get("intake-attorney-name"),
            court_name=data.get("court-name"),
            court_setting=data.get("court-setting"),
            is_archived=str(data.get("is-archived", "False")).lower() == "true",
            is_juvenile_system=str(data.get("is-juvenile-system", "False")).lower() == "true",
            total_dme_size=data.get("total-dme-size"),
            attachment_owner_id=data.get("attachment-owner-id"),
            links=links,
        )


@dataclass(frozen=True)
class DMEItem:
    """One item from a DME (Discovery Media Evidence) list."""

    name: str  # filename, e.g. "BONDD1DC25303761.pdf" or "Axon_Fleet_3_Front_Camera_Video_2026-01-05_1452.mp4"
    type: str  # e.g. "PC Affidavit / Arrest Warrant", "Video - BodyCam", "Audio - 911 Call"
    source: str  # e.g. "Government"
    size: str  # human string, e.g. "847 KB" or "1,282,011 KB"
    available_date: str  # ISO-ish string from TechShare
    last_accessed_date: str | None
    is_archived: bool
    enclosure_href: str | None  # URL to /dmefile?dmeId=...&isStream=...
    api_href: str | None  # the rel=defense-dme-api link

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> DMEItem:
        data = {f["name"]: f.get("value") for f in item.get("data", [])}
        # Collection+JSON allows links without href (action stubs). Filter them out.
        links = {l["rel"]: l["href"] for l in item.get("links", []) if l.get("rel") and l.get("href")}
        return cls(
            name=data.get("name", ""),
            type=data.get("type", ""),
            source=data.get("source", ""),
            size=data.get("size", ""),
            available_date=data.get("available-date", ""),
            last_accessed_date=data.get("last-accessed-date"),
            is_archived=str(data.get("is-dme-archived", "False")).lower() == "true",
            enclosure_href=links.get("enclosure"),
            api_href=links.get("defense-dme-api"),
        )

    @property
    def is_pc_affidavit(self) -> bool:
        # Tolerant match (2026-07-09, Patrick's review): the canonical
        # TechShare Type label is "PC Affidavit / Arrest Warrant", but
        # exact equality is brittle against spacing/case drift
        # ("PC Affidavit/Arrest Warrant", trailing whitespace, …).
        # Normalize case + whitespace and substring-match — matching is
        # ALWAYS on TechShare's structured `type` field (the DME table's
        # Type column), never the filename and never OCR/vision.
        normalized = " ".join(self.type.lower().split())
        return "pc affidavit" in normalized

    @property
    def is_video(self) -> bool:
        return self.type.startswith("Video")

    @property
    def is_audio(self) -> bool:
        return self.type.startswith("Audio")


@dataclass(frozen=True)
class PleaOffer:
    """Latest plea offer from /pleaoffer endpoint.

    Note: TechShare embeds the offer DATE inside the `text` string itself
    (e.g. "5/21/2026\\r\\n[Count 1 - A001] Dismissal..."), not as a separate
    field. Voxhora stores the verbatim text and renders it inline.
    """

    text: str  # full offer string incl. date prefix + count label + terms
    can_sign: bool  # whether Patrick can digitally sign acceptance via TechShare
    has_document: bool  # True = PDF attached; False = text-only (Wheeler case)
    actions_id: str | None  # UUID for action enumeration (agent doesn't follow)

    @classmethod
    def from_collection_json(cls, payload: dict[str, Any]) -> PleaOffer | None:
        items = payload.get("collection", {}).get("items", [])
        if not items:
            return None
        item = items[0]
        data = {f["name"]: f.get("value") for f in item.get("data", [])}
        text = data.get("plea-offer", "")
        if not text:
            return None
        return cls(
            text=text,
            # ASP.NET serializes booleans as the strings "True" / "False"
            can_sign=str(data.get("can-sign", "False")).lower() == "true",
            has_document=str(data.get("has-document", "False")).lower() == "true",
            actions_id=data.get("plea-actions"),
        )


@dataclass(frozen=True)
class TechShareEvent:
    """Parsed TechShare notification email body."""

    event_type: str  # "dme_discoverable" | "plea_offer_updated" | "unknown"
    cause_number: str  # extracted via regex (C1CR* or D1DC*)
    defendant_name: str | None = None  # only present in DME emails
    filenames: tuple[str, ...] = ()  # discovery filenames named in body (DME emails)
    raw_subject: str | None = None
    raw_body_excerpt: str | None = None  # first ~200 chars, for logging
