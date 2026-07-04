"""Tests for the case-UUID cache + storage utilities."""

from __future__ import annotations

import json
from pathlib import Path

from voxhora_techshare_agent import config, storage


def _seed_cache(tmp_path: Path, mapping: dict) -> None:
    """Write a synthetic cache file to a tmp_path-rooted state dir."""
    cache_dir = tmp_path / "state"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "case_uuid_cache.json").write_text(json.dumps(mapping))


def test_load_case_cache_returns_empty_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "state_dir", lambda username=None: tmp_path)
    assert storage.load_case_cache() == {}


def test_lookup_case_hits_existing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "state_dir", lambda username=None: tmp_path)
    (tmp_path / "case_uuid_cache.json").write_text(
        json.dumps(
            {
                "C1CR00001234": {
                    "case_uuid": "uuid-aaa",
                    "service_id": "sid-ca",
                    "backend_port": 1030,
                },
                "D1DC00001234": {
                    "case_uuid": "uuid-bbb",
                    "service_id": "sid-da",
                    "backend_port": 1031,
                },
            }
        )
    )
    assert storage.lookup_case("C1CR00001234")["case_uuid"] == "uuid-aaa"
    assert storage.lookup_case("D1DC00001234")["backend_port"] == 1031
    assert storage.lookup_case("C1CR99999999") is None


def test_case_cache_stats_counts_by_port(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "state_dir", lambda username=None: tmp_path)
    (tmp_path / "case_uuid_cache.json").write_text(
        json.dumps(
            {
                "C1CR00000001": {"case_uuid": "u1", "service_id": "s", "backend_port": 1030},
                "C1CR00000002": {"case_uuid": "u2", "service_id": "s", "backend_port": 1030},
                "D1DC00000001": {"case_uuid": "u3", "service_id": "s", "backend_port": 1031},
            }
        )
    )
    stats = storage.case_cache_stats()
    assert stats["total"] == 3
    assert stats["by_port"][1030] == 2
    assert stats["by_port"][1031] == 1


def test_corrupt_cache_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "state_dir", lambda username=None: tmp_path)
    (tmp_path / "case_uuid_cache.json").write_text("not-valid-json{{{")
    assert storage.load_case_cache() == {}


def test_atomic_write_json_round_trip(tmp_path):
    target = tmp_path / "subdir" / "thing.json"
    storage.atomic_write_json(target, {"hello": 1, "list": [1, 2, 3]})
    assert json.loads(target.read_text()) == {"hello": 1, "list": [1, 2, 3]}
    # No tempfiles left behind
    leftover = [p for p in target.parent.iterdir() if p.name.startswith(".")]
    assert leftover == []


# --------------------------------------------- ZIP extraction (2026-07-04)

import zipfile as _zipfile

from voxhora_techshare_agent.storage import extract_zip_inplace


def _make_zip(path, members):
    """members: dict of archive-internal-path -> bytes"""
    with _zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def test_extract_goes_into_subfolder_not_case_root(tmp_path):
    z = tmp_path / "Photos(Count=2)_123.zip"
    _make_zip(z, {"a.jpg": b"A", "b.jpg": b"B"})
    n = extract_zip_inplace(z)
    assert n == 2
    sub = tmp_path / "Photos(Count=2)_123"
    assert (sub / "a.jpg").read_bytes() == b"A"
    assert (sub / "b.jpg").read_bytes() == b"B"
    # nothing extracted loose into the case folder
    loose = [f.name for f in tmp_path.iterdir() if f.is_file() and f.suffix == ".jpg"]
    assert loose == []


def test_extract_flattens_internal_dirs_inside_subfolder(tmp_path):
    z = tmp_path / "records.zip"
    _make_zip(z, {"deep/nested/report.pdf": b"R"})
    assert extract_zip_inplace(z) == 1
    assert (tmp_path / "records" / "report.pdf").read_bytes() == b"R"


def test_extract_collisions_get_numeric_suffix_never_overwrite(tmp_path):
    z = tmp_path / "dup.zip"
    _make_zip(z, {"x/1.png": b"first", "y/1.png": b"second", "z/1.png": b"third"})
    assert extract_zip_inplace(z) == 3
    sub = tmp_path / "dup"
    contents = sorted(p.name for p in sub.iterdir())
    assert contents == ["1.png", "1_2.png", "1_3.png"]
    # all three payloads survived — the old logic overwrote the third
    assert {p.read_bytes() for p in sub.iterdir()} == {b"first", b"second", b"third"}


def test_extract_empty_zip_creates_no_subfolder(tmp_path):
    z = tmp_path / "empty.zip"
    _make_zip(z, {})
    assert extract_zip_inplace(z) == 0
    assert not (tmp_path / "empty").exists()
