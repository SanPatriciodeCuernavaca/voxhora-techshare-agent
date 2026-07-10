# AGENTS.md — voxhora-techshare-agent

Python agent that logs into TechShare (the Texas DA evidence portal) and downloads
PC affidavits, plea offers, and discovery files for the Voxhora Mac app. **Scope is
deliberately PC/discovery-download ONLY — it contains ZERO LLM calls** (synopsis
generation happens in the Swift app). Subcommand + manifest/exit-code interface is
the contract with the app; treat it as a public API.

**Full onboarding for a new agent: read the private repo
`SanPatriciodeCuernavaca/voxhora-exit-kit` (README first).**

## Test & run

```bash
pytest                      # full suite; must stay green
python -m voxhora_techshare_agent --help
```

Credentials live in the system keyring (slot shared with the app's
`TechShareKeychainHelper`) — never in files or argv.

## Ship path (IMPORTANT)

This repo is embedded into the Mac app: `voxhora-mac/scripts/bundle_techshare_agent.sh`
takes a git-archive of HEAD + wheels the deps + signs every nested Mach-O, and the
Mac app installs/updates it via Sparkle releases. **Landing a change here does
NOTHING for users until the next Mac release is cut with the kit rebuilt.** Patrick's
own Mac runs this checkout directly (dev fallback); everyone else gets the
signature-verified embedded copy.

## Hard-won invariants (do not regress)

- **Session death is normal, not an error:** every retry path re-authenticates the
  TechShare session first (`session.reauthenticate()`); mid-batch 401s heal.
- **Bulk downloads: smallest-files-first**, so a session death can only cost the
  big tail.
- **Resume, never restart, giant files:** Range-resume from the kept `.partial`
  appends only on a provable 206 + matching Content-Range (the DME host drops
  streams at ~16–20 min; >2 GB videos can NEVER finish from byte zero).
- **Honest per-file failure reporting:** `FAILED-ITEMS-JSON` after the OK summary +
  `failed_items` in `_manifest.json`. The app renders exactly that list to the
  lawyer. No silent skips, ever.
- **Exit-code contract:** 0 = OK, 2 = not on TechShare / auth-verify failure,
  4 = non-Travis. The Swift side buckets on these — don't renumber.
- PC-type matching uses the structured `type` field (tolerant matcher), never
  filename/OCR guessing.
- Cause numbers arrive NORMALIZED (dashes stripped, e.g. `C1CR25212217`) — the
  canonical join key with the app.
- TechShare blocks datacenter IPs — this agent only works from a residential
  connection (that's why it lives on the attorney's Mac, not a server).
