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
