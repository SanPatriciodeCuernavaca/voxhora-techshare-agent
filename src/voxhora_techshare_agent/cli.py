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
    fc.add_argument("--service-id", default=None, help="Service UUID (default: auto-detect from cause prefix)")
    fc.add_argument("--case-uuid", required=True, help="TechShare case UUID")

    bf = sub.add_parser("backfill", help="One-time scan over a date range (stub in v1).")
    bf.add_argument("--from", dest="from_date", required=True, help="ISO date e.g. 2026-01-01")
    bf.add_argument("--to", dest="to_date", default=None, help="ISO date (default: today)")
    bf.add_argument("--rate-limit", default="max", help="'max' or e.g. '1/sec'")

    args = parser.parse_args(argv)

    dispatch = {
        "login": cmd_login,
        "status": cmd_status,
        "process-email": cmd_process_email,
        "fetch": cmd_fetch,
        "backfill": cmd_backfill,
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
    print(f"Session authenticated: {authed}")
    print(f"State dir:            {config.state_dir()}")
    print(f"Cookies file:         {config.cookies_path()}")
    print(f"Seen DME cache:       {config.seen_dme_path()}")
    print(f"Bulk inbox:           {config.dropbox_inbox()}")
    print(f"Last-run log:         {config.last_run_path()}")
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

    service_id = _service_id_for_cause(event.cause_number)

    # Backfill mode + steady-state both work the same here: caller of
    # process-email is responsible for the case-UUID lookup. For v1 we need
    # to call search_cases() — which is currently NotImplementedError. The
    # email-driven flow assumes the MailInboxWatcher side (Voxhora-Mac) has
    # already cross-referenced cause-number → case-UUID via Voxhora's local
    # SwiftData store and passes it in via an env var.
    case_uuid = _resolve_case_uuid(event.cause_number)
    if not case_uuid:
        log.error(
            "Cannot resolve case UUID for cause %s. v1 expects the caller "
            "to pre-resolve via Voxhora's SwiftData store and pass via env "
            "var VOXHORA_TECHSHARE_CASE_UUID. (search_cases not yet "
            "implemented; tracked as follow-up.)",
            event.cause_number,
        )
        return 4

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
    service_id = args.service_id or _service_id_for_cause(cause_number)
    case_uuid = args.case_uuid

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


def _service_id_for_cause(cause_number: str) -> str:
    """Map cause-number prefix to the right Travis County service scope."""
    if cause_number.startswith("C1CR"):
        return config.SERVICE_TRAVIS_COUNTY_ATTORNEY
    if cause_number.startswith("D1DC"):
        return config.SERVICE_TRAVIS_DISTRICT_ATTORNEY
    raise ValueError(
        f"Cannot infer service scope from cause number {cause_number!r}; "
        f"pass --service-id explicitly."
    )


def _resolve_case_uuid(cause_number: str) -> str | None:
    """v1 placeholder: read from env var VOXHORA_TECHSHARE_CASE_UUID.

    Voxhora-Mac's TechShareEmailParser will pre-resolve cause-number →
    case-UUID by looking up the matching Case in SwiftData (Voxhora stores
    the UUID on the Case row once it's been seen). For the very first
    encounter, an HTML-scrape of /Ember/Cases will be needed (TBD).
    """
    import os
    return os.environ.get("VOXHORA_TECHSHARE_CASE_UUID")


if __name__ == "__main__":
    sys.exit(main())
