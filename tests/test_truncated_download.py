"""A short download must never be stored as complete evidence.

Guards the 2026-08-03 fix. Both streaming paths used to `os.replace` the
.partial onto the final name whatever arrived: `iter_content` stops when the
connection closes, and a clean close raises nothing. The short file then
looked complete forever — it uploaded, content-hash-verified as a faithful
transfer of the truncated bytes, and was marked seen. Silent evidence loss,
and the one failure a lawyer cannot see by looking.

Two independent guards, because neither covers everything:
  * transport — bytes written vs Content-Length / Content-Range total
  * inventory — landed size vs TechShare's own DME list size, which is what
    a chunked response (no Content-Length) falls back on
"""

from pathlib import Path

import pytest

from voxhora_techshare_agent import storage
from voxhora_techshare_agent.models import DMEItem
from voxhora_techshare_agent.session import (
    TechShareSession,
    TruncatedDownloadError,
    _promised_body_bytes,
)


class _Resp:
    def __init__(self, headers):
        self.headers = headers


def _item(name: str, size: str) -> DMEItem:
    return DMEItem(
        name=name, type="Video - BodyCam", source="Government", size=size,
        available_date="", last_accessed_date=None, is_archived=False,
        enclosure_href=None, api_href=None,
    )


# ----- what the host promised -----

def test_content_length_is_the_promise():
    assert _promised_body_bytes(_Resp({"Content-Length": "1000"})) == 1000


def test_resumed_response_adds_the_offset_already_on_disk():
    """A 206's Content-Length covers only the tail."""
    assert _promised_body_bytes(_Resp({"Content-Length": "400"}), resume_from=600) == 1000


def test_content_range_total_wins():
    r = _Resp({"Content-Length": "400", "Content-Range": "bytes 600-999/1000"})
    assert _promised_body_bytes(r, resume_from=600) == 1000


def test_no_promise_when_encoded_or_chunked():
    """Both would produce false mismatches on healthy multi-GB evidence."""
    assert _promised_body_bytes(_Resp({"Content-Length": "10", "Content-Encoding": "gzip"})) is None
    assert _promised_body_bytes(_Resp({})) is None
    assert _promised_body_bytes(_Resp({"Content-Length": "not-a-number"})) is None


# ----- transport guard: the short body never gets renamed -----

class _TruncatingResponse:
    """Promises 1000 bytes, delivers 600, closes without raising."""
    status_code = 200
    headers = {"Content-Length": "1000"}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=None):
        yield b"x" * 600


def test_truncated_stream_keeps_partial_and_raises(tmp_path: Path, monkeypatch):
    s = TechShareSession.__new__(TechShareSession)
    target = tmp_path / "evidence.mp4"

    monkeypatch.setattr(s, "_session", type("T", (), {
        "get": lambda *a, **k: _TruncatingResponse()})(), raising=False)
    monkeypatch.setattr(s, "keepalive", lambda: __import__("contextlib").nullcontext(),
                        raising=False)

    with pytest.raises(TruncatedDownloadError):
        s.prepared_download_to_path("/link", "cookie", target)

    assert not target.exists(), "a short file must NOT be stored under the real name"
    partial = target.with_suffix(".mp4.partial")
    assert partial.exists() and partial.stat().st_size == 600, "bytes kept for resume"


# ----- inventory guard: covers the chunked case the transport check can't -----

def test_verify_accepts_a_correct_landing(tmp_path: Path):
    item = _item("v.mp4", "1,421,728 KB")
    p = tmp_path / item.name
    with open(p, "wb") as f:
        f.truncate(1_421_728_115)           # the real Cannon landing, 2026-08-03
    storage.verify_downloaded_size(item, p)  # must not raise
    assert p.exists()


def test_verify_quarantines_a_short_landing(tmp_path: Path):
    item = _item("v.mp4", "1,421,728 KB")
    p = tmp_path / item.name
    with open(p, "wb") as f:
        f.truncate(117_440_512)
    with pytest.raises(storage.ShortDownloadError):
        storage.verify_downloaded_size(item, p)
    assert not p.exists(), "a short file left at the real name reads as PRESENT forever"
    assert (tmp_path / "v.mp4.partial").stat().st_size == 117_440_512


def test_verify_is_silent_without_a_usable_list_size(tmp_path: Path):
    item = _item("v.mp4", "")
    p = tmp_path / item.name
    with open(p, "wb") as f:
        f.truncate(123)
    storage.verify_downloaded_size(item, p)   # nothing to check against
    assert p.exists()


def test_quarantined_file_is_not_mistaken_for_a_complete_copy(tmp_path: Path):
    """The two 08-03 fixes must agree: a quarantined short file is not skippable."""
    item = _item("v.mp4", "1,421,728 KB")
    p = tmp_path / item.name
    with open(p, "wb") as f:
        f.truncate(117_440_512)
    with pytest.raises(storage.ShortDownloadError):
        storage.verify_downloaded_size(item, p)
    assert storage.staged_copy_is_complete(item, p) is False
