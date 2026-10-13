"""Written discovery downloads itself; audio/video waits for a person (2026-10-12).

`fetch --only written` never downloads an item TechShare labels Video/Audio,
never an audio/video file type, and nothing large, and says what it left
behind (NOT-FETCHED-JSON). (A small ZIP not labelled Video/Audio is fetched and
unpacked even if it happens to hold a clip — Patrick's "small ZIPs" ruling.) TechShare
discovery emails leave a cause-only event in the spool for the Mac app, before
any network call. Plan: vault "PLAN 2026-10-12 Auto written discovery".
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
from pathlib import Path

import pytest

from voxhora_techshare_agent import cli, storage
from voxhora_techshare_agent.models import DMEItem


def _item(name: str, type_: str, size: str, dme_id: str = "") -> DMEItem:
    dme_id = dme_id or f"{abs(hash((name, type_, size))):x}"
    return DMEItem(
        name=name, type=type_, source="Government", size=size,
        available_date="2026-10-12", last_accessed_date=None, is_archived=False,
        enclosure_href=f"https://x/dmefile?dmeId={dme_id}&isStream=1", api_href=None,
    )


# ---------------------------------------------------------------- what is "written"

@pytest.mark.parametrize("name,type_,size,expected", [
    ("Offense Report.pdf", "Offense Report", "145 KB", True),
    ("Supplement.docx", "Supplemental Case Info", "40 KB", True),
    ("Scene 1.jpg", "Photographs", "2,100 KB", True),           # Patrick: photos yes
    ("Photos.pdf", "Photographs", "900 KB", True),
    ("Disclosure.zip", "Disclosure File", "851 KB", True),        # Patrick: small ZIPs yes
    ("Disclosure big.zip", "Disclosure File", "60,000 KB", False),  # over the ZIP cap
    ("Supplemental.zip", "Supplemental Case Info", "18,900,000 KB", False),  # the 18.9 GB one
    ("In car.zip", "Video - In Car", "900 KB", False),             # label veto
    ("911 call.msg", "Audio - 911 Call", "30 KB", False),          # label veto beats extension
    ("Report.pdf", "Video - BodyCam", "100 KB", False),            # label veto
    ("Huge scan.pdf", "Offense Report", "150,000 KB", False),      # over the 100 MB cap
    ("Axon_Body.mp4", "Video - BodyCam", "1,282,011 KB", False),
    ("Jail call.wma", "Handle By", "4,000 KB", False),             # audio by type, not label
    ("Mystery", "Other", "10 KB", False),                          # no extension
    ("Report.pdf", "Offense Report", "", False),                   # unknown size
    ("Email.msg", "State's Correspondence (Discoverable)", "30 KB", True),
    ("C1CR26000000.pdf", "PC Affidavit / Arrest Warrant", "50 KB", True),
])
def test_is_auto_written(name, type_, size, expected):
    assert cli._is_auto_written(_item(name, type_, size)) is expected


# ---------------------------------------------------------------- fetch --only written

class _Session:
    def ensure_authenticated(self):
        pass


class _Client:
    def __init__(self, items):
        self.items = items
        self.downloaded: list[str] = []

    def get_case_detail(self, service_id, case_uuid):
        return {"uuid": case_uuid}

    def get_dme_list(self, service_id, case):
        return self.items

    def pc_affidavits_in(self, items):
        return [i for i in items if i.is_pc_affidavit]

    def download_dme_file_to_path(self, service_id, item, path):
        self.downloaded.append(item.name)
        low = storage.expected_size_range(item)[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * low)
        return low


def _run_fetch(monkeypatch, tmp_path, items, only):
    client = _Client(items)
    monkeypatch.setattr(cli, "_resolve_case", lambda c: {"service_id": "svc", "case_uuid": "uuid"})
    monkeypatch.setattr(cli, "TechShareSession", lambda: _Session())
    monkeypatch.setattr(cli, "TechShareClient", lambda s: client)
    monkeypatch.setattr(storage, "case_discovery_target_path",
                        lambda it, cause, target_dir=None: tmp_path / "case" / it.name)
    monkeypatch.setattr(storage, "is_present_in_portal", lambda it, cause: False)
    rc = cli.cmd_fetch(argparse.Namespace(
        cause_number="C1CR26000000", service_id="svc", case_uuid="uuid",
        item_ids=None, target_dir=str(tmp_path / "case"), manifest=None, only=only,
    ))
    return rc, client


CASE = [
    _item("Offense Report.pdf", "Offense Report", "145 KB", "a1"),
    _item("Scene 1.jpg", "Photographs", "2,100 KB", "a2"),
    _item("Disclosure.zip", "Disclosure File", "1 KB", "a3"),
    _item("Axon_Body.mp4", "Video - BodyCam", "6,000 KB", "a4"),
    _item("Officer files.zip", "Supplemental Case Info", "70,000 KB", "a5"),
    _item("911 call.wav", "Audio - 911 Call", "500 KB", "a6"),
]


def _not_fetched(capsys):
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("NOT-FETCHED-JSON: "):
            return json.loads(line[len("NOT-FETCHED-JSON: "):])
    return None


def test_written_only_never_downloads_audio_or_video(tmp_path, monkeypatch, capsys):
    rc, client = _run_fetch(monkeypatch, tmp_path, CASE, "written")
    assert sorted(client.downloaded) == ["Disclosure.zip", "Offense Report.pdf", "Scene 1.jpg"]
    assert rc == 0
    assert _not_fetched(capsys) == {
        "video": {"count": 1, "bytes": 6_000_000},
        "bundle": {"count": 1, "bytes": 70_000_000},
        "audio": {"count": 1, "bytes": 500_000},
    }


def test_written_only_leaves_the_rest_unseen_for_the_full_fetch(tmp_path, monkeypatch, capsys):
    _run_fetch(monkeypatch, tmp_path, CASE, "written")
    seen = storage.load_seen_dme_ids()
    assert storage.dme_fingerprint(CASE[0]) in seen
    assert storage.dme_fingerprint(CASE[3]) not in seen, "the video must still come down on the button"


def test_full_fetch_unchanged(tmp_path, monkeypatch, capsys):
    """An app that doesn't pass --only (every app before this one) gets
    exactly what it always got: everything, and no NOT-FETCHED line."""
    _, client = _run_fetch(monkeypatch, tmp_path, CASE, None)
    assert sorted(client.downloaded) == sorted(i.name for i in CASE)
    assert _not_fetched(capsys) is None


def test_not_fetched_leaves_out_what_is_already_in_the_portal(tmp_path, monkeypatch, capsys):
    storage.save_seen_dme_ids({storage.dme_fingerprint(CASE[3])})    # the video, downloaded earlier
    monkeypatch.setattr(storage, "is_present_in_portal", lambda it, cause: it.name == "Axon_Body.mp4")
    summary = cli._not_fetched_summary([CASE[3], CASE[5]], storage.load_seen_dme_ids(), "C1CR26000000")
    assert summary == {"audio": {"count": 1, "bytes": 500_000}}


def test_case_with_no_audio_or_video_prints_an_empty_summary(tmp_path, monkeypatch, capsys):
    _run_fetch(monkeypatch, tmp_path, CASE[:3], "written")
    assert _not_fetched(capsys) == {}


def test_only_accepts_written_and_help_advertises_it(capsys):
    """The Mac app checks `fetch --help` for --only before it ever asks for a
    written-only run, and an old kit rejects the flag before any network call."""
    with pytest.raises(SystemExit):
        cli.main(["fetch", "C1CR26000000", "--only", "video"])
    with pytest.raises(SystemExit):
        cli.main(["fetch", "--help"])
    assert "--only" in capsys.readouterr().out


def test_standalone_msg_gets_a_readable_companion(tmp_path, monkeypatch, capsys):
    converted = []
    monkeypatch.setattr(storage, "convert_msg_to_text", lambda p: converted.append(p.name))
    _run_fetch(monkeypatch, tmp_path,
               [_item("Email.msg", "State's Correspondence (Discoverable)", "30 KB", "m1")], "written")
    assert converted == ["Email.msg"]


# ---------------------------------------------------------------- spool for the Mac app

DME_BODY = (Path(__file__).parent / "fixtures" / "email_dme.txt").read_text()


class _Boom:
    def __init__(self, *a, **k):
        raise RuntimeError("TechShare is down")


def _events():
    path = storage.discovery_events_path()
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_discovery_email_is_spooled_before_any_network_call(monkeypatch):
    monkeypatch.setattr(cli, "TechShareSession", _Boom)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(DME_BODY))
    with pytest.raises(RuntimeError):
        cli.cmd_process_email(argparse.Namespace(subject=None, subject_file=None))
    events = _events()
    assert [(e["cause"], e["kind"]) for e in events] == [("C1CR25208232", "dme_discoverable")]


def test_attorney_of_record_is_spooled(monkeypatch):
    monkeypatch.setattr(cli, "_process_attorney_of_record", lambda event: 0)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("You are now attorney of record."))
    cli.cmd_process_email(argparse.Namespace(
        subject="Attorney of record in case # D1DC26300000 (DOE, JANE)", subject_file=None))
    assert [(e["cause"], e["kind"]) for e in _events()] == [("D1DC26300000", "attorney_of_record")]


def test_spool_never_carries_a_name(monkeypatch):
    monkeypatch.setattr(cli, "_process_attorney_of_record", lambda event: 0)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("You are now attorney of record."))
    cli.cmd_process_email(argparse.Namespace(
        subject="Attorney of record in case # D1DC26300000 (DOE, JANE)", subject_file=None))
    text = storage.discovery_events_path().read_text()
    assert "DOE" not in text and "JANE" not in text
    assert set(json.loads(text.splitlines()[0])) == {"cause", "kind", "ts"}


def test_other_emails_are_not_spooled(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("A Case has been Disposed. Case Number: C1CR26000001"))
    monkeypatch.setattr(cli, "_resolve_case", lambda c: {"service_id": "s", "case_uuid": "u"})
    cli.cmd_process_email(argparse.Namespace(subject="A Case has been Disposed", subject_file=None))
    assert _events() == []


def test_spool_is_bounded():
    for i in range(storage._SPOOL_MAX_LINES + 50):
        storage.append_discovery_event(f"C1CR26{i:06d}", "dme_discoverable")
    events = _events()
    assert len(events) == storage._SPOOL_MAX_LINES
    assert events[-1]["cause"] == f"C1CR26{storage._SPOOL_MAX_LINES + 49:06d}", "keeps the newest"


# ---------------------------------------------------------------- cause number line

def test_case_number_line_beats_an_earlier_incident_number():
    from voxhora_techshare_agent.email_parser import parse_email_body
    body = DME_BODY.replace("Incident Number: C1CR25208232", "Incident Number: C1CR25999999")
    assert parse_email_body(body).cause_number == "C1CR25208232"


# ---------------------------------------------------------------- quiet stderr when piped

def test_piped_stderr_gets_warnings_not_info(capsys):
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    try:
        for h in [h for h in root.handlers if getattr(h, "_voxhora_stderr", False)]:
            root.removeHandler(h)
        cli._configure_stderr_logging()          # capsys' stderr is not a terminal
        logging.getLogger("voxhora_techshare_agent.cli").info("streamed big.mp4 -> case")
        logging.getLogger("voxhora_techshare_agent.cli").warning("re-fetching x")
        err = capsys.readouterr().err
        assert "streamed big.mp4" not in err
        assert "re-fetching x" in err
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
        for h in before:
            if h not in root.handlers:
                root.addHandler(h)
        root.setLevel(level)


# ---------------------------------------------------------------- Gate 2 fixes

def test_photo_bundles_get_the_general_cap_other_zips_the_paperwork_cap():
    assert cli._is_auto_written(_item("Scene photos.zip", "Photographs", "80,000 KB")) is True
    assert cli._is_auto_written(_item("Scene photos.zip", "Photographs", "150,000 KB")) is False
    assert cli._is_auto_written(_item("Officer files.zip", "Disclosure File", "80,000 KB")) is False


def test_a_retried_email_does_not_pile_up_events():
    for _ in range(5):
        storage.append_discovery_event("C1CR26000000", "dme_discoverable")
    storage.append_discovery_event("C1CR26000000", "attorney_of_record")
    assert [(e["cause"], e["kind"]) for e in _events()] == [
        ("C1CR26000000", "dme_discoverable"), ("C1CR26000000", "attorney_of_record")]


def test_summary_skips_nameless_rows_and_files_waiting_for_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "is_present_in_portal", lambda it, cause: False)
    staged_video = _item("Staged.mp4", "Video - BodyCam", "5 KB", "s1")
    monkeypatch.setattr(storage, "case_discovery_target_path",
                        lambda it, cause, target_dir=None: tmp_path / it.name)
    (tmp_path / "Staged.mp4").write_bytes(b"x" * 5000)       # complete, awaiting upload
    nameless = _item("", "", "", "n1")
    waiting = _item("Body.mp4", "Video - BodyCam", "9 KB", "w1")
    summary = cli._not_fetched_summary([staged_video, nameless, waiting], set(), "C1CR26000000", tmp_path)
    assert summary == {"video": {"count": 1, "bytes": 9000}}


def test_ok_line_says_what_was_left_and_app_still_parses_it(tmp_path, monkeypatch, capsys):
    import re
    _run_fetch(monkeypatch, tmp_path, CASE, "written")
    ok = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("OK — "))
    assert "3 not in this written-only run" in ok
    # DownloadJobsManager.parseAgentSummary's pattern, verbatim.
    m = re.search(r"(\d+) new files?(?: downloaded)?, (\d+) failed", ok)
    assert m and m.groups() == ("3", "0")


def test_written_only_manifest_replaces_only_what_it_attempted(tmp_path):
    manifest = tmp_path / "_manifest.json"
    manifest.write_text(json.dumps({"items": {}, "failed_items": [
        {"id": "dmeId:pdf", "filename": "Report.pdf", "reason": "old"},
        {"id": "dmeId:video", "filename": "Body.mp4", "reason": "old"},
    ]}))
    cli._write_manifest(manifest, "C1CR26000000", {}, failed_items=[
        {"id": "dmeId:jpg", "filename": "Scene.jpg", "reason": "new"}],
        attempted_ids={"dmeId:pdf", "dmeId:jpg"})
    failed = {f["id"] for f in json.loads(manifest.read_text())["failed_items"]}
    assert failed == {"dmeId:video", "dmeId:jpg"}, "the PDF succeeded now; the video wasn't attempted"


def test_piped_stderr_log_is_capped_with_one_notice(capsys):
    handler = cli._StderrLogHandler(budget=500)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("cap-test")
    log.propagate = False
    log.addHandler(handler)
    try:
        for i in range(50):
            log.warning("download attempt %d failed: connection dropped", i)
    finally:
        log.removeHandler(handler)
    err = capsys.readouterr().err
    assert len(err) < 700
    assert err.count("further log lines omitted") == 1


def test_stderr_handler_follows_a_swapped_stderr(monkeypatch):
    """Bound at emit time, not creation: a handler created under one stream
    must not write into a closed one later (seen in the suite)."""
    handler = cli._StderrLogHandler(budget=None)
    handler.setFormatter(logging.Formatter("%(message)s"))
    swapped = io.StringIO()
    monkeypatch.setattr(sys, "stderr", swapped)
    handler.emit(logging.LogRecord("t", logging.WARNING, __file__, 1, "after the swap", None, None))
    assert "after the swap" in swapped.getvalue()


def test_errors_still_reach_stderr_after_the_budget(capsys):
    handler = cli._StderrLogHandler(budget=300)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("cap-error-test")
    log.propagate = False
    log.addHandler(handler)
    try:
        for i in range(30):
            log.warning("retry %d", i)
        log.error("run 123 exit 1: TechShare refused the login")
    finally:
        log.removeHandler(handler)
    assert "TechShare refused the login" in capsys.readouterr().err


def test_configure_keeps_info_for_the_run_log():
    """The run-log file gets every INFO line only if the ROOT logger lets INFO
    through; the stderr handler alone filters to WARNING."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    caught = []

    class _List(logging.Handler):
        def emit(self, record):
            caught.append(record.getMessage())

    try:
        root.setLevel(logging.WARNING)
        root.addHandler(_List())
        cli._configure_stderr_logging()
        logging.getLogger("voxhora_techshare_agent.cli").info("streamed a -> b")
        assert "streamed a -> b" in caught
    finally:
        root.setLevel(level)
        for h in list(root.handlers):
            if h not in handlers:
                root.removeHandler(h)


@pytest.mark.parametrize("name,type_", [
    ("SCAN.PDF", "Offense Report"), ("IMG_0001.JPG", "Photographs"),
    ("DISCLOSURE.ZIP", "Disclosure File"), ("Email.MSG", "State's Correspondence (Discoverable)"),
])
def test_uppercase_extensions_are_written(name, type_):
    assert cli._is_auto_written(_item(name, type_, "100 KB")) is True


def test_uppercase_video_is_not_written_and_counts_as_video():
    item = _item("BODYCAM.MP4", "Evidence", "100 KB")
    assert cli._is_auto_written(item) is False
    assert cli._not_fetched_kind(item) == "video"


def test_written_only_fetch_manifest_keeps_unattempted_failures(tmp_path, monkeypatch, capsys):
    """End to end through cmd_fetch: a written-only run must not wipe the
    video's failure from the manifest (the full fetch still owes it), and must
    clear the PDF's old failure once the PDF comes down."""
    manifest = tmp_path / "_manifest.json"
    video_fp = storage.dme_fingerprint(CASE[3])
    pdf_fp = storage.dme_fingerprint(CASE[0])
    manifest.write_text(json.dumps({"items": {}, "failed_items": [
        {"id": video_fp, "filename": CASE[3].name, "reason": "old"},
        {"id": pdf_fp, "filename": CASE[0].name, "reason": "old"},
    ]}))
    client = _Client(CASE)
    monkeypatch.setattr(cli, "_resolve_case", lambda c: {"service_id": "svc", "case_uuid": "uuid"})
    monkeypatch.setattr(cli, "TechShareSession", lambda: _Session())
    monkeypatch.setattr(cli, "TechShareClient", lambda s: client)
    monkeypatch.setattr(storage, "case_discovery_target_path",
                        lambda it, cause, target_dir=None: tmp_path / "case" / it.name)
    monkeypatch.setattr(storage, "is_present_in_portal", lambda it, cause: False)
    cli.cmd_fetch(argparse.Namespace(
        cause_number="C1CR26000000", service_id="svc", case_uuid="uuid", item_ids=None,
        target_dir=str(tmp_path / "case"), manifest=str(manifest), only="written",
    ))
    failed = {f["id"] for f in json.loads(manifest.read_text())["failed_items"]}
    assert failed == {video_fp}


def test_full_fetch_ok_line_is_byte_identical(tmp_path, monkeypatch, capsys):
    _run_fetch(monkeypatch, tmp_path, CASE, None)
    out = capsys.readouterr().out.splitlines()
    assert "OK — C1CR26000000: 6 new files downloaded, 0 failed (6 total in case)" in out


def test_piped_cap_counts_bytes_not_characters(capsys):
    handler = cli._StderrLogHandler(budget=1000)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("cap-bytes-test")
    log.propagate = False
    log.addHandler(handler)
    try:
        for _ in range(100):
            log.warning("re-fetching — → ✓ Muñoz")          # 3-byte characters
    finally:
        log.removeHandler(handler)
    assert len(capsys.readouterr().err.encode("utf-8")) < 1200


def test_summary_creates_no_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "is_present_in_portal", lambda it, cause: False)
    missing = tmp_path / "Doe, Jane" / "C1CR26000000"
    cli._not_fetched_summary([_item("Body.mp4", "Video - BodyCam", "9 KB", "z1")],
                             set(), "C1CR26000000", missing)
    assert not missing.exists()
