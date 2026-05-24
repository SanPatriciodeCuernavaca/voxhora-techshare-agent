"""Filesystem persistence: atomic writes, seen-DME-id dedup, plea-offer JSON output.

Everything the agent writes is meant to be safely re-runnable. Atomic-write
semantics (write to .tmp + rename) avoid leaving half-written files if the
process is killed mid-download.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .models import DMEItem, PleaOffer

log = logging.getLogger(__name__)


# ----- atomic writes -----


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically (write-to-tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, delete=False, prefix=f".{path.name}.", suffix=".tmp"
    ) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True, default=str))


# ----- seen-DME-id dedup -----


def load_seen_dme_ids(username: str | None = None) -> set[str]:
    """Set of DME enclosure URL fingerprints we've already downloaded."""
    path = config.seen_dme_path(username)
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except Exception as e:
        log.warning("seen-DME cache corrupt (%s); starting fresh", e)
        return set()


def save_seen_dme_ids(ids: set[str], username: str | None = None) -> None:
    path = config.seen_dme_path(username)
    atomic_write_json(path, sorted(ids))


def dme_fingerprint(item: DMEItem) -> str:
    """Stable identity for dedup. Prefers the dmeId query param if present."""
    href = item.enclosure_href or ""
    # Extract the dmeId UUID from the query string if available; otherwise
    # fall back to the (name + size + available-date) tuple as a fingerprint.
    import re
    m = re.search(r"dmeId=([a-f0-9-]+)", href, re.IGNORECASE)
    if m:
        return f"dmeId:{m.group(1)}"
    return f"compose:{item.name}|{item.size}|{item.available_date}"


# ----- file outputs -----


def write_pc_affidavit(item: DMEItem, content: bytes, cause_number: str) -> Path:
    """Write a PC Affidavit PDF into Voxhora's Bulk_Inbox for AutoIntakeWatcher.

    Filename convention: <cause-number>.pdf (if not already named that way)
    Voxhora's existing AutoIntakeWatcher matches D1DC*/C1CR* patterns; the
    cause-number-named file ensures the pipeline attributes to the right case.
    """
    inbox = config.dropbox_inbox()
    target = inbox / f"{cause_number}.pdf"
    # If the file already exists, don't overwrite — TechShare may re-share
    # an identical PC; leave the original timestamp/processing intact.
    if target.exists():
        log.info("PC %s already on disk; skipping write", target.name)
        return target
    atomic_write_bytes(target, content)
    log.info("wrote PC affidavit: %s (%d bytes)", target, len(content))
    return target


def write_case_discovery_file(item: DMEItem, content: bytes, cause_number: str) -> Path:
    """Write a DME item (video, audio, other) into the case-specific folder.

    Used by the on-demand `fetch <cause>` subcommand. Filenames are preserved
    as-named by TechShare (e.g. Axon_Fleet_3_Front_Camera_Video_2026-01-05_1452.mp4).
    """
    folder = config.dropbox_case_discovery_dir(cause_number)
    target = folder / item.name
    if target.exists() and target.stat().st_size > 0:
        log.info("DME %s already on disk; skipping", target.name)
        return target
    atomic_write_bytes(target, content)
    log.info("wrote DME: %s (%d bytes)", target, len(content))
    return target


def write_plea_offer(plea: PleaOffer, cause_number: str) -> Path:
    """Write the plea-offer text into a JSON sidecar for Voxhora to ingest.

    The Voxhora-Mac side of v1 reads this file (TBD ingestor) and populates
    Case.latestPleaOfferText + latestPleaOfferDate via the existing CloudKit
    sync path. CloudKit then carries it to iPhone/iPad case view.
    """
    inbox = config.dropbox_inbox()
    target = inbox / f"{cause_number}.plea.json"
    payload = {
        "cause_number": cause_number,
        "plea_offer_text": plea.text,
        "can_sign": plea.can_sign,
        "has_document": plea.has_document,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_json(target, payload)
    log.info("wrote plea offer JSON: %s", target)
    return target


# ----- last-run bookkeeping -----


def record_run_result(
    *,
    mode: str,
    cause_number: str | None = None,
    event_type: str | None = None,
    pcs_downloaded: int = 0,
    plea_captured: bool = False,
    error: str | None = None,
    username: str | None = None,
) -> None:
    """Append a row to the last-run log + update aggregate counters.

    Voxhora-Mac's Settings UI reads the aggregate to show "Last run: X cases
    processed · timestamp" in the TechShare backfill panel.
    """
    path = config.last_run_path(username)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            existing = {}

    history = existing.get("history", [])
    history.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "cause_number": cause_number,
            "event_type": event_type,
            "pcs_downloaded": pcs_downloaded,
            "plea_captured": plea_captured,
            "error": error,
        }
    )
    # Cap history at 500 rows
    history = history[-500:]

    aggregate = existing.get("aggregate", {})
    aggregate["last_ts"] = history[-1]["ts"]
    aggregate["total_runs"] = aggregate.get("total_runs", 0) + 1
    aggregate["total_pcs_downloaded"] = aggregate.get("total_pcs_downloaded", 0) + pcs_downloaded
    aggregate["total_pleas_captured"] = aggregate.get("total_pleas_captured", 0) + (1 if plea_captured else 0)

    atomic_write_json(path, {"aggregate": aggregate, "history": history})
