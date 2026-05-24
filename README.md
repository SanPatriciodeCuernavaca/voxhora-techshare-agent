# voxhora-techshare-agent

Standalone Python utility that automates TechShare discovery retrieval for Travis County criminal defense attorneys. Pairs with [Voxhora](../voxhora-ios) (legal-billing app) to deliver PC affidavits + plea offers to the attorney's iPhone in near-realtime.

**Status**: v1 development (2026-05-25). Patrick-only single-user CLI. Multi-tenant productization gated on Travis County greenlight.

---

## What it does

Watches `TechShareProsecutor@traviscountytx.gov` emails in Mail.app. When a discovery email arrives:

1. Parses the email body to extract cause-number + event type
2. Hits TechShare's `/api/proxy` with the case-detail backend URL
3. Walks the DME (Discovery Media Evidence) list
4. Downloads any new **PC Affidavit / Arrest Warrant** PDFs
5. Captures the latest plea offer text (if present)
6. Writes results into `~/Dropbox/Voxhora/Bulk_Inbox/` for Voxhora's AutoIntakeWatcher to pick up
7. Green-flags the email so subsequent polls skip it

The agent **never** auto-downloads videos, audio, or non-PC discovery — those are user-initiated via the Voxhora-Mac case-view button (`fetch <cause-number>` subcommand).

## Subcommands

```bash
voxhora-techshare-agent process-email      # read email body from stdin, process one event
voxhora-techshare-agent backfill --from <date> --to <date>  # one-time scan over date range
voxhora-techshare-agent fetch <cause-number>  # on-demand bulk pull for ONE case (videos + everything)
voxhora-techshare-agent login              # store TechShare credentials in macOS Keychain
voxhora-techshare-agent status             # diagnostic: session valid? CSRF fresh? last run?
```

## Architecture decisions (locked 2026-05-25)

- **Email-triggered, not polling** — TechShare emails Patrick when new evidence is shared. Agent reacts to those, doesn't bulk-poll TechShare. Near-zero ongoing audit footprint.
- **PC affidavits only auto-download** — everything else is user-initiated.
- **State per user** under `~/Library/Application Support/voxhora-techshare-agent/<username>/` (cookies pickle, last-run timestamps, seen-DME IDs). Designed for multi-tenant v2 from day one.
- **Credentials in macOS Keychain** via `keyring`, never in env vars or config files.
- **No video transcoding, no OCR** — agent is a downloader; Voxhora handles content processing in its existing Phase 1 pipeline.

## Endpoints (verified live 2026-05-25)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/csrf-token` | Returns `{token: "<108-char string>"}` |
| POST | `/api/proxy` | Backend proxy. Body: `{externalServiceId, Method, Path}` |
| POST | `/api/auditentry` | Audit log (logged automatically by TechShare on each proxy call) |
| GET | `/api/notifications` | Long-poll push channel (30-42s hold) |
| GET | `/api/service` | User's service scopes |

Service UUIDs:
- Travis County County Attorney: `598994d8-48eb-457f-b5fe-3f97e5072ecb` (backend port 1030)
- Travis County District Attorney: `b278d33e-7e14-4abd-be16-6f22135d9193` (backend port 1031)

Full recon: see `~/Obsidian/Voxhora/Voxhora - TechShare Recon 2026-05-25.md`.

## Setup

```bash
cd ~/voxhora-techshare-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
voxhora-techshare-agent login    # stores credentials in macOS Keychain
voxhora-techshare-agent status   # verifies session
```

## Voxhora integration

Voxhora-Mac's `AutoIntakeWatcher` is configured (in Settings → Auto-intake folders) to watch `~/Dropbox/Voxhora/Bulk_Inbox/`. The agent writes PC affidavit PDFs there; AutoIntakeWatcher's existing Phase 1 pipeline OCRs them, runs Haiku synopsis, attaches to the matching Case in SwiftData, syncs to iPhone/iPad via CloudKit.

Plea offer text is written to `~/Dropbox/Voxhora/Bulk_Inbox/<cause-number>.plea.json` for a separate Voxhora-Mac ingestor that populates `Case.latestPleaOfferText`. (Ingestor TBD — pending Voxhora-Mac side of v1 build.)

## License

Private — Patrick Fagerberg, San Patricio de Cuernavaca LLC.
