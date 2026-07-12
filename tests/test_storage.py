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


# ------------------------------------------- .msg → .txt companions (2026-07-04)

from voxhora_techshare_agent.storage import convert_msg_to_text


def test_msg_conversion_never_raises_on_garbage(tmp_path):
    bogus = tmp_path / "not-really-an-email.msg"
    bogus.write_bytes(b"this is not an OLE compound file")
    assert convert_msg_to_text(bogus) is None  # graceful, no exception
    assert not (tmp_path / "not-really-an-email.msg.txt").exists()


def test_zip_extraction_converts_msg_members(tmp_path, monkeypatch):
    """extract_zip_inplace calls the converter for .msg members; verify the
    hook fires (converter monkeypatched — crafting a real OLE file in-test
    isn't worth it; the real converter is exercised above + in production)."""
    import voxhora_techshare_agent.storage as storage_mod
    calls = []
    monkeypatch.setattr(storage_mod, "convert_msg_to_text", lambda p: calls.append(p) or None)
    z = tmp_path / "emails.zip"
    _make_zip(z, {"a.msg": b"x", "b.pdf": b"y"})
    assert storage_mod.extract_zip_inplace(z) == 2
    assert [p.name for p in calls] == ["a.msg"]


# ------------------------------- video → Portal-playable mp4 (2026-07-11)

from voxhora_techshare_agent.storage import (
    convert_video_to_playable,
    find_ffmpeg,
    is_portal_unplayable_video,
)


def test_unplayable_detection_by_extension(tmp_path):
    assert is_portal_unplayable_video(Path("cam.avi"))
    assert is_portal_unplayable_video(Path("CAM.AVI"))
    assert is_portal_unplayable_video(Path("body.wmv"))
    assert not is_portal_unplayable_video(Path("dash.mp4"))
    assert not is_portal_unplayable_video(Path("interview.mov"))
    assert not is_portal_unplayable_video(Path("report.pdf"))


def test_convert_skips_native_formats(tmp_path):
    native = tmp_path / "already-fine.mp4"
    native.write_bytes(b"x")
    assert convert_video_to_playable(native) is None
    assert native.exists()  # untouched


def test_convert_real_avi_end_to_end(tmp_path):
    """Synthesize a 1-second AVI with ffmpeg, convert it, and verify the
    mp4 lands + the original is dot-prefix-hidden (audit bytes kept)."""
    import subprocess

    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        pytest.skip("no ffmpeg on this machine")
    src = tmp_path / "surveillance.avi"
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
         "testsrc=duration=1:size=320x240:rate=10", str(src)],
        check=True, capture_output=True,
    )
    out = convert_video_to_playable(src)
    assert out is not None and out.name == "surveillance.mp4"
    assert out.stat().st_size > 1024
    assert not src.exists()  # hidden, not deleted…
    assert (tmp_path / ".surveillance.avi").exists()  # …bytes preserved


def test_convert_garbage_leaves_original_visible(tmp_path):
    """A corrupt video must fail soft: no mp4, original stays visible."""
    if find_ffmpeg() is None:
        pytest.skip("no ffmpeg on this machine")
    bogus = tmp_path / "corrupt.avi"
    bogus.write_bytes(b"RIFF not actually a video")
    assert convert_video_to_playable(bogus) is None
    assert bogus.exists()
    assert not (tmp_path / "corrupt.mp4").exists()


def test_convert_wont_adopt_unrelated_sibling_mp4(tmp_path):
    """A pre-existing <stem>.mp4 may be an UNRELATED native file from another
    camera. Never adopt its bytes or hide our .avi against it — leave the
    original visible and bail (evidence integrity over convenience)."""
    src = tmp_path / "cam.avi"
    src.write_bytes(b"RIFF whatever")
    unrelated = tmp_path / "cam.mp4"
    unrelated.write_bytes(b"a different camera's native file")
    out = convert_video_to_playable(src)
    assert out is None                                                 # did not adopt
    assert unrelated.read_bytes() == b"a different camera's native file"  # untouched
    assert src.exists()                                                # original NOT hidden
    assert not (tmp_path / ".cam.avi").exists()


def test_convert_removes_redundant_redownloaded_original(tmp_path):
    """After a successful convert, if a hidden original already exists (prior
    run) the freshly re-downloaded visible copy is removed, not left as a
    second dead row — ZIP-path parity, audit bytes kept in the hidden copy."""
    ff = find_ffmpeg()
    if ff is None:
        pytest.skip("no ffmpeg on this machine")
    import subprocess
    src = tmp_path / "cam.avi"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=1:size=160x120:rate=8", str(src)],
                   check=True, capture_output=True)
    (tmp_path / ".cam.avi").write_bytes(b"preserved original from prior run")
    out = convert_video_to_playable(src)
    assert out is not None and out.name == "cam.mp4"
    assert not src.exists()                                            # redundant copy removed
    assert (tmp_path / ".cam.avi").read_bytes() == b"preserved original from prior run"


def test_convert_leaves_no_partial_at_target_on_total_failure(tmp_path):
    """A corrupt input leaves NO cam.mp4 (not even a partial), so a later run
    never trusts a half-written file as finished. The .converting temp is
    cleaned too."""
    if find_ffmpeg() is None:
        pytest.skip("no ffmpeg on this machine")
    bogus = tmp_path / "broken.avi"
    bogus.write_bytes(b"RIFF definitely not a real video payload" * 50)
    assert convert_video_to_playable(bogus) is None
    assert not (tmp_path / "broken.mp4").exists()
    assert not (tmp_path / ".broken.mp4.converting").exists()
    assert bogus.exists()


# ------------------------------- audio → Portal-playable m4a (2026-07-11)

from voxhora_techshare_agent.storage import (
    convert_audio_to_playable,
    is_portal_audio_candidate,
    _wav_needs_transcode,
)


def _make_audio(ff, path, codec, extra=None):
    import subprocess
    cmd = [ff, "-y", "-v", "error", "-f", "lavfi", "-i",
           "sine=frequency=440:duration=1"]
    if extra:
        cmd += extra
    cmd += ["-c:a", codec, str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


def test_audio_candidate_dispatch():
    assert is_portal_audio_candidate(Path("call.wma"))
    assert is_portal_audio_candidate(Path("CALL.WMA"))
    assert is_portal_audio_candidate(Path("note.wav"))       # wav always a candidate (probe decides)
    assert not is_portal_audio_candidate(Path("song.mp3"))    # natively playable
    assert not is_portal_audio_candidate(Path("clip.m4a"))
    assert not is_portal_audio_candidate(Path("doc.pdf"))


def test_wma_converts_to_m4a_and_hides_original(tmp_path):
    ff = find_ffmpeg()
    if ff is None:
        pytest.skip("no ffmpeg")
    src = tmp_path / "jailcall.wma"
    _make_audio(ff, src, "wmav2")
    out = convert_audio_to_playable(src)
    assert out is not None and out.name == "jailcall.m4a"
    assert out.stat().st_size > 512
    assert not src.exists()                          # original hidden…
    assert (tmp_path / ".jailcall.wma").exists()     # …bytes preserved
    # verify it's really AAC
    import subprocess
    r = subprocess.run([ff, "-hide_banner", "-i", str(out)], capture_output=True, text=True)
    assert "aac" in r.stderr.lower()


def test_pcm_wav_left_untouched(tmp_path):
    ff = find_ffmpeg()
    if ff is None:
        pytest.skip("no ffmpeg")
    src = tmp_path / "clean.wav"
    _make_audio(ff, src, "pcm_s16le")
    assert _wav_needs_transcode(src) is False        # PCM → no transcode
    out = convert_audio_to_playable(src)
    assert out is None                               # left alone
    assert src.exists()
    assert not (tmp_path / "clean.m4a").exists()


def test_nonpcm_wav_transcodes(tmp_path):
    ff = find_ffmpeg()
    if ff is None:
        pytest.skip("no ffmpeg")
    src = tmp_path / "note.wav"
    _make_audio(ff, src, "adpcm_ms")                 # non-PCM inside a .wav
    assert _wav_needs_transcode(src) is True
    out = convert_audio_to_playable(src)
    assert out is not None and out.name == "note.m4a"
    assert not src.exists()
    assert (tmp_path / ".note.wav").exists()


def test_audio_wont_adopt_unrelated_sibling_m4a(tmp_path):
    ff = find_ffmpeg()
    if ff is None:
        pytest.skip("no ffmpeg")
    src = tmp_path / "rec.wma"
    _make_audio(ff, src, "wmav2")
    unrelated = tmp_path / "rec.m4a"
    unrelated.write_bytes(b"a different recording's native m4a")
    out = convert_audio_to_playable(src)
    assert out is None                               # did not adopt
    assert unrelated.read_bytes() == b"a different recording's native m4a"
    assert src.exists()                              # original NOT hidden
    assert not (tmp_path / ".rec.wma").exists()


def test_audio_failsoft_on_garbage(tmp_path):
    if find_ffmpeg() is None:
        pytest.skip("no ffmpeg")
    bogus = tmp_path / "broken.wma"
    bogus.write_bytes(b"not actually a wma stream" * 40)
    assert convert_audio_to_playable(bogus) is None
    assert bogus.exists()
    assert not (tmp_path / "broken.m4a").exists()
    assert not (tmp_path / ".broken.m4a.converting").exists()
