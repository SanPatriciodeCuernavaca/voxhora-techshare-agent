"""Tests for the 2026-07-04 discovery-download reliability beat:
smallest-first fetch order, per-file failure capture (FAILED-ITEMS-JSON +
manifest failed_items), and mid-run re-authentication on retry."""

from __future__ import annotations

import json
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
