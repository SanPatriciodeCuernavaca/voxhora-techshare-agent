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
        """Download the binary content of a DME item via its enclosure link.

        Returns raw bytes. Caller writes to disk with the desired filename.
        For large videos, this materializes the full body in memory; future
        enhancement: streaming-write via session.proxy_download_stream.
        """
        if not item.enclosure_href:
            raise ValueError(f"DME item {item.name!r} has no enclosure link")
        # The /dmefile endpoint takes ?dmeId=... &isStream=0|1.
        # Defaults from TechShare's Ember UI include isStream=1 (browser
        # playback). We force isStream=0 for download semantics.
        href = item.enclosure_href
        if "isStream=" in href:
            href = href.replace("isStream=1", "isStream=0")
        elif "?" in href:
            href = href + "&isStream=0"
        else:
            href = href + "?isStream=0"
        return self.session.proxy_download(service_id, href)

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
