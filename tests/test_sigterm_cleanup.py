"""A cancel (SIGTERM from the Mac app) cleans up instead of orphaning (2026-10-12).

Python's default SIGTERM death skips every finally block and leaves an ffmpeg
started by subprocess.run running — still writing its fixed `.converting`
temp, which the next run's ffmpeg (`-y`, same name) writes too. Now the run
unwinds (children killed, temps removed) and still dies BY the signal, which
is how the app recognises a cancel. Plan: STEP 3 v2.1, A2 / P-N3.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from voxhora_techshare_agent import cli, storage


CHILD = r"""
import subprocess, sys
from pathlib import Path
from voxhora_techshare_agent import cli

pid_file, marker = sys.argv[1], Path(sys.argv[2])

def command():
    try:
        # Stand-in for an ffmpeg conversion: a grandchild that records its pid.
        subprocess.run([sys.executable, "-c",
            "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)",
            pid_file], capture_output=True, timeout=120)
    finally:
        marker.write_text("cleanup ran")
    return 0

cli._run_guarded("test-run", command)
"""


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_sigterm_kills_the_converter_runs_cleanup_and_exits_by_signal(tmp_path):
    pid_file, marker = tmp_path / "grandchild.pid", tmp_path / "marker"
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(pid_file), str(marker)])
    deadline = time.monotonic() + 20
    while not (pid_file.exists() and pid_file.read_text()):
        assert time.monotonic() < deadline, "grandchild never started"
        time.sleep(0.05)
    grandchild = int(pid_file.read_text())

    try:
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=20) == -signal.SIGTERM, "the app reads a cancel from the signal exit"
    finally:
        child.kill()
        _kill_quietly(grandchild)
    assert marker.read_text() == "cleanup ran"
    deadline = time.monotonic() + 5
    while _alive(grandchild):
        assert time.monotonic() < deadline, "the converter outlived the cancelled agent"
        time.sleep(0.05)


def test_handler_is_restored_after_a_normal_run():
    def sentinel(signum, frame):
        pass
    previous = signal.signal(signal.SIGTERM, sentinel)
    try:
        assert cli._run_guarded("test-run", lambda: 7) == 7
        assert signal.getsignal(signal.SIGTERM) is sentinel
    finally:
        signal.signal(signal.SIGTERM, previous)


SLOW_CLEANUP = r"""
import sys, time
from pathlib import Path
from voxhora_techshare_agent import cli
ready, marker, cleanup_seconds = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
cli._CLEANUP_WATCHDOG_SECONDS = 1.0

def command():
    try:
        ready.write_text("go")
        time.sleep(60)
    finally:
        time.sleep(cleanup_seconds)        # cleanup that takes a while (or stalls)
        marker.write_text("cleanup ran")
    return 0

cli._run_guarded("test-run", command)
"""


def _start_slow(tmp_path, cleanup_seconds):
    ready, marker = tmp_path / "ready", tmp_path / "marker"
    child = subprocess.Popen([sys.executable, "-c", SLOW_CLEANUP,
                              str(ready), str(marker), str(cleanup_seconds)])
    deadline = time.monotonic() + 20
    while not ready.exists():
        assert time.monotonic() < deadline, "child never started"
        time.sleep(0.05)
    return child, marker


def test_a_second_sigterm_does_not_cut_cleanup_short(tmp_path):
    child, marker = _start_slow(tmp_path, cleanup_seconds=0.5)
    try:
        child.send_signal(signal.SIGTERM)
        time.sleep(0.2)
        child.send_signal(signal.SIGTERM)          # ignored while cleaning up
        assert child.wait(timeout=20) == -signal.SIGTERM
        assert marker.read_text() == "cleanup ran"
    finally:
        child.kill()


def test_a_stalled_cleanup_is_ended_by_the_watchdog(tmp_path):
    child, marker = _start_slow(tmp_path, cleanup_seconds=60)
    try:
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=20) == -signal.SIGKILL, "a stuck agent must not live on"
        assert not marker.exists()
    finally:
        child.kill()


MAIN_CHILD = r"""
import subprocess, sys
from voxhora_techshare_agent import cli
ready = sys.argv[1]

def blocking_status(args):
    subprocess.run([sys.executable, "-c",
        "import sys, time; open(sys.argv[1], 'w').write('go'); time.sleep(60)", ready])
    return 0

cli.cmd_status = blocking_status
sys.exit(cli.main(["status"]))
"""


def test_main_runs_commands_under_the_guard(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path))
    ready = tmp_path / "ready"
    child = subprocess.Popen([sys.executable, "-c", MAIN_CHILD, str(ready)], env=env,
                             stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while not ready.exists():
        assert time.monotonic() < deadline, "command never started"
        time.sleep(0.05)
    try:
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=20) == -signal.SIGTERM
    finally:
        child.kill()
    logs = list(tmp_path.rglob("agent-*.log"))
    assert logs and "terminated by SIGTERM" in logs[0].read_text()


def _cancel(*_a, **_k):
    raise cli.AgentTerminated()


def test_cancel_mid_video_conversion_leaves_no_converting_temp(tmp_path, monkeypatch):
    video = tmp_path / "Store camera.avi"
    video.write_bytes(b"x" * 2048)
    monkeypatch.setattr(storage, "find_ffmpeg", lambda: "ffmpeg")

    def encode_then_cancel(args, **_k):
        Path(args[-1]).write_bytes(b"half an encode")      # ffmpeg's output temp
        raise cli.AgentTerminated()

    monkeypatch.setattr(subprocess, "run", encode_then_cancel)
    with pytest.raises(cli.AgentTerminated):
        storage.convert_video_to_playable(video)
    assert not list(tmp_path.glob("*.converting")) and not list(tmp_path.glob(".*.converting"))
    assert video.exists(), "the original evidence is untouched"


def test_cancel_mid_audio_conversion_leaves_no_converting_temp(tmp_path, monkeypatch):
    audio = tmp_path / "Jail call.wma"
    audio.write_bytes(b"x" * 2048)
    monkeypatch.setattr(storage, "find_ffmpeg", lambda: "ffmpeg")

    def encode_then_cancel(args, **_k):
        Path(args[-1]).write_bytes(b"half an encode")
        raise cli.AgentTerminated()

    monkeypatch.setattr(subprocess, "run", encode_then_cancel)
    with pytest.raises(cli.AgentTerminated):
        storage.convert_audio_to_playable(audio)
    assert not list(tmp_path.glob(".*.converting"))
    assert audio.exists()


def test_cancel_mid_atomic_write_leaves_no_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "fsync", _cancel)
    with pytest.raises(cli.AgentTerminated):
        storage.atomic_write_bytes(tmp_path / "C1CR26000000.pdf", b"%PDF")
    assert list(tmp_path.iterdir()) == []


def _three_member_zip(tmp_path):
    zpath = tmp_path / "Disclosure.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            zf.writestr(name, b"%PDF" * 1000)
    return zpath


def test_cancel_mid_zip_rolls_back_and_the_rerun_has_no_duplicates(tmp_path, monkeypatch):
    """Cancel after the first member: nothing half-extracted may stay (it looked
    like the whole bundle, and the re-run added `_2` copies of every member)."""
    import shutil
    zpath = _three_member_zip(tmp_path)
    real_copy, calls = shutil.copyfileobj, []

    def copy_then_cancel_on_second(src, dst, *a, **k):
        calls.append(Path(dst.name).name)
        if len(calls) == 2:
            dst.write(b"%PDF")
            raise cli.AgentTerminated()
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "copyfileobj", copy_then_cancel_on_second)
    with pytest.raises(cli.AgentTerminated):
        storage.extract_zip_inplace(zpath)
    assert not (tmp_path / "Disclosure").exists(), "no half-filled folder left"
    assert zpath.exists(), "the ZIP stays for the next run"

    monkeypatch.setattr(shutil, "copyfileobj", real_copy)
    assert storage.extract_zip_inplace(zpath) == 3
    assert sorted(p.name for p in (tmp_path / "Disclosure").iterdir()) == ["a.pdf", "b.pdf", "c.pdf"]


def test_members_are_written_under_a_temp_name_the_uploader_skips(tmp_path, monkeypatch):
    """The streaming uploader ships files at their final names mid-download;
    a member must never sit at its final name half-written."""
    import shutil
    zpath = _three_member_zip(tmp_path)
    real_copy, names = shutil.copyfileobj, []

    def spy(src, dst, *a, **k):
        names.append(Path(dst.name).name)
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "copyfileobj", spy)
    storage.extract_zip_inplace(zpath)
    assert names and all(n.startswith(".") and n.endswith(".tmp") for n in names), names


def test_an_ordinary_error_mid_zip_also_rolls_back(tmp_path, monkeypatch):
    import shutil
    zpath = _three_member_zip(tmp_path)
    real_copy, calls = shutil.copyfileobj, []

    def copy_then_fail(src, dst, *a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("No space left on device")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "copyfileobj", copy_then_fail)
    assert storage.extract_zip_inplace(zpath) == 0
    assert not (tmp_path / "Disclosure").exists()


def test_failed_rename_in_atomic_write_leaves_no_temp(tmp_path, monkeypatch):
    def refuse(self, target):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(OSError):
        storage.atomic_write_bytes(tmp_path / "C1CR26000000.pdf", b"%PDF")
    assert list(tmp_path.iterdir()) == []


def test_member_names_at_the_filename_limit_still_extract(tmp_path):
    """A temp name built from the member's name pushed 234-255-byte names over
    the 255-byte limit, and the whole ZIP failed on every run."""
    long_name = ("Deputy Officer Body Camera Exhibit " * 8)[:246] + ".pdf"   # 250 bytes
    assert len(long_name.encode()) == 250
    zpath = tmp_path / "Officer files.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(long_name, b"%PDF" * 100)
    assert storage.extract_zip_inplace(zpath) == 1
    assert (tmp_path / "Officer files" / long_name).exists()


def test_atomic_write_handles_a_name_at_the_limit(tmp_path):
    long_name = "x" * 250 + ".txt"
    storage.atomic_write_bytes(tmp_path / long_name, b"ok")
    assert (tmp_path / long_name).read_bytes() == b"ok"
