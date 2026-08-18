"""The case-uuid cache write race (2026-08-18, John Purgason).

Every multi-cause appointment spawns add-cause once PER CAUSE, simultaneously
(AutoIntakeWatcher fires the pair within a millisecond). Each process did
load-cache → add its cause → write whole file: both load, each writes its own
cause, and the last writer erases the sibling's entry. C1CR26205548 lost its
registration exactly that way — refresh said "not in cache" forever, so no PC
could ever fetch, while its sibling -549 sat registered right beside it.

Two REAL processes (not threads — production is separate PIDs) each register
120 causes into one cache under a sandbox HOME. Every registration must
survive. Fails against the unlocked read-modify-write; passes once the upsert
holds a cross-process lock around the read-merge-write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


CHILD = """
import sys
from voxhora_techshare_agent import storage
tag = sys.argv[1]
for i in range(120):
    storage.upsert_case_cache_entry(
        f"C1CR{tag}{i:04d}",
        {"case_uuid": f"u-{tag}-{i}", "service_id": "s", "backend_port": 1030},
    )
"""


def test_concurrent_registrations_do_not_lose_entries(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path))
    procs = [
        subprocess.Popen([sys.executable, "-c", CHILD, tag], env=env)
        for tag in ("11", "22")
    ]
    for p in procs:
        assert p.wait(timeout=120) == 0, "child registration process failed"

    caches = list(tmp_path.rglob("case_uuid_cache.json"))
    assert len(caches) == 1, f"expected one sandboxed cache, found {caches}"
    cache = json.loads(caches[0].read_text())
    assert len(cache) == 240, (
        f"lost {240 - len(cache)} registrations to the write race — "
        "each lost cause is a client whose PC can never auto-fetch"
    )
