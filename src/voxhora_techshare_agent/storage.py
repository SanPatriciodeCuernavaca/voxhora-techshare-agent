"""Filesystem persistence: atomic writes, seen-DME-id dedup, plea-offer JSON output.

Everything the agent writes is meant to be safely re-runnable. Atomic-write
semantics (write to .tmp + rename) avoid leaving half-written files if the
process is killed mid-download.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
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


def extract_zip_inplace(zip_path: Path) -> int:
    """Extract a ZIP archive into a SUBFOLDER named after it (2026-07-04),
    flattening the archive's internal directory structure inside that
    subfolder. Returns the number of files extracted, or 0 on corruption /
    extraction failure (caller decides what to do — typically log + leave
    the ZIP intact).

    Patrick 2026-05-27 — TechShare delivers photo bundles as ZIP
    archives. Voxhora's Discovery Portal viewer (PDFKit/AVPlayer/audio
    transport) doesn't read archives — the user sees a dead ZIP icon.
    Solution: extract on download. The original ZIP is renamed to
    `.<filename>` (hidden) so the audit chain keeps the original artifact.

    Patrick 2026-07-04 — extraction goes into `<case folder>/<zip stem>/`
    now, NEVER loose into the case folder. The loose flatten was built
    for 26-photo bundles and buried Richardson's 41 real evidence files
    under 1,631 extracted ones when the county shipped a 1.5 GB
    officer-records ZIP (a whole Windows video-player app: 238 DLLs,
    451 help pages, plus real record PDFs). The Portal's local scanner
    renders a subfolder as ONE bundle row ("each bundle should be one
    file"); cloud-mode listing currently skips subfolders (bundle
    collapse queued) — the files stay reachable via Finder/Dropbox.

    Collision handling INSIDE the subfolder (two members in different
    internal dirs sharing a basename): numeric suffixes — never
    overwrite. (The old prefix-once logic silently overwrote on a
    second collision; 427 of Richardson's members were lost that way,
    recoverable only because the original ZIP is preserved.)
    """
    subdir = zip_path.parent / zip_path.stem
    extracted = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [m for m in zf.namelist() if not m.endswith("/") and Path(m).name]
            if members:
                subdir.mkdir(exist_ok=True)
            for member in members:
                name = Path(member).name
                target = subdir / name
                counter = 2
                while target.exists():
                    stem, dot, ext = name.rpartition(".")
                    target = subdir / (f"{stem}_{counter}.{ext}" if dot else f"{name}_{counter}")
                    counter += 1
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted += 1
                # 2026-07-04 — Outlook emails get a readable .txt companion
                # so the Portal can display them (the .msg itself is a
                # binary format no in-app viewer renders).
                if target.suffix.lower() == ".msg":
                    convert_msg_to_text(target)
    except zipfile.BadZipFile:
        log.warning("ZIP corrupted, leaving intact: %s", zip_path.name)
        return 0
    except Exception as e:
        log.error("ZIP extract failed for %s: %s", zip_path.name, e)
        return 0
    return extracted


def convert_msg_to_text(msg_path: Path) -> Path | None:
    """Write a readable `<name>.msg.txt` companion next to an Outlook
    .msg file (2026-07-04, Patrick: "I want to be able to read the
    emails in the viewer"). .msg is a binary OLE format nothing in the
    Portal can render; .txt opens in the existing QuickLook viewer.
    Companion carries the headers + plain-text body. Attachments inside
    the .msg are NOT extracted (noted in the companion footer; the
    original .msg stays on disk for Outlook/Finder). Returns the
    companion path, or None on any failure (never raises — a bad email
    must not break a ZIP extraction).
    """
    try:
        import extract_msg  # lazy — optional dependency
    except ImportError:
        log.warning("extract_msg not installed; skipping .msg conversion for %s", msg_path.name)
        return None
    try:
        msg = extract_msg.openMsg(str(msg_path))
        try:
            body = (msg.body or "").strip()
            attachment_names = [
                getattr(a, "longFilename", None) or getattr(a, "shortFilename", None) or "(unnamed)"
                for a in (msg.attachments or [])
            ]
            lines = [
                f"Subject: {msg.subject or '(no subject)'}",
                f"From:    {msg.sender or '(unknown)'}",
                f"To:      {msg.to or '(unknown)'}",
            ]
            if msg.cc:
                lines.append(f"Cc:      {msg.cc}")
            lines.append(f"Date:    {msg.date or '(unknown)'}")
            lines.append("-" * 60)
            lines.append(body if body else "(no plain-text body in this email)")
            if attachment_names:
                lines.append("")
                lines.append("-" * 60)
                lines.append(f"[{len(attachment_names)} attachment(s) inside the original .msg — "
                             "open it in Outlook/Finder to get them]")
                lines.extend(f"  • {n}" for n in attachment_names)
            target = msg_path.with_name(msg_path.name + ".txt")
            target.write_text("\n".join(lines), encoding="utf-8")
            return target
        finally:
            msg.close()
    except Exception as e:
        log.warning(".msg conversion failed for %s: %s", msg_path.name, e)
        return None


def hide_zip_after_extract(zip_path: Path) -> Path | None:
    """Rename `Photos.zip` → `.Photos.zip` so DiscoveryFolderScanner's
    `.skipsHiddenFiles` enumerator skips it from the Portal grid while
    preserving bytes for audit. Returns the new hidden path, or None
    if rename failed.
    """
    hidden = zip_path.parent / f".{zip_path.name}"
    if hidden.exists():
        # Already hidden version from a prior fetch — remove the new
        # extracted ZIP rather than overwriting (audit-safer).
        try:
            zip_path.unlink()
        except Exception as e:
            log.warning("couldn't remove new ZIP %s (existing hidden present): %s",
                        zip_path.name, e)
        return hidden
    try:
        zip_path.rename(hidden)
        return hidden
    except Exception as e:
        log.warning("couldn't hide ZIP %s after extract: %s", zip_path.name, e)
        return None


# ----- video conversion (Portal playability) -----

# Container/codec wrappers AVFoundation (the Portal's AVPlayer + QuickTime)
# refuses outright. Surveillance exporters love these. mp4/m4v/mov/3gp are
# native and never touched.
PORTAL_UNPLAYABLE_VIDEO_EXTS = {
    ".avi", ".wmv", ".flv", ".mkv", ".mpg", ".mpeg", ".m2ts", ".mts",
    ".ts", ".vob", ".asf", ".webm", ".divx", ".3g2",
}


def is_portal_unplayable_video(path: Path) -> bool:
    return path.suffix.lower() in PORTAL_UNPLAYABLE_VIDEO_EXTS


def find_ffmpeg() -> str | None:
    """Locate an ffmpeg binary: PATH first, then Homebrew's usual homes,
    then the static binary bundled by the imageio-ffmpeg wheel (shipped
    in the agent kit so attorneys' Macs need no Homebrew)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(candidate):
            return candidate
    try:
        import imageio_ffmpeg  # lazy — optional dependency

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def convert_video_to_playable(video_path: Path) -> Path | None:
    """Write a Portal-playable `<name>.mp4` next to a video the Portal's
    AVPlayer can't open, then dot-prefix-hide the original (bytes
    preserved for the audit chain, same rule as ZIPs).

    Patrick 2026-07-11 — Damian Jimenez Hernandez's TechShare discovery
    was 9 store-surveillance AVIs (H.264 inside an AVI wrapper written
    so nonstandard that even a lossless remux fails); QuickTime and the
    Portal both refused them. Broad fix per Patrick: EVERY discovery
    video converts to a viewable format when it lands.

    Encoder ladder (each attempt verified by exit 0 + a real file):
      1. h264_videotoolbox + aac  — Apple hardware encoder, ~10x faster
      2. libx264 + aac            — always present in bundled ffmpeg
      3. libx264, audio dropped   — salvages video when audio is corrupt
    Never raises; on total failure the original stays visible and the
    caller just logs (a bad video must not fail the fetch item).
    Returns the .mp4 path, or None.

    Evidence-integrity rules (hardened 2026-07-11 after adversarial review):
    - Encode to a unique temp in the same dir, then os.replace into place —
      a killed encode never leaves a half-written `<name>.mp4` that a later
      run would trust as finished.
    - Do NOT adopt a pre-existing `<stem>.mp4` sibling as our output. The
      seen-set is the real cross-run idempotency guard; a sibling mp4 that's
      already here for a fresh item is an UNRELATED file (different camera),
      so we must never rename its neighbour away or point the manifest at
      its bytes. Leave the original visible and bail.
    - One shared wall-clock deadline across the whole ladder, so a
      pathological video can't stall the single-threaded fetch for 3×timeout.
    - The whole body is guarded: nothing escapes to fail the fetch item.
    """
    try:
        if not is_portal_unplayable_video(video_path):
            return None
        target = video_path.with_suffix(".mp4")
        if target.exists():
            # Not necessarily ours — could be an unrelated same-stem native
            # file from another camera. Never clobber/adopt it: leave the
            # original visible and unconverted (rare; evidence preserved).
            log.warning("a %s already exists next to %s — leaving original "
                        "unconverted to avoid adopting unrelated bytes",
                        target.name, video_path.name)
            return None
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            log.warning("no ffmpeg available; %s stays unconverted", video_path.name)
            return None
        import subprocess  # lazy — only this path needs it

        tmp = target.with_name(f".{target.name}.converting")
        base = [ffmpeg, "-y", "-v", "error", "-err_detect", "ignore_err",
                "-fflags", "+genpts", "-i", str(video_path)]
        # `-f mp4` is REQUIRED: the temp path ends in `.converting`, so ffmpeg
        # can't infer the muxer from the extension the way it could for a
        # literal `.mp4` output.
        tail = ["-movflags", "+faststart", "-f", "mp4", str(tmp)]
        attempts = [
            ["-c:v", "h264_videotoolbox", "-b:v", "6M", "-pix_fmt", "yuv420p", "-c:a", "aac"],
            ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac"],
            ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-an"],
        ]
        size_mb = max(1, video_path.stat().st_size // (1024 * 1024))
        deadline = min(3600, max(300, size_mb * 6))  # shared across the ladder
        import time
        started = time.monotonic()
        ok = False
        for codec_args in attempts:
            remaining = deadline - (time.monotonic() - started)
            if remaining <= 0:
                log.warning("conversion deadline (%ss) exhausted for %s",
                            deadline, video_path.name)
                break
            try:
                result = subprocess.run(
                    base + codec_args + tail,
                    capture_output=True, timeout=remaining,
                )
                if result.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1024:
                    ok = True
                    break
            except subprocess.TimeoutExpired:
                log.warning("conversion timed out for %s", video_path.name)
            except Exception as e:
                log.warning("conversion attempt errored for %s: %s", video_path.name, e)
            _safe_unlink(tmp)
        if not ok:
            _safe_unlink(tmp)
            log.warning("all conversion attempts failed for %s; original left visible",
                        video_path.name)
            return None
        # Atomically place the finished mp4, then hide the original so the
        # grid shows ONE playable file (audit keeps the original bytes).
        os.replace(tmp, target)
        hidden = video_path.parent / f".{video_path.name}"
        try:
            if hidden.exists():
                # A hidden original from a prior run is already preserved —
                # remove the redundant re-downloaded visible copy (mirrors
                # hide_zip_after_extract's audit-safe dedup).
                _safe_unlink(video_path)
            else:
                video_path.rename(hidden)
        except Exception as e:
            log.warning("couldn't hide original %s after convert: %s", video_path.name, e)
        return target
    except Exception as e:  # fail-soft: a bad video must never fail the fetch item
        log.warning("video conversion crashed for %s: %s", getattr(video_path, "name", video_path), e)
        return None


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except Exception:
        pass


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
