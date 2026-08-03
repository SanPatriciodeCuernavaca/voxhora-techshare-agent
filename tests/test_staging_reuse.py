"""Milestone 5 — a file already complete in staging must not be re-downloaded.

Guards the 2026-08-03 fix. The Portal presence check (priority 3) looks in
DROPBOX, so evidence that downloaded fine but has not been uploaded yet reads
as absent and was fetched from TechShare a second time — 5.01 GB / 29 minutes
on Rai C1CR25207191, for bytes already sitting on the disk.

The whole fix rests on one measured fact: TechShare's `size` string is DECIMAL
kB truncated to whole units, so floor(bytes / 1000) == reported. Verified
2026-08-03 against every landed Portal file with a cached list entry —
877/877 inside a 1000-byte window, min delta 0 B, max 999 B. Using 1024
matches only 135/877. test_kib_multiplier_would_break_it pins that, because
that is the mistake a future reader is most likely to "fix" back in.
"""

from pathlib import Path

from voxhora_techshare_agent import storage
from voxhora_techshare_agent.models import DMEItem


def _item(name: str, size: str) -> DMEItem:
    return DMEItem(
        name=name,
        type="Video - BodyCam",
        source="Government",
        size=size,
        available_date="2026-07-18",
        last_accessed_date=None,
        is_archived=False,
        enclosure_href="https://x/dmefile?dmeId=abc&isStream=1",
        api_href=None,
    )


def _write(path: Path, size_bytes: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size_bytes)
    return path


# ----- size parsing -----

def test_size_range_is_decimal_kb():
    assert storage.expected_size_range(_item("a.mp4", "17,026 KB")) == (17_026_000, 17_026_999)


def test_size_range_none_when_unusable():
    for bad in ("", "   ", "unknown", "0 KB"):
        assert storage.expected_size_range(_item("a.mp4", bad)) is None


# ----- the completeness decision -----

def test_exact_size_is_complete(tmp_path: Path):
    item = _item("Axon_Fleet_3.mp4", "5,009,920 KB")
    p = _write(tmp_path / item.name, 5_009_920_915)
    assert storage.staged_copy_is_complete(item, p) is True


def test_boundaries_of_the_window(tmp_path: Path):
    item = _item("v.mp4", "1,000 KB")
    assert storage.staged_copy_is_complete(item, _write(tmp_path / "lo.mp4", 1_000_000)) is True
    assert storage.staged_copy_is_complete(item, _write(tmp_path / "hi.mp4", 1_000_999)) is True
    assert storage.staged_copy_is_complete(item, _write(tmp_path / "under.mp4", 999_999)) is False
    assert storage.staged_copy_is_complete(item, _write(tmp_path / "over.mp4", 1_001_000)) is False


def test_truncated_file_is_refetched(tmp_path: Path):
    """The failure that would lose evidence: a short file renamed as if whole."""
    item = _item("Axon_Fleet_3.mp4", "5,009,920 KB")
    p = _write(tmp_path / item.name, 1_375_731_712)      # a real stranded partial size
    assert storage.staged_copy_is_complete(item, p) is False


def test_partial_never_matches(tmp_path: Path):
    """An interrupted download lives at <name>.partial, so the final name is absent."""
    item = _item("Axon_Fleet_3.mp4", "5,009,920 KB")
    _write(tmp_path / "Axon_Fleet_3.mp4.partial", 5_009_920_915)
    assert storage.staged_copy_is_complete(item, tmp_path / item.name) is False


def test_missing_file_is_not_complete(tmp_path: Path):
    item = _item("nope.mp4", "17,026 KB")
    assert storage.staged_copy_is_complete(item, tmp_path / "nope.mp4") is False


def test_directory_is_not_complete(tmp_path: Path):
    item = _item("d.mp4", "1 KB")
    (tmp_path / "d.mp4").mkdir()
    assert storage.staged_copy_is_complete(item, tmp_path / "d.mp4") is False


# ----- anything still owed post-download work must NOT be skipped -----

def test_zip_is_never_skipped(tmp_path: Path):
    """Skipping would strand it unextracted — the download block owns that."""
    item = _item("photos.zip", "1,000 KB")
    p = _write(tmp_path / item.name, 1_000_000)
    assert storage.staged_copy_is_complete(item, p) is False


def test_unplayable_video_is_never_skipped(tmp_path: Path):
    item = _item("interview.wmv", "1,000 KB")
    p = _write(tmp_path / item.name, 1_000_000)
    assert storage.staged_copy_is_complete(item, p) is False


def test_unplayable_audio_and_wav_are_never_skipped(tmp_path: Path):
    for name in ("call.wma", "call.wav"):
        item = _item(name, "1,000 KB")
        p = _write(tmp_path / name, 1_000_000)
        assert storage.staged_copy_is_complete(item, p) is False


def test_playable_mp4_and_pdf_are_skippable(tmp_path: Path):
    """The cases that actually cost money — multi-GB Axon video — must skip."""
    for name in ("Axon_Body_4.mp4", "offense_report.pdf"):
        item = _item(name, "2,000 KB")
        p = _write(tmp_path / name, 2_000_500)
        assert storage.staged_copy_is_complete(item, p) is True


# ----- control test: the wrong multiplier must not pass -----

def test_kib_multiplier_would_break_it(tmp_path: Path):
    """Real numbers from Rai's 5.01 GB video. Under the KiB reading the same
    file looks 2.3% short and would be re-downloaded forever."""
    item = _item("Axon_Fleet_3.mp4", "5,009,920 KB")
    p = _write(tmp_path / item.name, 5_009_920_915)
    assert storage.staged_copy_is_complete(item, p) is True
    assert 5_009_920 * 1024 != 5_009_920_915        # the mistake this pins


# ----- end to end: cmd_fetch must not call the network at all -----

class _RefuseToDownload:
    """Serves a DME list; any download attempt fails the test outright.

    The predicate tests above prove the DECISION. This proves the WIRING —
    that cmd_fetch actually consults it before reaching for the network.
    """

    def __init__(self, items):
        self._items = items
        self.attempts: list[str] = []

    def get_case_detail(self, service_id, case_uuid):
        return {"uuid": case_uuid}

    def get_dme_list(self, service_id, case):
        return self._items

    def download_dme_file_to_path(self, service_id, item, path):
        self.attempts.append(item.name)
        raise AssertionError(
            f"re-downloaded {item.name!r} — the complete staged copy should have been used"
        )


class _SessionOK:
    def ensure_authenticated(self):
        pass


def test_cmd_fetch_skips_the_network_when_the_bytes_are_already_staged(tmp_path, monkeypatch):
    """The Rai scenario end to end: complete in staging, absent from Dropbox.

    Without the fix cmd_fetch re-pulls the whole file from TechShare — 5.01 GB
    and 29 minutes on Rai C1CR25207191. Here the fake client raises if it is
    ever asked to download, so a regression fails loudly instead of quietly
    costing the county another copy.
    """
    import argparse as _argparse
    from voxhora_techshare_agent import cli
    from voxhora_techshare_agent import storage as _storage

    item = _item("Axon_Fleet_3_Front.mp4", "1,421,728 KB")
    _write(tmp_path / item.name, 1_421_728_115)      # the real Cannon landing
    client = _RefuseToDownload([item])

    monkeypatch.setattr(cli, "_resolve_case", lambda c: {"service_id": "svc", "case_uuid": "uuid"})
    monkeypatch.setattr(cli, "TechShareSession", lambda: _SessionOK())
    monkeypatch.setattr(cli, "TechShareClient", lambda s: client)
    monkeypatch.setattr(_storage, "load_seen_dme_ids", lambda *a, **k: set())
    monkeypatch.setattr(_storage, "save_seen_dme_ids", lambda *a, **k: None)
    monkeypatch.setattr(
        _storage, "case_discovery_target_path",
        lambda item, cause, target_dir=None: tmp_path / item.name,
    )
    # Not in Dropbox yet — exactly why priority 3 would order a re-fetch.
    monkeypatch.setattr(_storage, "is_present_in_portal", lambda item, cause: False)

    rc = cli.cmd_fetch(_argparse.Namespace(
        cause_number="C1CR25207191", service_id="svc", case_uuid="uuid",
        item_ids=None, target_dir=str(tmp_path), manifest=None,
    ))

    assert client.attempts == [], "cmd_fetch must not touch the network for staged bytes"
    assert rc == 0
    assert (tmp_path / item.name).stat().st_size == 1_421_728_115, "staged bytes untouched"
