"""Every test runs with a throwaway HOME.

2026-10-12 — the agent's state (seen set, case cache, discovery-event spool,
cookies, last run) and its logs all live under Path.home(). Tests that call a
command without sandboxing it wrote into the attorney's REAL state folder on
the Mac running the suite (caught when the new discovery-event spool appeared
there with a test cause number). Path.home() follows HOME, so pointing HOME at
a per-test temp folder covers every path at once; a test that patches
config.state_dir itself still wins.
"""

import pytest


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture(autouse=True)
def _no_leftover_stderr_handler():
    """main() installs the piped-stderr handler once per process; a test that
    calls main() must not leave it (and its spent byte budget) to the next."""
    import logging
    yield
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_voxhora_stderr", False)]:
        root.removeHandler(h)
