"""High-level TechShare API client built on TechShareSession.

This module is the agent's stable interface against TechShare. The wire-level
schema (PascalCase body, Collection+JSON envelope, etc.) is hidden here so
callers work with clean dataclasses.
"""

from __future__ import annotations

import logging
from typing import Iterable

from . import config
from .models import CaseDetail, DMEItem, PleaOffer
from .session import TechShareSession

log = logging.getLogger(__name__)


def _ensure_download_mode(href: str) -> str:
    """Force isStream=0 on a /dmefile URL so the server returns
    Content-Disposition: attachment + the full file body instead of
    streaming-for-browser-playback. Used for both download_dme_file
    and download_dme_file_to_path."""
    if "isStream=1" in href:
        return href.replace("isStream=1", "isStream=0")
    if "isStream=" in href:
        return href  # already 0
    return href + ("&isStream=0" if "?" in href else "?isStream=0")


class TechShareClient:
    """High-level operations layered on TechShareSession."""

    def __init__(self, session: TechShareSession) -> None:
        self.session = session

    # ----- case detail -----

    def get_case_detail(self, service_id: str, case_uuid: str) -> CaseDetail:
        """Fetch the case-detail object for a given case UUID."""
        backend_base = config.service_backend_for_scope(service_id)
        path = f"{backend_base}/case/{case_uuid}"
        payload = self.session.proxy_get(service_id, path)
        return CaseDetail.from_collection_json(payload)

    # ----- DME (Discovery Media Evidence) -----

    def get_dme_list(self, service_id: str, case: CaseDetail) -> list[DMEItem]:
        """Walk the case's rel=dme link to fetch its DME items."""
        dme_href = case.links.get("dme")
        if not dme_href:
            log.warning("case %s has no rel=dme link", case.case_number)
            return []
        payload = self.session.proxy_get(service_id, dme_href)
        items = payload.get("collection", {}).get("items", [])
        return [DMEItem.from_item(it) for it in items]

    def pc_affidavits_in(self, items: Iterable[DMEItem]) -> list[DMEItem]:
        return [it for it in items if it.is_pc_affidavit]

    def videos_in(self, items: Iterable[DMEItem]) -> list[DMEItem]:
        return [it for it in items if it.is_video]

    # ----- plea offer -----

    def get_plea_offer(self, service_id: str, case: CaseDetail) -> PleaOffer | None:
        """Walk the case's rel=plea-offer-summary link if present.

        Returns None when no plea has been offered (link relation is conditional —
        absent on cases that have no current offer).
        """
        plea_href = case.links.get("plea-offer-summary")
        if not plea_href:
            return None
        payload = self.session.proxy_get(service_id, plea_href)
        return PleaOffer.from_collection_json(payload)

    # ----- file download -----

    def download_dme_file(self, service_id: str, item: DMEItem) -> bytes:
        """Download a DME item's bytes into memory.

        DEPRECATED for large items — use download_dme_file_to_path for any
        non-PDF item (videos, multi-GB ZIPs). This method OOMs for the
        1.28 GB-typical bodycam videos. Kept for small items where the
        caller specifically wants bytes (e.g. PC affidavits being passed
        to PDFKit text-extract in the existing Phase 1 pipeline).
        """
        if not item.enclosure_href:
            raise ValueError(f"DME item {item.name!r} has no enclosure link")
        href = _ensure_download_mode(item.enclosure_href)
        return self.session.proxy_download(service_id, href)

    def download_dme_file_to_path(
        self,
        service_id: str,
        item: DMEItem,
        target_path,
    ) -> int:
        """Stream a DME item directly to `target_path`. Returns bytes written.

        Safe for arbitrarily large items — writes via .partial + atomic
        rename. Cleans up on any failure. Use this for everything except
        PC affidavits in the Phase 1 in-memory OCR pipeline.

        Uses the web player's PREP flow (2026-05-29): /api/proxy cannot serve
        multi-GB videos (500s instantly in download mode, hangs forever in
        stream mode), so we POST /api/dme/download/prep to stage the file,
        then stream it from the dedicated DME content host with the
        DefensePortalAuth cookie prep hands back. This is exactly what the
        TechShare web video player does. Works for any DME size, so all
        streamed items (videos, audio, ZIPs, other) route through it.
        """
        if not item.enclosure_href:
            raise ValueError(f"DME item {item.name!r} has no enclosure link")
        link, auth = self.session.prep_dme_download(service_id, item.enclosure_href)
        return self.session.prepared_download_to_path(link, auth, target_path)

    # ----- case search (TBD — schema partial from recon) -----

    def search_cases(self, service_id: str, case_number: str) -> list[CaseDetail]:
        """Search for a case by its human-readable case number.

        TODO: The case-list/search endpoint body schema was only partially
        captured during recon. The third ajax block in /App/controllers/service.js
        exposed top-level keys (submissions, submissionUrl, serviceId, countyName)
        but the exact request shape wasn't probed live. Implementing this
        requires either: (a) another recon round to capture the exact body,
        (b) HTML-scraping the /Ember/Cases page (which we know renders the
        full case list including UUIDs in <a> hrefs).

        For agent v1, the email-triggered flow doesn't NEED search — the
        email body contains the case-number, and we cross-reference against
        Voxhora's stored TechShare UUIDs (per-case mapping table maintained
        as we process emails). The first time we see a case-number we don't
        know the UUID for, this method gets called as a fallback.
        """
        raise NotImplementedError(
            "search_cases not yet implemented — schema partial; "
            "fallback path is HTML-scrape of /Ember/Cases. "
            "Tracked as a v1 follow-up."
        )
