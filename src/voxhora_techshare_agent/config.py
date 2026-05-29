"""Filesystem layout, environment, and per-user paths."""

from __future__ import annotations

import getpass
import os
from pathlib import Path

TECHSHARE_HOST = "attorney.techsharetx.gov"
TECHSHARE_BASE_URL = f"https://{TECHSHARE_HOST}"

# Travis County service scopes (verified 2026-05-25)
SERVICE_TRAVIS_COUNTY_ATTORNEY = "598994d8-48eb-457f-b5fe-3f97e5072ecb"
SERVICE_TRAVIS_DISTRICT_ATTORNEY = "b278d33e-7e14-4abd-be16-6f22135d9193"

# Backend per service (HTTP, internal-only — reached through /api/proxy)
BACKEND_CA = "http://198.214.211.41:1030"
BACKEND_DA = "http://198.214.211.41:1031"

# DME content host for LARGE-file downloads (videos, audio, big ZIPs).
# These do NOT come through /api/proxy — that relay 500s instantly in
# download mode and hangs indefinitely in stream mode on multi-GB files
# (verified 2026-05-29). The TechShare web player instead POSTs
# /api/dme/download/prep (which stages the file + returns a `downloadLink`
# response header + a DefensePortalAuth cookie), then GETs the file from
# this dedicated DME content host. Travis County host, captured 2026-05-29
# by tracing the live web player's network traffic.
DME_DOWNLOAD_HOST = "defensedmeca.traviscountytx.gov"
DME_DOWNLOAD_BASE_URL = f"https://{DME_DOWNLOAD_HOST}"

# Email sender that triggers agent processing
TECHSHARE_SENDER = "TechShareProsecutor@traviscountytx.gov"

# Keychain service name (per-user credentials)
KEYCHAIN_SERVICE = "voxhora-techshare-agent"


def state_dir(username: str | None = None) -> Path:
    """Per-user state directory. macOS convention."""
    user = username or getpass.getuser()
    base = Path.home() / "Library" / "Application Support" / "voxhora-techshare-agent" / user
    base.mkdir(parents=True, exist_ok=True)
    return base


def cookies_path(username: str | None = None) -> Path:
    return state_dir(username) / "cookies.pickle"


def seen_dme_path(username: str | None = None) -> Path:
    return state_dir(username) / "seen_dme_ids.json"


def last_run_path(username: str | None = None) -> Path:
    return state_dir(username) / "last_run.json"


def case_uuid_cache_path(username: str | None = None) -> Path:
    """Cause-number → {case_uuid, service_id, backend_port} map.

    Seeded by a one-time scrape of /Ember/Cases (see Voxhora handoff doc
    2026-05-25 for the seed methodology). Future: agent self-refreshes via
    an authenticated HTTP scrape of the same endpoint.
    """
    return state_dir(username) / "case_uuid_cache.json"


def log_dir(username: str | None = None) -> Path:
    base = Path.home() / "Library" / "Logs" / "voxhora-techshare-agent" / (username or getpass.getuser())
    base.mkdir(parents=True, exist_ok=True)
    return base


def dropbox_inbox() -> Path:
    """Where the agent writes outputs for Voxhora's AutoIntakeWatcher.

    Override via VOXHORA_DROPBOX_INBOX env var.
    """
    override = os.environ.get("VOXHORA_DROPBOX_INBOX")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Dropbox" / "Voxhora" / "Bulk_Inbox"


def dropbox_case_discovery_dir(cause_number: str) -> Path:
    """Per-case discovery folder for on-demand fetches (videos, audio, all DME)."""
    override = os.environ.get("VOXHORA_DROPBOX_CASE_DISCOVERY")
    base = Path(override).expanduser() if override else Path.home() / "Dropbox" / "Voxhora" / "Case_Discovery"
    return base / cause_number


def service_backend_for_scope(service_id: str) -> str:
    """Map a service UUID to its backend base URL."""
    if service_id == SERVICE_TRAVIS_COUNTY_ATTORNEY:
        return BACKEND_CA
    if service_id == SERVICE_TRAVIS_DISTRICT_ATTORNEY:
        return BACKEND_DA
    raise ValueError(f"Unknown service scope: {service_id}")
