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


def case_discovery_target_path(
    item: DMEItem,
    cause_number: str,
    target_dir: Path | None = None,
) -> Path:
    """Build the destination path for a DME item in the per-case discovery folder.

    Used by the streaming download path. Filenames preserve TechShare's
    name verbatim (which embeds case number + Axon device + timestamp).

    2026-05-25 (Phase 0.3) — `target_dir` overrides the default
    `config.dropbox_case_discovery_dir(cause_number)` when set. Used by
    Voxhora-Mac's DownloadQueue (Phase 1) to write per-client folder
    layout (`Discovery/<client>/<cause>/`) instead of the legacy flat
    `Case_Discovery/<cause>/` path. When None (default), behavior
    unchanged.
    """
    folder = target_dir if target_dir is not None else config.dropbox_case_discovery_dir(cause_number)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / item.name


# ----- list cache (Phase 0.3, 2026-05-25) -----
#
# Voxhora-Mac's Discovery Portal calls `list <cause>` to inventory a
# case's DME items before queuing any downloads. Without caching, every
# browse-back-and-forth in the Portal hits TechShare's /api/proxy
# endpoint, which (per Travis County TOS) likely counts as a discovery-
# touch audit entry. 1-hour cache TTL keeps the audit footprint clean
# while still surfacing newly-added discovery within reasonable time.
#
# Per-cause cache file at:
#   ~/Library/Application Support/voxhora-techshare-agent/<user>/list_cache/<cause>.json
#
# File contents: the verbatim JSON `list` subcommand would emit, plus
# a `cached_at_utc` timestamp the loader checks against TTL.


def list_cache_dir(username: str | None = None) -> Path:
    base = config.state_dir(username) / "list_cache"
    base.mkdir(parents=True, exist_ok=True)
    return base


def list_cache_path(cause_number: str, username: str | None = None) -> Path:
    return list_cache_dir(username) / f"{cause_number}.json"


def load_list_cache(
    cause_number: str,
    max_age_seconds: int = 3600,
    username: str | None = None,
) -> dict | None:
    """Return cached `list` JSON if present + fresh, else None."""
    path = list_cache_path(cause_number, username)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception as e:
        log.warning("list cache for %s corrupt (%s); ignoring", cause_number, e)
        return None
    cached_at = payload.get("cached_at_utc")
    if not cached_at:
        return None
    try:
        ts = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
    except Exception:
        return None
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age > max_age_seconds:
        log.info("list cache for %s expired (%.0fs old, max %ds)", cause_number, age, max_age_seconds)
        return None
    return payload


def save_list_cache(
    cause_number: str,
    payload: dict,
    username: str | None = None,
) -> Path:
    """Persist `list` JSON to the per-cause cache file."""
    path = list_cache_path(cause_number, username)
    # The caller already populated `cached_at_utc`; we just write through.
    atomic_write_json(path, payload)
    return path


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


def load_case_cache(username: str | None = None) -> dict[str, dict]:
    """Load the cause-number → {case_uuid, service_id, backend_port} map.

    Returns {} if the cache doesn't exist yet (caller should surface a
    helpful error directing the user to seed via the scrape script).
    """
    path = config.case_uuid_cache_path(username)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.error("case-uuid cache corrupt (%s); returning empty", e)
        return {}


def lookup_case(cause_number: str, username: str | None = None) -> dict | None:
    """Look up a case in the cache. Returns None if not present."""
    return load_case_cache(username).get(cause_number)


def case_cache_stats(username: str | None = None) -> dict:
    """Counts by backend port (useful for cli status output)."""
    cache = load_case_cache(username)
    by_port: dict[int, int] = {}
    for entry in cache.values():
        port = entry.get("backend_port")
        if port is not None:
            by_port[port] = by_port.get(port, 0) + 1
    return {"total": len(cache), "by_port": by_port}


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
