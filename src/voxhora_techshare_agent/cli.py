"""argparse-based CLI for voxhora-techshare-agent.

Subcommands:

  login            store TechShare credentials in macOS Keychain
  status           diagnostic: session valid? CSRF fresh? last run?
  process-email    read email body from stdin, process one event
  fetch            on-demand bulk discovery pull for ONE case (all DME)
  fetch-items      download specific DME item IDs only (Portal-driven, 2026-05-25)
  list             return JSON inventory of a case's DME items, no downloads (2026-05-25)
  refresh          light refresh — PC + plea only
  backfill         one-time scan over a date range (v1 stub)
  backfill-all     light refresh over every cached case (PC + plea each)

Each subcommand wires TechShareSession + TechShareClient + storage together.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from . import config, storage
from .email_parser import parse_email_body
from .models import CaseDetail, DMEItem
from .proxy_client import TechShareClient
from .session import TechShareAuthError, TechShareSession

log = logging.getLogger(__name__)


# ----- entrypoint -----


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="voxhora-techshare-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="Store TechShare credentials in macOS Keychain.")
    sub.add_parser("status", help="Show session + last-run status.")

    pe = sub.add_parser("process-email", help="Process a single TechShare email body from stdin.")
    pe.add_argument("--subject", default=None, help="Email subject (optional, for logging).")

    fc = sub.add_parser("fetch", help="On-demand bulk DME pull for one case (videos + all DME).")
    fc.add_argument("cause_number", help="e.g. C1CR26203830 or D1DC23207931")
    fc.add_argument("--service-id", default=None, help="Service UUID override (default: from case cache)")
    fc.add_argument("--case-uuid", default=None, help="Case UUID override (default: from case cache)")
    fc.add_argument("--target-dir", default=None, help="Override download destination (default: ~/Dropbox/Voxhora/Case_Discovery/<cause>/). Used by Voxhora-Mac's Discovery Portal to route into per-client folder layout.")
    fc.add_argument("--manifest", default=None, help="After fetch, write a JSON manifest at this path mapping item id → {path, size_bytes, downloaded_at}. Portal reads this to render the on-disk state.")

    # Phase 0.3 (2026-05-25) — Portal-driven per-item fetch + inventory list.
    fi = sub.add_parser("fetch-items", help="Download specific DME items by id (Portal-driven, Phase 0.3).")
    fi.add_argument("cause_number", help="e.g. C1CR26203830")
    fi.add_argument("item_ids", nargs="+", help="One or more DME item identifiers. Accept either a bare dmeId UUID OR the full fingerprint emitted by `list` (e.g. 'dmeId:abc-123' or 'compose:filename|size|date').")
    fi.add_argument("--service-id", default=None)
    fi.add_argument("--case-uuid", default=None)
    fi.add_argument("--target-dir", default=None, help="Override download destination (default: per-cause folder).")
    fi.add_argument("--manifest", default=None, help="After fetch, write JSON manifest at this path.")

    ls = sub.add_parser("list", help="Return JSON inventory of a case's DME items — no downloads (Phase 0.3).")
    ls.add_argument("cause_number", help="e.g. C1CR26203830")
    ls.add_argument("--service-id", default=None)
    ls.add_argument("--case-uuid", default=None)
    ls.add_argument("--no-cache", action="store_true", help="Bypass the 1-hour list cache; force a fresh fetch from TechShare.")
    ls.add_argument("--max-age", type=int, default=3600, help="Cache TTL in seconds (default 3600 = 1 hour).")

    bf = sub.add_parser("backfill", help="One-time scan over a date range (stub in v1).")
    bf.add_argument("--from", dest="from_date", required=True, help="ISO date e.g. 2026-01-01")
    bf.add_argument("--to", dest="to_date", default=None, help="ISO date (default: today)")
    bf.add_argument("--rate-limit", default="max", help="'max' or e.g. '1/sec'")

    rf = sub.add_parser("refresh", help="Light refresh for ONE case — pulls PC affidavit + plea offer only (no videos/audio/other DME). Use this for steady-state agent flows.")
    rf.add_argument("cause_number", help="e.g. C1CR26203830 or D1DC23207931")

    ba = sub.add_parser("backfill-all", help="Light refresh for ALL cases in the cause→UUID cache (PC + plea per case). Use after seeding the cache to populate every existing client.")
    ba.add_argument("--rate-limit-seconds", type=float, default=0.0, help="Seconds to sleep between cases (default 0). Use e.g. 1.0 to spread audit footprint over time.")
    ba.add_argument("--limit", type=int, default=0, help="Stop after this many cases (0 = no limit).")

    args = parser.parse_args(argv)

    dispatch = {
        "login": cmd_login,
        "status": cmd_status,
        "process-email": cmd_process_email,
        "fetch": cmd_fetch,
        "fetch-items": cmd_fetch_items,
        "list": cmd_list,
        "backfill": cmd_backfill,
        "refresh": cmd_refresh,
        "backfill-all": cmd_backfill_all,
    }
    try:
        return dispatch[args.command](args)
    except TechShareAuthError as e:
        print(f"AUTH ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        log.exception("unhandled error")
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


# ----- subcommands -----


def cmd_login(_args: argparse.Namespace) -> int:
    username = input("TechShare username: ").strip()
    password = getpass.getpass("TechShare password: ")
    session = TechShareSession()
    session.store_credentials(username, password)
    # Try to log in to verify
    try:
        session.login()
    except TechShareAuthError as e:
        print(f"Stored credentials, but login failed: {e}", file=sys.stderr)
        print("Re-run `voxhora-techshare-agent login` to update credentials.", file=sys.stderr)
        return 2
    print(f"OK — credentials stored in macOS Keychain (service={config.KEYCHAIN_SERVICE})")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    session = TechShareSession()
    authed = session.is_authenticated()
    cache_stats = storage.case_cache_stats()
    print(f"Session authenticated: {authed}")
    print(f"State dir:             {config.state_dir()}")
    print(f"Cookies file:          {config.cookies_path()}")
    print(f"Seen DME cache:        {config.seen_dme_path()}")
    print(f"Case UUID cache:       {config.case_uuid_cache_path()}")
    print(f"  total cases:         {cache_stats['total']}")
    for port, n in sorted(cache_stats["by_port"].items()):
        scope = "Travis CA" if port == 1030 else ("Travis DA" if port == 1031 else f"port {port}")
        print(f"  {scope} (port {port}): {n}")
    print(f"Bulk inbox:            {config.dropbox_inbox()}")
    print(f"Last-run log:          {config.last_run_path()}")
    return 0 if authed else 3


def cmd_process_email(args: argparse.Namespace) -> int:
    body = sys.stdin.read()
    if not body.strip():
        print("ERROR: empty email body on stdin", file=sys.stderr)
        return 1

    event = parse_email_body(body, subject=args.subject)
    log.info("parsed event: type=%s cause=%s", event.event_type, event.cause_number)

    if event.event_type == "unknown":
        log.info("unknown event type; nothing to do (caller should still green-flag)")
        storage.record_run_result(mode="process-email", cause_number=event.cause_number, event_type="unknown")
        return 0

    if not event.cause_number:
        log.warning("no cause number extracted from email; cannot proceed")
        return 1

    session = TechShareSession()
    session.ensure_authenticated()
    client = TechShareClient(session)

    resolved = _resolve_case(event.cause_number)
    if not resolved:
        # Email references a cause not in Patrick's cache (closed case,
        # archived, or never-added-to-Voxhora). Nothing for the agent to
        # do; exit success so the caller (MailInboxWatcher) green-flags
        # the email and skips it forever instead of looping retries.
        log.info(
            "skip — cause %s not in cause→UUID cache (no Voxhora client); marking email handled",
            event.cause_number,
        )
        storage.record_run_result(
            mode="process-email",
            cause_number=event.cause_number,
            event_type=event.event_type,
            error="not_in_cache_skipped",
        )
        return 0
    service_id = resolved["service_id"]
    case_uuid = resolved["case_uuid"]

    case = client.get_case_detail(service_id, case_uuid)
    log.info("case loaded: %s defendant=%s status=%s", case.case_number, case.defendant_name, case.status)

    pcs_downloaded = 0
    plea_captured = False

    if event.event_type == "dme_discoverable":
        dme_items = client.get_dme_list(service_id, case)
        pcs = client.pc_affidavits_in(dme_items)
        log.info("DME items=%d  PC affidavits=%d", len(dme_items), len(pcs))

        seen = storage.load_seen_dme_ids()
        for pc in pcs:
            fp = storage.dme_fingerprint(pc)
            if fp in seen:
                log.info("PC %s already downloaded (fingerprint %s); skipping", pc.name, fp)
                continue
            data = client.download_dme_file(service_id, pc)
            storage.write_pc_affidavit(pc, data, event.cause_number)
            seen.add(fp)
            pcs_downloaded += 1
        storage.save_seen_dme_ids(seen)

    elif event.event_type == "plea_offer_updated":
        plea = client.get_plea_offer(service_id, case)
        if plea:
            storage.write_plea_offer(plea, event.cause_number)
            plea_captured = True
        else:
            log.warning("plea_offer_updated event but no plea-offer-summary link on case %s", case.case_number)

    storage.record_run_result(
        mode="process-email",
        cause_number=event.cause_number,
        event_type=event.event_type,
        pcs_downloaded=pcs_downloaded,
        plea_captured=plea_captured,
    )
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Pull all DME for one cause. Phase 0.3 adds --target-dir and --manifest."""
    cause_number = args.cause_number

    # Resolve from cache unless overridden on CLI
    resolved = _resolve_case(cause_number) or {}
    service_id = args.service_id or resolved.get("service_id")
    case_uuid = args.case_uuid or resolved.get("case_uuid")
    if not service_id or not case_uuid:
        log.error(
            "Cannot resolve %s — not in cause→UUID cache, and --service-id / "
            "--case-uuid not provided.",
            cause_number,
        )
        return 4

    target_dir = Path(args.target_dir).expanduser() if args.target_dir else None
    manifest_path = Path(args.manifest).expanduser() if args.manifest else None

    session = TechShareSession()
    session.ensure_authenticated()
    client = TechShareClient(session)

    case = client.get_case_detail(service_id, case_uuid)
    dme_items = client.get_dme_list(service_id, case)
    log.info("fetch %s: %d DME items total", cause_number, len(dme_items))

    seen = storage.load_seen_dme_ids()
    manifest_entries: dict[str, dict] = {}
    new_downloads = 0
    failures = 0
    for item in dme_items:
        fp = storage.dme_fingerprint(item)
        if fp in seen:
            log.info("skip (already-seen): %s", item.name)
            continue
        # Patrick 2026-05-27 LOCK — DO NOT skip on `is_archived`.
        # TechShare flips that flag when the attorney has viewed an
        # item on the web UI; it does NOT mean "should not download."
        # seen_dme_ids (above) is the canonical "already-downloaded"
        # gate. Photo ZIPs surfaced this bug — TechShare archived
        # them after a web preview, the agent skipped them, photos
        # never landed on disk.
        try:
            if item.is_pc_affidavit:
                # PC affidavits go into Phase 1 OCR pipeline — caller
                # (Voxhora's AutoIntakeWatcher) wants bytes for PDFKit.
                # PCs are PDFs, max few MB; in-memory load is fine.
                data = client.download_dme_file(service_id, item)
                written_path = storage.write_pc_affidavit(item, data, cause_number)
                size_bytes = len(data)
            else:
                # Stream everything else directly to disk — videos can
                # be 1+ GB; never hold them in RAM.
                written_path = storage.case_discovery_target_path(
                    item, cause_number, target_dir=target_dir
                )
                bytes_written = client.download_dme_file_to_path(service_id, item, written_path)
                size_bytes = bytes_written
                log.info("streamed %s → %s (%d bytes)", item.name, written_path, bytes_written)
            seen.add(fp)
            new_downloads += 1
            manifest_entries[fp] = _manifest_entry(item, written_path, size_bytes)
            # Persist seen-set after EACH success so a mid-fetch crash
            # doesn't re-download work already complete.
            storage.save_seen_dme_ids(seen)
        except Exception as e:
            log.error("FAILED %s: %s", item.name, e)
            failures += 1
            # Persist seen-set even on partial failure
            storage.save_seen_dme_ids(seen)
            # Don't abort the loop — keep trying remaining items

    if manifest_path is not None:
        _write_manifest(manifest_path, cause_number, manifest_entries)
        log.info("wrote manifest: %s (%d entries)", manifest_path, len(manifest_entries))

    print(f"OK — {cause_number}: {new_downloads} new files downloaded, {failures} failed ({len(dme_items)} total in case)")
    return 0 if failures == 0 else 2


def cmd_fetch_items(args: argparse.Namespace) -> int:
    """Phase 0.3 — Portal-driven per-item fetch.

    The Discovery Portal calls `list <cause>` to inventory + selects items
    (via checkboxes / type-filter buttons / individual picks), then calls
    `fetch-items <cause> <id> [<id>...]` to download only those.

    Item identifiers are the fingerprints emitted by `list`. We accept
    either a bare dmeId UUID (auto-prefixed with 'dmeId:') or the full
    fingerprint string ('dmeId:...' or 'compose:...').

    --target-dir and --manifest behave identically to `fetch`.
    """
    cause_number = args.cause_number
    resolved = _resolve_case(cause_number) or {}
    service_id = args.service_id or resolved.get("service_id")
    case_uuid = args.case_uuid or resolved.get("case_uuid")
    if not service_id or not case_uuid:
        log.error("Cannot resolve %s for fetch-items.", cause_number)
        return 4

    requested = _normalize_fingerprints(args.item_ids)
    if not requested:
        log.error("No item ids provided.")
        return 1

    target_dir = Path(args.target_dir).expanduser() if args.target_dir else None
    manifest_path = Path(args.manifest).expanduser() if args.manifest else None

    session = TechShareSession()
    session.ensure_authenticated()
    client = TechShareClient(session)

    case = client.get_case_detail(service_id, case_uuid)
    dme_items = client.get_dme_list(service_id, case)

    # Filter to requested fingerprints. Items not found in the live list
    # (e.g., stale Portal manifest) are reported as failures but don't
    # abort the rest of the batch.
    fingerprint_to_item = {storage.dme_fingerprint(item): item for item in dme_items}
    missing = sorted(requested - set(fingerprint_to_item.keys()))
    to_fetch = [fingerprint_to_item[fp] for fp in requested if fp in fingerprint_to_item]
    log.info(
        "fetch-items %s: %d requested, %d resolved, %d missing",
        cause_number, len(requested), len(to_fetch), len(missing),
    )
    for fp in missing:
        log.warning("requested fingerprint not in live DME list: %s", fp)

    seen = storage.load_seen_dme_ids()
    manifest_entries: dict[str, dict] = {}
    new_downloads = 0
    failures = len(missing)  # missing items count as failures
    for item in to_fetch:
        fp = storage.dme_fingerprint(item)
        if fp in seen:
            log.info("skip (already-seen): %s", item.name)
            # Still surface in manifest so the Portal sees the existing on-disk state
            existing_path = storage.case_discovery_target_path(
                item, cause_number, target_dir=target_dir
            )
            if existing_path.exists():
                manifest_entries[fp] = _manifest_entry(item, existing_path, existing_path.stat().st_size)
            continue
        # Patrick 2026-05-27 LOCK — `is_archived` is TechShare's
        # "attorney viewed it on the web" flag, NOT a "should skip
        # download" signal. Removed: bulk fetch downloads EVERYTHING
        # not in seen_dme_ids regardless of TechShare's archived flag.
        # Photo ZIPs in particular were silently archived by TechShare
        # and never reached the local disk.
        try:
            if item.is_pc_affidavit:
                data = client.download_dme_file(service_id, item)
                written_path = storage.write_pc_affidavit(item, data, cause_number)
                size_bytes = len(data)
            else:
                written_path = storage.case_discovery_target_path(
                    item, cause_number, target_dir=target_dir
                )
                size_bytes = client.download_dme_file_to_path(service_id, item, written_path)
                log.info("streamed %s → %s (%d bytes)", item.name, written_path, size_bytes)
            seen.add(fp)
            new_downloads += 1
            manifest_entries[fp] = _manifest_entry(item, written_path, size_bytes)
            storage.save_seen_dme_ids(seen)
        except Exception as e:
            log.error("FAILED %s: %s", item.name, e)
            failures += 1
            storage.save_seen_dme_ids(seen)

    if manifest_path is not None:
        _write_manifest(manifest_path, cause_number, manifest_entries)
        log.info("wrote manifest: %s (%d entries)", manifest_path, len(manifest_entries))

    print(
        f"OK — {cause_number}: {new_downloads} new files, {failures} failed, "
        f"{len(requested) - len(missing) - new_downloads} skipped-already-on-disk"
    )
    return 0 if failures == 0 else 2


def cmd_list(args: argparse.Namespace) -> int:
    """Phase 0.3 — emit JSON inventory of a case's DME items.

    No bytes downloaded. Per-cause 1-hour cache reduces TechShare audit
    footprint when the Portal browses back-and-forth.
    """
    cause_number = args.cause_number

    # Cache hit path
    if not args.no_cache:
        cached = storage.load_list_cache(cause_number, max_age_seconds=args.max_age)
        if cached is not None:
            print(json.dumps(cached, indent=2, sort_keys=True))
            return 0

    # Cache miss / bypassed — fetch live
    resolved = _resolve_case(cause_number) or {}
    service_id = args.service_id or resolved.get("service_id")
    case_uuid = args.case_uuid or resolved.get("case_uuid")
    if not service_id or not case_uuid:
        log.error("Cannot resolve %s for list.", cause_number)
        return 4

    session = TechShareSession()
    session.ensure_authenticated()
    client = TechShareClient(session)

    case = client.get_case_detail(service_id, case_uuid)
    dme_items = client.get_dme_list(service_id, case)

    payload = {
        "cause_number": cause_number,
        "case_number": case.case_number,
        "defendant_name": case.defendant_name,
        "status": case.status,
        "total_dme_size": case.total_dme_size,
        "is_archived": case.is_archived,
        "cached_at_utc": datetime.now(timezone.utc).isoformat(),
        "items": [_list_item_json(item) for item in dme_items],
    }

    storage.save_list_cache(cause_number, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


# ----- helpers (Phase 0.3) -----


def _normalize_fingerprints(raw_ids: list[str]) -> set[str]:
    """Accept either bare UUIDs or full fingerprint strings. Returns the
    normalized set ready to match against `storage.dme_fingerprint(item)`.
    """
    import re
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
    out: set[str] = set()
    for raw in raw_ids:
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith("dmeId:") or raw.startswith("compose:"):
            out.add(raw)
        elif uuid_re.match(raw):
            out.add(f"dmeId:{raw}")
        else:
            log.warning("unrecognized item id format (skipping): %s", raw)
    return out


def _classify_item(item: DMEItem) -> str:
    """Map a DMEItem to Portal-level category (video|written|audio|other)
    used by the Discovery Portal's filter chips + "Add all of type X"
    bulk buttons. Filename extension is the primary signal; falls back to
    TechShare's `type` label when extension is ambiguous.
    """
    name_lower = item.name.lower()
    if name_lower.endswith((".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")):
        return "video"
    if name_lower.endswith((".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")):
        return "audio"
    if name_lower.endswith((".pdf", ".txt", ".doc", ".docx", ".rtf")):
        return "written"
    # Patrick 2026-05-27 — photos arrive as image files (direct) OR as
    # ZIP archives bundling multiple photos. Both surface in the Portal
    # under filter chip "Images" / "Archives" so the attorney can
    # see + bulk-add them by type.
    if name_lower.endswith((".jpg", ".jpeg", ".png", ".heic", ".tiff", ".gif", ".webp", ".bmp")):
        return "image"
    if name_lower.endswith((".zip", ".7z", ".rar", ".tar", ".gz")):
        return "archive"
    # Fall back to TechShare's type label
    if item.is_video:
        return "video"
    if item.is_audio:
        return "audio"
    if item.is_pc_affidavit or item.type.lower().endswith("affidavit"):
        return "written"
    return "other"


def _list_item_json(item: DMEItem) -> dict:
    return {
        "id": storage.dme_fingerprint(item),
        "name": item.name,
        "type_label": item.type,
        "category": _classify_item(item),
        "source": item.source,
        "size": item.size,
        "available_date": item.available_date,
        "last_accessed_date": item.last_accessed_date,
        "is_archived": item.is_archived,
        "is_pc_affidavit": item.is_pc_affidavit,
        "is_video": item.is_video,
        "is_audio": item.is_audio,
    }


def _manifest_entry(item: DMEItem, path: Path, size_bytes: int) -> dict:
    return {
        "filename": item.name,
        "type_label": item.type,
        "category": _classify_item(item),
        "size_bytes": int(size_bytes),
        "size_string": item.size,
        "path": str(path),
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _write_manifest(path: Path, cause_number: str, entries: dict[str, dict]) -> None:
    """Atomically write the post-fetch manifest at `path`. If a manifest
    already exists at that path, MERGE — preserve prior items so multiple
    fetch-items calls accumulate.
    """
    existing: dict[str, dict] = {}
    if path.exists():
        try:
            prior = json.loads(path.read_text())
            existing = prior.get("items", {})
        except Exception as e:
            log.warning("prior manifest at %s unreadable (%s); overwriting", path, e)
    merged = {**existing, **entries}
    payload = {
        "cause_number": cause_number,
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
        "items": merged,
    }
    storage.atomic_write_json(path, payload)


def _refresh_one_case(cause_number: str, client: TechShareClient) -> dict:
    """Internal: light refresh of one case — PC affidavit + plea offer only.

    Returns {"cause": ..., "pcs_downloaded": int, "plea_captured": bool, "skipped": bool, "error": str|None}
    Does NOT download videos, audio, or other discovery — that's reserved
    for the explicit `fetch` subcommand (invoked via Voxhora-Mac's
    "Download Remaining Discovery" button).
    """
    result = {"cause": cause_number, "pcs_downloaded": 0, "plea_captured": False, "skipped": False, "error": None}
    resolved = _resolve_case(cause_number)
    if not resolved:
        result["error"] = "not in cache"
        return result
    service_id = resolved["service_id"]
    case_uuid = resolved["case_uuid"]

    try:
        case = client.get_case_detail(service_id, case_uuid)
    except Exception as e:
        result["error"] = f"case-detail failed: {e}"
        return result

    # PC affidavit only
    try:
        dme_items = client.get_dme_list(service_id, case)
    except Exception as e:
        result["error"] = f"dme-list failed: {e}"
        return result

    pcs = client.pc_affidavits_in(dme_items)
    seen = storage.load_seen_dme_ids()
    for pc in pcs:
        fp = storage.dme_fingerprint(pc)
        if fp in seen:
            continue
        try:
            data = client.download_dme_file(service_id, pc)
            storage.write_pc_affidavit(pc, data, cause_number)
            seen.add(fp)
            result["pcs_downloaded"] += 1
        except Exception as e:
            result["error"] = f"PC download failed: {e}"
            break
    storage.save_seen_dme_ids(seen)

    # Plea offer (only if the link is present on the case)
    try:
        plea = client.get_plea_offer(service_id, case)
        if plea:
            storage.write_plea_offer(plea, cause_number)
            result["plea_captured"] = True
    except Exception as e:
        # Don't fail the whole refresh on a plea read error
        log.warning("plea fetch failed for %s: %s", cause_number, e)

    return result


def cmd_refresh(args: argparse.Namespace) -> int:
    session = TechShareSession()
    session.ensure_authenticated()
    client = TechShareClient(session)
    r = _refresh_one_case(args.cause_number, client)
    storage.record_run_result(
        mode="refresh",
        cause_number=args.cause_number,
        pcs_downloaded=r["pcs_downloaded"],
        plea_captured=r["plea_captured"],
        error=r["error"],
    )
    if r["error"]:
        print(f"ERROR {args.cause_number}: {r['error']}", file=sys.stderr)
        return 1
    print(f"OK {args.cause_number} — PCs +{r['pcs_downloaded']}, plea {'captured' if r['plea_captured'] else 'none'}")
    return 0


def cmd_backfill_all(args: argparse.Namespace) -> int:
    """Iterate every case in the cause→UUID cache and run a light refresh.

    Light = PC affidavit + plea offer only. No videos/audio/other DME —
    that's reserved for the per-case `fetch` subcommand invoked via the
    Voxhora-Mac case-view "Download Remaining Discovery" button.
    """
    import time
    cache = storage.load_case_cache()
    if not cache:
        print("ERROR: case-uuid cache empty. Seed via Chrome MCP scrape "
              "(see Voxhora handoff doc 2026-05-25) before running backfill-all.",
              file=sys.stderr)
        return 1

    causes = sorted(cache.keys())
    if args.limit > 0:
        causes = causes[: args.limit]

    session = TechShareSession()
    session.ensure_authenticated()
    client = TechShareClient(session)

    totals = {"cases": 0, "pcs": 0, "pleas": 0, "errors": 0}
    print(f"backfill-all: {len(causes)} cases (rate-limit {args.rate_limit_seconds}s between cases)")
    for i, cause in enumerate(causes, 1):
        r = _refresh_one_case(cause, client)
        totals["cases"] += 1
        totals["pcs"] += r["pcs_downloaded"]
        if r["plea_captured"]:
            totals["pleas"] += 1
        if r["error"]:
            totals["errors"] += 1
            print(f"  [{i}/{len(causes)}] {cause}: ERROR {r['error']}", file=sys.stderr)
        else:
            print(f"  [{i}/{len(causes)}] {cause}: PCs +{r['pcs_downloaded']}, plea {'Y' if r['plea_captured'] else '-'}")
        storage.record_run_result(
            mode="backfill-all",
            cause_number=cause,
            pcs_downloaded=r["pcs_downloaded"],
            plea_captured=r["plea_captured"],
            error=r["error"],
        )
        if args.rate_limit_seconds > 0 and i < len(causes):
            time.sleep(args.rate_limit_seconds)

    print(f"\nDONE — {totals['cases']} cases, {totals['pcs']} new PCs, {totals['pleas']} pleas captured, {totals['errors']} errors")
    return 0 if totals["errors"] == 0 else 2


def cmd_backfill(args: argparse.Namespace) -> int:
    log.warning(
        "backfill subcommand is a v1 stub. Full implementation requires "
        "AppleScript Mail.app iteration to enumerate emails in the date "
        "range, which is the next milestone. For now, run process-email "
        "manually by piping a few historical email bodies via stdin."
    )
    print(f"STUB — would scan emails from {args.from_date} to {args.to_date or 'today'} (rate={args.rate_limit})")
    return 0


# ----- helpers -----


def _resolve_case(cause_number: str) -> dict | None:
    """Resolve a cause-number to its TechShare routing info.

    Fallback chain:
      1. Env vars VOXHORA_TECHSHARE_CASE_UUID + VOXHORA_TECHSHARE_SERVICE_ID
         (manual overrides for testing or single-shot calls)
      2. case_uuid_cache.json — populated by the periodic scrape of
         /Ember/Cases (see Voxhora handoff doc 2026-05-25 for seed)

    Returns a dict {case_uuid, service_id, backend_port} or None.
    """
    import os
    env_uuid = os.environ.get("VOXHORA_TECHSHARE_CASE_UUID")
    env_sid = os.environ.get("VOXHORA_TECHSHARE_SERVICE_ID")
    if env_uuid and env_sid:
        # Infer port from service id; fall back to env override
        port = int(os.environ.get("VOXHORA_TECHSHARE_BACKEND_PORT") or 0) or None
        if port is None:
            port = 1030 if env_sid == config.SERVICE_TRAVIS_COUNTY_ATTORNEY else 1031
        return {"case_uuid": env_uuid, "service_id": env_sid, "backend_port": port}
    return storage.lookup_case(cause_number)


if __name__ == "__main__":
    sys.exit(main())
