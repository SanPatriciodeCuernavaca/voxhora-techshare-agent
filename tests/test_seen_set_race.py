"""The seen-DME write race (2026-10-12, auto written discovery plan R1).

Every agent process shares ONE seen_dme_ids.json: a multi-hour video fetch,
a written-discovery fetch, process-email, the PC backfill. Each loads the set
once at start, then after every item adds its fingerprint and rewrites the
WHOLE file from its in-memory copy — so a long run erased every fingerprint a
sibling added meanwhile, and those PCs and files were downloaded again.

Four REAL processes (production is separate PIDs), released together by a
start barrier, each run the exact fetch
pattern — load once, then add + save per item — under a sandbox HOME. Every
fingerprint from both must survive. Fails against the whole-file rewrite;
passes once save is a locked read-merge-write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


CHILD = """
import sys, time
from pathlib import Path
from voxhora_techshare_agent import storage
tag, go = sys.argv[1], Path(sys.argv[2])
seen = storage.load_seen_dme_ids()          # once, at run start — like cmd_fetch
while not go.exists():                      # start barrier: both children save
    time.sleep(0.001)                       # at the same moment, every run
for i in range(300):
    seen.add(f"dmeId:{tag}-{i:04d}")
    storage.save_seen_dme_ids(seen)          # after every item — like cmd_fetch
"""


def test_concurrent_runs_do_not_erase_each_others_fingerprints(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path))
    go = tmp_path / "go"
    procs = [
        subprocess.Popen([sys.executable, "-c", CHILD, tag, str(go)], env=env)
        for tag in ("video-run", "written-run", "process-email", "backfill")
    ]
    time.sleep(0.5)                          # let both load their stale copy
    go.touch()
    for p in procs:
        assert p.wait(timeout=120) == 0, "child fetch process failed"

    files = list(tmp_path.rglob("seen_dme_ids.json"))
    assert len(files) == 1, f"expected one sandboxed seen file, found {files}"
    seen = set(json.loads(files[0].read_text()))
    assert len(seen) == 1200, (
        f"lost {1200 - len(seen)} fingerprints to the write race — "
        "each lost one is a PC or file downloaded again"
    )


def test_save_only_adds(tmp_path, monkeypatch):
    """A stale in-memory copy can never shrink the file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from voxhora_techshare_agent import storage

    storage.save_seen_dme_ids({"a", "b", "c"})
    storage.save_seen_dme_ids({"a"})            # an older process's view
    assert storage.load_seen_dme_ids() == {"a", "b", "c"}


def test_save_returns_what_siblings_added(tmp_path, monkeypatch):
    """A long run learns mid-run what a sibling already downloaded, so it
    does not fetch the same PC or file a second time (`seen |= save(seen)`)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from voxhora_techshare_agent import storage

    long_run = storage.load_seen_dme_ids()      # loaded at its start: empty
    storage.save_seen_dme_ids({"pc-from-process-email"})   # a sibling, meanwhile
    long_run.add("video-1")
    long_run |= storage.save_seen_dme_ids(long_run)
    assert "pc-from-process-email" in long_run


def test_fetch_skips_a_pc_a_sibling_delivered_mid_run(tmp_path, monkeypatch):
    """End to end through cmd_fetch with the REAL seen file: while this run
    downloads a small document, process-email delivers the case's PC. When the
    run reaches the PC it must not fetch it again. Fails if the call sites go
    back to `storage.save_seen_dme_ids(seen)` without absorbing the result."""
    import argparse
    from voxhora_techshare_agent import cli, storage
    from voxhora_techshare_agent.models import DMEItem

    monkeypatch.setenv("HOME", str(tmp_path))

    def item(name, type_, size, dme_id):
        return DMEItem(
            name=name, type=type_, source="Government", size=size,
            available_date="2026-10-12", last_accessed_date=None, is_archived=False,
            enclosure_href=f"https://x/dmefile?dmeId={dme_id}&isStream=1", api_href=None,
        )

    doc = item("Offense Report.pdf", "Offense Report", "9 KB", "aaaa")
    pc = item("C1CR26000000.pdf", "PC Affidavit / Arrest Warrant", "50 KB", "bbbb")

    class Client:
        pc_downloads = 0

        def get_case_detail(self, service_id, case_uuid):
            return {"uuid": case_uuid}

        def get_dme_list(self, service_id, case):
            return [pc, doc]

        def pc_affidavits_in(self, items):
            return [i for i in items if i.is_pc_affidavit]

        def download_dme_file_to_path(self, service_id, it, path):
            # Smallest first, so this is the document. Meanwhile, a sibling
            # process (process-email) delivers the PC and records it.
            storage.save_seen_dme_ids({storage.dme_fingerprint(pc)})
            low = storage.expected_size_range(it)[0]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * low)
            return low

        def download_dme_file(self, service_id, it):
            Client.pc_downloads += 1
            return b"%PDF-1.4 pc"

    class Session:
        def ensure_authenticated(self):
            pass

    monkeypatch.setattr(cli, "_resolve_case", lambda c: {"service_id": "svc", "case_uuid": "uuid"})
    monkeypatch.setattr(cli, "TechShareSession", lambda: Session())
    monkeypatch.setattr(cli, "TechShareClient", lambda s: Client())
    monkeypatch.setattr(
        storage, "case_discovery_target_path",
        lambda it, cause, target_dir=None: tmp_path / "case" / it.name,
    )
    monkeypatch.setattr(storage, "write_pc_affidavit", lambda *a, **k: tmp_path / "inbox.pdf")

    cli.cmd_fetch(argparse.Namespace(
        cause_number="C1CR26000000", service_id="svc", case_uuid="uuid",
        item_ids=None, target_dir=str(tmp_path / "case"), manifest=None,
    ))

    assert Client.pc_downloads == 0, (
        "the PC a sibling delivered mid-run was downloaded a second time"
    )
