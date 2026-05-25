#!/bin/bash
# voxhora-techshare-agent — flag-existing-techshare-emails.sh
#
# Run ONCE before enabling MailInboxWatcher's autonomous TechShare mode.
# Walks every existing email from TechShareProsecutor@traviscountytx.gov
# and flags it green — Voxhora's "already processed, skip me" marker.
# After this runs, only NEW TechShare emails arriving going forward will
# be processed by the agent (because they'll be unflagged).
#
# Why this matters: Patrick has thousands of historical TechShare
# notifications. Without this pre-flag, the first MailInboxWatcher tick
# would try to process every single one — a massive (and unnecessary)
# audit-footprint spike on TechShare, plus redundant work (backfill-all
# already covered the substantive state).
#
# Safe to re-run. Already-green emails are counted but not re-flagged.

set -euo pipefail

echo "=== Voxhora — bootstrap-flag existing TechShare emails ==="
echo
echo "About to walk every email in Mail.app from"
echo "TechShareProsecutor@traviscountytx.gov and set the green flag"
echo "on each unflagged one. This is one-time prep before the"
echo "autonomous Mail.app watcher kicks in."
echo
echo "Mail.app must be running and signed in to the account that"
echo "receives TechShare notifications."
echo
read -r -p "Proceed? [y/N] " answer
case "${answer}" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 0;;
esac

echo
echo "Running AppleScript (may take a minute on large inboxes)..."
echo

osascript <<'APPLESCRIPT'
tell application "Mail"
    with timeout of 3600 seconds
        set targetSender to "techshareprosecutor@traviscountytx.gov"
        set flaggedCount to 0
        set alreadyGreenCount to 0
        set errorCount to 0
        try
            set matchingMsgs to (every message of inbox whose sender contains targetSender)
            repeat with msg in matchingMsgs
                set msgIsGreen to false
                try
                    if (flagged status of msg) is true and (flag index of msg) is 3 then
                        set msgIsGreen to true
                    end if
                end try
                if msgIsGreen then
                    set alreadyGreenCount to alreadyGreenCount + 1
                else
                    try
                        -- ORDER MATTERS: status first, then index.
                        set flagged status of msg to true
                        set flag index of msg to 3
                        set flaggedCount to flaggedCount + 1
                    on error
                        set errorCount to errorCount + 1
                    end try
                end if
            end repeat
        on error errMsg
            return "SCAN_ERROR: " & errMsg
        end try
        return "OK | newly-flagged=" & flaggedCount & " | already-green=" & alreadyGreenCount & " | errors=" & errorCount
    end timeout
end tell
APPLESCRIPT

echo
echo "Done. Now safe to enable MailInboxWatcher's autonomous mode:"
echo "  - Quit Voxhora-Mac"
echo "  - Relaunch from ~/Applications or DerivedData"
echo "  - Within 60 sec the watcher will tick; with all historicals"
echo "    flagged green, it'll find 0 unflagged and skip silently."
echo "  - Going forward, ONLY new TechShare emails arriving fresh in"
echo "    your inbox will trigger the agent's process-email pipeline."
