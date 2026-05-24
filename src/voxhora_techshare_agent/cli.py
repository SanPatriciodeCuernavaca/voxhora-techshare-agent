"""argparse-based CLI for voxhora-techshare-agent.

Subcommands:

  login            store TechShare credentials in macOS Keychain
  status           diagnostic: session valid? CSRF fresh? last run?
  process-email    read email body from stdin, process one event
  fetch            on-demand bulk discovery pull for ONE case (videos + all DME)
  backfill         one-time scan over a date range (NOT YET WIRED — needs
                   AppleScript Mail.app iteration; v1 stub)

Each subcommand wires TechShareSession + TechShareClient + storage together.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
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
        log.error(
            "Cannot resolve case %s — not in cause→UUID cache. Run "
            "`voxhora-techshare-agent refresh-cases` after logging in to "
            "TechShare, or pass VOXHORA_TECHSHARE_CASE_UUID + "
            "VOXHORA_TECHSHARE_SERVICE_ID env vars.",
            event.cause_number,
        )
        return 4
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

    session = TechShareSession()
    session.ensure_authenticated()
    client = TechShareClient(session)

    case = client.get_case_detail(service_id, case_uuid)
    dme_items = client.get_dme_list(service_id, case)
    log.info("fetch %s: %d DME items total", cause_number, len(dme_items))

    seen = storage.load_seen_dme_ids()
    new_downloads = 0
    for item in dme_items:
        fp = storage.dme_fingerprint(item)
        if fp in seen:
            log.info("skip (already-seen): %s", item.name)
            continue
        if item.is_archived:
            log.info("skip (archived): %s", item.name)
            continue
        data = client.download_dme_file(service_id, item)
        if item.is_pc_affidavit:
            storage.write_pc_affidavit(item, data, cause_number)
        else:
            storage.write_case_discovery_file(item, data, cause_number)
        seen.add(fp)
        new_downloads += 1

    storage.save_seen_dme_ids(seen)
    print(f"OK — {cause_number}: {new_downloads} new files downloaded ({len(dme_items)} total in case)")
    return 0


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
