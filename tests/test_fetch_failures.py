"""Tests for the 2026-07-04 discovery-download reliability beat:
smallest-first fetch order, per-file failure capture (FAILED-ITEMS-JSON +
manifest failed_items), and mid-run re-authentication on retry."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from voxhora_techshare_agent import cli
from voxhora_techshare_agent.models import DMEItem
from voxhora_techshare_agent.proxy_client import TechShareClient


def _item(name: str, size: str, enclosure: str | None = "https://x/dmefile?dmeId=abc&isStream=1") -> DMEItem:
    return DMEItem(
        name=name,
        type="Video - BodyCam",
        source="Government",
        size=size,
        available_date="6/16/2026",
        last_accessed_date=None,
        is_archived=False,
        enclosure_href=enclosure,
        api_href=None,
    )


# ---------------------------------------------------------------- _size_kb

def test_size_kb_parses_comma_grouped_kb():
    assert cli._size_kb(_item("big.mp4", "2,524,423 KB")) == 2_524_423
    assert cli._size_kb(_item("small.pdf", "1 KB")) == 1


def test_size_kb_blank_sorts_last():
    unknown = cli._size_kb(_item("mystery.bin", ""))
    assert unknown > cli._size_kb(_item("big.mp4", "999,999,999 KB"))


def test_smallest_first_ordering():
    items = [
        _item("giant.mp4", "2,524,423 KB"),
        _item("tiny.pdf", "1 KB"),
        _item("photo.jpg", "4,051 KB"),
        _item("unknown.bin", ""),
    ]
    ordered = sorted(items, key=cli._size_kb)
    assert [i.name for i in ordered] == ["tiny.pdf", "photo.jpg", "giant.mp4", "unknown.bin"]


# ------------------------------------------------------- manifest failures

def test_write_manifest_records_failed_items(tmp_path: Path):
    path = tmp_path / "_manifest.json"
    failed = [{"filename": "a.mp4", "id": "dmeId:1", "reason": "prep 500"}]
    cli._write_manifest(path, "C1CR26000001", {}, failed_items=failed)
    data = json.loads(path.read_text())
    assert data["failed_items"] == failed


def test_write_manifest_bulk_fetch_replaces_failed_list(tmp_path: Path):
    """A bulk fetch attempts every outstanding item, so its failure list is
    the complete outstanding set — an item that succeeded this run must
    drop off the prior failed list."""
    path = tmp_path / "_manifest.json"
    cli._write_manifest(path, "C1CR26000001", {}, failed_items=[
        {"filename": "a.mp4", "id": "dmeId:1", "reason": "prep 500"},
        {"filename": "b.pdf", "id": "dmeId:2", "reason": "prep 500"},
    ])
    # Next run: b.pdf succeeded, only a.mp4 still failing.
    cli._write_manifest(path, "C1CR26000001", {"dmeId:2": {"filename": "b.pdf"}}, failed_items=[
        {"filename": "a.mp4", "id": "dmeId:1", "reason": "timeout"},
    ])
    data = json.loads(path.read_text())
    assert [f["filename"] for f in data["failed_items"]] == ["a.mp4"]
    assert "dmeId:2" in data["items"]  # prior successes still merge


def test_write_manifest_subset_run_preserves_failed_list(tmp_path: Path):
    """fetch-items only attempts a subset — passing None must preserve the
    prior outstanding list untouched."""
    path = tmp_path / "_manifest.json"
    failed = [{"filename": "a.mp4", "id": "dmeId:1", "reason": "prep 500"}]
    cli._write_manifest(path, "C1CR26000001", {}, failed_items=failed)
    cli._write_manifest(path, "C1CR26000001", {"dmeId:9": {"filename": "z.pdf"}})  # no failed_items kwarg
    data = json.loads(path.read_text())
    assert data["failed_items"] == failed


# -------------------------------------------------- mid-run re-auth on retry

class _FakeSession:
    """prep fails until reauthenticate() is called — models the dead
    TechShare session that only a fresh login can revive."""

    def __init__(self):
        self.reauth_calls = 0
        self.prep_calls = 0

    def prep_dme_download(self, service_id, dme_url):
        self.prep_calls += 1
        if self.reauth_calls == 0:
            raise RuntimeError("prep returned no downloadLink/DefensePortalAuth (status 500, link=False, auth=False)")
        return ("https://dme/download?token=t", "auth-cookie")

    def prepared_download_to_path(self, link, auth, target_path):
        Path(target_path).write_bytes(b"content")
        return 7

    def reauthenticate(self):
        self.reauth_calls += 1


def test_retry_reauthenticates_and_recovers(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("voxhora_techshare_agent.proxy_client.time.sleep", lambda s: None)
    client = TechShareClient.__new__(TechShareClient)
    client.session = _FakeSession()
    target = tmp_path / "file.pdf"
    written = client.download_dme_file_to_path("svc", _item("file.pdf", "1 KB"), target)
    assert written == 7
    assert client.session.reauth_calls == 1  # healed on first retry
    assert client.session.prep_calls == 2    # failed once, succeeded once


def test_retry_exhaustion_still_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("voxhora_techshare_agent.proxy_client.time.sleep", lambda s: None)

    class _DeadSession(_FakeSession):
        def prep_dme_download(self, service_id, dme_url):
            self.prep_calls += 1
            raise RuntimeError("prep 500")

    client = TechShareClient.__new__(TechShareClient)
    client.session = _DeadSession()
    with pytest.raises(RuntimeError):
        client.download_dme_file_to_path("svc", _item("f.pdf", "1 KB"), tmp_path / "f.pdf")
    assert client.session.prep_calls == 3    # all attempts used
    assert client.session.reauth_calls == 2  # re-auth before each retry


# ------------------------------------------------------ resume-from-partial

from voxhora_techshare_agent.session import TechShareSession


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        yield self._body


class _FakeTransport:
    """Stands in for requests.Session; records the Range header sent."""

    def __init__(self, response: _FakeResponse):
        self.response = response
        self.sent_headers: dict | None = None

    def get(self, url, cookies=None, timeout=None, stream=None, headers=None):
        self.sent_headers = headers or {}
        return self.response


def _session_with(response: _FakeResponse) -> tuple[TechShareSession, _FakeTransport]:
    s = TechShareSession.__new__(TechShareSession)
    transport = _FakeTransport(response)
    s._session = transport
    return s, transport


def test_resume_appends_on_honored_206(tmp_path: Path):
    target = tmp_path / "video.mp4"
    partial = tmp_path / "video.mp4.partial"
    partial.write_bytes(b"AAAA")  # 4 bytes from a prior attempt
    resp = _FakeResponse(206, b"BBBB", {"Content-Range": "bytes 4-7/8"})
    s, transport = _session_with(resp)
    written = s.prepared_download_to_path("/download?token=t", "auth", target)
    assert transport.sent_headers.get("Range") == "bytes=4-"
    assert written == 8
    assert target.read_bytes() == b"AAAABBBB"
    assert not partial.exists()


def test_resume_restarts_clean_when_server_ignores_range(tmp_path: Path):
    target = tmp_path / "video.mp4"
    (tmp_path / "video.mp4.partial").write_bytes(b"OLD!")
    resp = _FakeResponse(200, b"FRESHDATA")  # 200 = Range ignored
    s, _ = _session_with(resp)
    written = s.prepared_download_to_path("/download?token=t", "auth", target)
    assert written == len(b"FRESHDATA")
    assert target.read_bytes() == b"FRESHDATA"


def test_resume_restarts_when_body_is_reencoded(tmp_path: Path):
    target = tmp_path / "video.mp4"
    (tmp_path / "video.mp4.partial").write_bytes(b"OLD!")
    resp = _FakeResponse(206, b"FRESH", {"Content-Range": "bytes 4-8/9", "Content-Encoding": "gzip"})
    s, _ = _session_with(resp)
    written = s.prepared_download_to_path("/download?token=t", "auth", target)
    assert target.read_bytes() == b"FRESH"
    assert written == len(b"FRESH")


def test_interrupted_stream_keeps_partial_for_next_resume(tmp_path: Path):
    target = tmp_path / "video.mp4"

    class _DyingResponse(_FakeResponse):
        def iter_content(self, chunk_size):
            yield b"SOMEBYTES"
            raise TimeoutError("stream died")

    s, _ = _session_with(_DyingResponse(200, b""))
    with pytest.raises(TimeoutError):
        s.prepared_download_to_path("/download?token=t", "auth", target)
    partial = tmp_path / "video.mp4.partial"
    assert partial.exists() and partial.read_bytes() == b"SOMEBYTES"


def test_zero_byte_fresh_failure_deletes_partial(tmp_path: Path):
    target = tmp_path / "video.mp4"

    class _InstantDeath(_FakeResponse):
        def iter_content(self, chunk_size):
            raise TimeoutError("no bytes at all")
            yield b""  # pragma: no cover

    s, _ = _session_with(_InstantDeath(200, b""))
    with pytest.raises(TimeoutError):
        s.prepared_download_to_path("/download?token=t", "auth", target)
    assert not (tmp_path / "video.mp4.partial").exists()


# --- Non-destructive re-login (2026-07-28) --------------------------------
#
# Patrick lost a body-cam video 275 minutes into a run. The reported error was
# "Could not retrieve CSRF token; session may be expired" — which is IMPOSSIBLE
# as a first failure, because csrf_token() short-circuits on a cached token.
# It can only appear after reauthenticate() nulls that token. The old version
# cleared the token, the DPA cache and the cookies BEFORE calling login(), so a
# re-login that itself failed left the session strictly worse than it found it
# and guaranteed the remaining retries would die too.

import requests

from voxhora_techshare_agent.session import TechShareSession


def _session_with_state(monkeypatch, login_raises):
    s = TechShareSession.__new__(TechShareSession)
    s._session = requests.Session()
    s._csrf_token = "TOKEN-FROM-RUN-START"
    s._dpa_cache = "DPA-FROM-RUN-START"
    s._session.cookies.set("DefensePortalAuth", "COOKIE-FROM-RUN-START",
                           domain="attorney.techsharetx.gov", path="/")

    def fake_login():
        if login_raises:
            raise RuntimeError("login refused")
        s._csrf_token = "TOKEN-AFTER-RELOGIN"
        s._dpa_cache = None
    s.login = fake_login
    return s


def test_failed_relogin_restores_auth_state(monkeypatch):
    """The fix. A re-login that fails must leave the session exactly as it was."""
    s = _session_with_state(monkeypatch, login_raises=True)

    with pytest.raises(RuntimeError, match="login refused"):
        s.reauthenticate()

    assert s._csrf_token == "TOKEN-FROM-RUN-START", (
        "A failed re-login must not destroy the cached CSRF token — that is what "
        "manufactured the misleading 'could not retrieve CSRF token' error."
    )
    assert s._dpa_cache == "DPA-FROM-RUN-START"
    assert s._session.cookies.get(
        "DefensePortalAuth", domain="attorney.techsharetx.gov", path="/"
    ) == "COOKIE-FROM-RUN-START"


def test_failed_relogin_propagates_the_real_error(monkeypatch):
    """The original reason must survive, not be swallowed into a warning."""
    s = _session_with_state(monkeypatch, login_raises=True)
    with pytest.raises(RuntimeError, match="login refused"):
        s.reauthenticate()


def test_successful_relogin_still_replaces_state(monkeypatch):
    """The normal path is unchanged — a good re-login still swaps in fresh auth."""
    s = _session_with_state(monkeypatch, login_raises=False)
    s.reauthenticate()
    assert s._csrf_token == "TOKEN-AFTER-RELOGIN"
    assert s._dpa_cache is None
    assert s._session.cookies.get(
        "DefensePortalAuth", domain="attorney.techsharetx.gov", path="/"
    ) is None, "A successful re-login must still purge the stale DPA cookie."


# --- Session keepalive (2026-07-28) ---------------------------------------
#
# Multi-GB transfers stream from a separate DME host, so the API session sees
# no traffic for 16-20 minutes at a stretch and idles out mid-run. The
# heartbeat keeps it alive. It must be fail-soft: a missed ping can never be
# allowed to break a download that is otherwise succeeding.

import threading as _threading


def _bare_session():
    s = TechShareSession.__new__(TechShareSession)
    s._session = requests.Session()
    s._csrf_token = "TOKEN"
    s._dpa_cache = "DPA"
    return s


def test_keepalive_pings_while_open_and_stops_after():
    s = _bare_session()
    pings = []
    s._session_ping = lambda: (pings.append(1), True)[1]

    with s.keepalive(interval_seconds=0.05):
        time.sleep(0.28)
        during = len(pings)
    settled = len(pings)
    time.sleep(0.2)

    assert during >= 2, f"expected repeated pings during the transfer, got {during}"
    assert len(pings) == settled, "heartbeat kept running after the transfer ended"


def test_keepalive_survives_a_failing_ping():
    """Fail-soft: a dead ping must not propagate into the download."""
    s = _bare_session()
    calls = []

    def flaky():
        calls.append(1)
        raise requests.ConnectionError("network blip")
    s._session_ping = flaky

    with s.keepalive(interval_seconds=0.05):
        time.sleep(0.15)
    # No exception escaped, and the transfer body ran to completion.
    assert calls, "ping should have been attempted"


def test_keepalive_does_not_mutate_cached_auth_state():
    """The heartbeat runs concurrently with the stream — it must stay read-only.

    Mutating _csrf_token from the heartbeat is exactly the class of bug that
    made the 07-28 failure so hard to read.
    """
    s = _bare_session()
    s._session_ping = lambda: True
    with s.keepalive(interval_seconds=0.05):
        time.sleep(0.12)
    assert s._csrf_token == "TOKEN"
    assert s._dpa_cache == "DPA"


def test_keepalive_thread_is_daemon_and_exits():
    s = _bare_session()
    s._session_ping = lambda: True
    before = _threading.active_count()
    with s.keepalive(interval_seconds=0.05):
        time.sleep(0.1)
    time.sleep(0.2)
    assert _threading.active_count() <= before, "keepalive thread leaked"


# ------------------------------------ fetch-items failure reasons (M5 prio 4)
#
# 2026-08-02 — cmd_fetch has emitted FAILED-ITEMS-JSON since 2026-07-04;
# cmd_fetch_items never did, so a Portal-driven fetch reported failures to the
# lawyer as a bare count with no reason and no filename. These pin the fix.

import argparse as _argparse

from voxhora_techshare_agent import storage as _storage


class _FakeSessionOK:
    def ensure_authenticated(self):
        return None


class _FakeItemsClient:
    """Serves a fixed DME list; raises for any name in `fail_on`."""

    def __init__(self, items, fail_on=None):
        self._items = items
        self._fail_on = set(fail_on or ())

    def get_case_detail(self, service_id, case_uuid):
        return {"uuid": case_uuid}

    def get_dme_list(self, service_id, case):
        return self._items

    def download_dme_file_to_path(self, service_id, item, path):
        if item.name in self._fail_on:
            raise RuntimeError("prep 500 from TechShare")
        path.parent.mkdir(parents=True, exist_ok=True)
        # 2026-08-03 — write a size consistent with the item's own DME-list
        # entry. cmd_fetch/cmd_fetch_items now reject a landing that
        # disagrees with TechShare's inventory (the short-download guard),
        # and a 1-byte stand-in for a "9 KB" item is not a faithful fake:
        # it made this fixture exercise the truncation path by accident.
        window = _storage.expected_size_range(item)
        written = window[0] if window else 1
        path.write_bytes(b"x" * written)
        return written


def _wire_fetch_items(monkeypatch, tmp_path, items, fail_on=None):
    monkeypatch.setattr(cli, "_resolve_case", lambda c: {"service_id": "svc", "case_uuid": "uuid"})
    monkeypatch.setattr(cli, "TechShareSession", lambda: _FakeSessionOK())
    monkeypatch.setattr(cli, "TechShareClient", lambda s: _FakeItemsClient(items, fail_on))
    monkeypatch.setattr(_storage, "load_seen_dme_ids", lambda *a, **k: set())
    monkeypatch.setattr(_storage, "save_seen_dme_ids", lambda *a, **k: None)
    monkeypatch.setattr(
        _storage, "case_discovery_target_path",
        lambda item, cause, target_dir=None: tmp_path / item.name,
    )


def _args(cause, item_ids, tmp_path):
    return _argparse.Namespace(
        cause_number=cause, service_id="svc", case_uuid="uuid",
        item_ids=item_ids, target_dir=str(tmp_path), manifest=None,
    )


def _failed_items_json(capsys):
    """Parse the FAILED-ITEMS-JSON line out of stdout, or None if absent."""
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("FAILED-ITEMS-JSON: "):
            return json.loads(line[len("FAILED-ITEMS-JSON: "):])
    return None


def test_fetch_items_reports_reason_for_download_failure(tmp_path: Path, monkeypatch, capsys):
    """A file that fails to download must reach the lawyer WITH its reason —
    not as a bare 'N failed' count."""
    item = _item("offense_report.pdf", "12 KB")
    _wire_fetch_items(monkeypatch, tmp_path, [item], fail_on={"offense_report.pdf"})

    rc = cli.cmd_fetch_items(_args("C1CR26500006", ["dmeId:abc"], tmp_path))

    assert rc == 2
    failed = _failed_items_json(capsys)
    assert failed is not None, "fetch-items emitted no FAILED-ITEMS-JSON line"
    assert len(failed) == 1
    assert failed[0]["filename"] == "offense_report.pdf"
    assert failed[0]["id"] == "dmeId:abc"
    assert "prep 500" in failed[0]["reason"]


def test_fetch_items_reports_reason_for_missing_fingerprint(tmp_path: Path, monkeypatch, capsys):
    """A requested item no longer in TechShare's live list already counted as
    a failure — it must now say WHY, and name which fingerprint."""
    _wire_fetch_items(monkeypatch, tmp_path, [])

    rc = cli.cmd_fetch_items(_args("C1CR26500006", ["dmeId:deadbeef"], tmp_path))

    assert rc == 2
    failed = _failed_items_json(capsys)
    assert failed is not None, "fetch-items emitted no FAILED-ITEMS-JSON line"
    assert failed[0]["id"] == "dmeId:deadbeef"
    assert failed[0]["reason"].strip() != ""


def test_fetch_items_success_emits_no_failure_line(tmp_path: Path, monkeypatch, capsys):
    """No failures ⇒ no FAILED-ITEMS-JSON line at all, so the Mac app's
    parser never sees an empty array it has to special-case."""
    item = _item("dashcam.pdf", "9 KB")
    _wire_fetch_items(monkeypatch, tmp_path, [item])

    rc = cli.cmd_fetch_items(_args("C1CR26500006", ["dmeId:abc"], tmp_path))

    assert rc == 0
    assert _failed_items_json(capsys) is None


# ------------------------------------------- durable run log (M5 prio 4)
#
# 2026-08-02 — the agent's output was piped to the Mac app and discarded;
# there was no fetch log at all. config.log_dir() existed and nothing called
# it. That absence is why Gomez had to be reconstructed from audit rows and
# disk state instead of read out of a log.

import logging as _logging

from voxhora_techshare_agent import config as _config


@pytest.fixture
def _clean_root_handlers():
    """Snapshot/restore root handlers so a test's FileHandler never leaks
    into the real ~/Library/Logs or into another test."""
    root = _logging.getLogger()
    before = list(root.handlers)
    yield
    for h in list(root.handlers):
        if h not in before:
            h.close()
            root.removeHandler(h)


def test_run_log_is_written_and_carries_the_run_id(tmp_path: Path, monkeypatch, _clean_root_handlers):
    monkeypatch.setattr(_config, "log_dir", lambda username=None: tmp_path)

    run_id = cli._attach_run_log()
    _logging.getLogger("voxhora_techshare_agent.cli").error("FAILED offense_report.pdf: prep 500")

    logs = list(tmp_path.glob("agent-*.log"))
    assert logs, "no run log file was created"
    text = logs[0].read_text()
    assert run_id in text, "run id missing — concurrent runs would be indistinguishable"
    # The per-item detail is the whole point: this is what Gomez needed.
    assert "offense_report.pdf" in text
    assert "prep 500" in text


def test_run_log_failure_never_breaks_the_run(tmp_path: Path, monkeypatch, capsys, _clean_root_handlers):
    """Fail-soft: a read-only or missing log dir must NOT stop the agent from
    fetching evidence. It degrades to the old stderr-only behavior."""
    def _boom(username=None):
        raise PermissionError("read-only volume")
    monkeypatch.setattr(_config, "log_dir", _boom)

    run_id = cli._attach_run_log()   # must not raise

    assert run_id, "a run id is still required even when logging is unavailable"
    assert "could not open run log" in capsys.readouterr().err


def test_run_log_scrubs_anything_password_shaped():
    """The log outlives the run, so don't trust argv to be secret-free."""
    assert cli._scrub(["fetch", "C1CR26500006"]) == "fetch C1CR26500006"
    assert cli._scrub(["login", "--password", "hunter2"]) == "login --password ***"
    assert "hunter2" not in cli._scrub(["login", "-p", "hunter2"])
