"""TechShare lists the same recording under more than one name.

Guards the 2026-08-03 fix. The DME list catalogues one video several times with
different names — 16 of 33 items on C1CR26206330 carry a "(N)" suffix, and one
pair differs only by TechShare's own typo ("Deputy" vs "Deputyy"). A name-only
presence check cannot tell that two names mean one video, so each twin was
downloaded again.

Measured on Patrick's Portal: 27.39 GB of duplicated video, 24 redundant copies
across 19 size-groups. A live fetch was watched re-pulling a 3.48 GB video whose
byte-identical twin was already sitting in the same folder. That bill lands on
every attorney from their first case, in bandwidth and in Dropbox storage.

The floor is the whole safety argument: above 50 MB a coincidental byte-exact
match between two different police videos is implausible (323 files >= 50 MB
held 299 distinct sizes, and all 19 collisions were real twins). Below it,
small PDFs collide readily and a re-fetch costs seconds — so nothing changes.
"""

from pathlib import Path

from voxhora_techshare_agent import storage
from voxhora_techshare_agent.models import DMEItem


def _item(name: str, size: str) -> DMEItem:
    return DMEItem(
        name=name, type="Video - BodyCam", source="Government", size=size,
        available_date="", last_accessed_date=None, is_archived=False,
        enclosure_href=None, api_href=None,
    )


def _write(p: Path, n: int) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.truncate(n)
    return p


# ----- the real case this was built for -----

def test_twin_under_techshares_other_name_is_present(tmp_path: Path):
    """Lopez Martinez C1CR26206330, exactly as it happened live."""
    item = _item(" Deputy Skolaski, Scott (Badge ID S6359) -  ASSAULT W INJURY.mp4",
                 "3,476,025 KB")
    _write(tmp_path / " Deputyy Skolaski, Scott (Badge ID S6359)(2).mp4", 3_476_025_312)
    assert storage._same_size_twin_present(item, [tmp_path]) is True


def test_no_twin_means_genuinely_missing(tmp_path: Path):
    item = _item("missing_video.mp4", "3,476,025 KB")
    _write(tmp_path / "unrelated.mp4", 1_000_000_000)
    assert storage._same_size_twin_present(item, [tmp_path]) is False


# ----- the safety floor -----

def test_small_files_never_match_on_size(tmp_path: Path):
    """A 9 KB PDF colliding with another 9 KB PDF must NOT count as present."""
    item = _item("offense_report.pdf", "9 KB")
    _write(tmp_path / "something_else_entirely.pdf", 9_000)
    assert storage._same_size_twin_present(item, [tmp_path]) is False


def test_just_below_the_floor_is_refused(tmp_path: Path):
    below = (50 * 1024 * 1024 // 1000) - 1          # in KB, just under 50 MiB
    item = _item("v.mp4", f"{below} KB")
    _write(tmp_path / "twin.mp4", below * 1000 + 10)
    assert storage._same_size_twin_present(item, [tmp_path]) is False


def test_just_above_the_floor_is_accepted(tmp_path: Path):
    above = (50 * 1024 * 1024 // 1000) + 1
    item = _item("v.mp4", f"{above} KB")
    _write(tmp_path / "twin.mp4", above * 1000 + 10)
    assert storage._same_size_twin_present(item, [tmp_path]) is True


# ----- precision: it is a byte window, not a tolerance -----

def test_a_megabyte_off_is_not_a_twin(tmp_path: Path):
    item = _item("v.mp4", "3,476,025 KB")
    _write(tmp_path / "close_but_no.mp4", 3_476_025_312 - 1_000_000)
    assert storage._same_size_twin_present(item, [tmp_path]) is False


def test_one_byte_outside_the_window_is_not_a_twin(tmp_path: Path):
    item = _item("v.mp4", "1,000,000 KB")
    _write(tmp_path / "under.mp4", 1_000_000_000 - 1)
    _write(tmp_path / "over.mp4", 1_000_000_000 + 1_000)
    assert storage._same_size_twin_present(item, [tmp_path]) is False


def test_no_list_size_means_no_match(tmp_path: Path):
    item = _item("v.mp4", "")
    _write(tmp_path / "twin.mp4", 3_476_025_312)
    assert storage._same_size_twin_present(item, [tmp_path]) is False


def test_directories_are_not_twins(tmp_path: Path):
    item = _item("v.mp4", "3,476,025 KB")
    (tmp_path / "a_folder").mkdir()
    assert storage._same_size_twin_present(item, [tmp_path]) is False


def test_missing_portal_dir_is_survivable(tmp_path: Path):
    item = _item("v.mp4", "3,476,025 KB")
    assert storage._same_size_twin_present(item, [tmp_path / "nope"]) is False


# ----- wired into the presence check -----

def test_is_present_in_portal_accepts_the_twin(tmp_path, monkeypatch):
    """The whole point: cmd_fetch must stop re-downloading it."""
    item = _item(" Deputy Skolaski, Scott (Badge ID S6359) -  ASSAULT W INJURY.mp4",
                 "3,476,025 KB")
    _write(tmp_path / " Deputyy Skolaski, Scott (Badge ID S6359)(2).mp4", 3_476_025_312)
    monkeypatch.setattr(storage.config, "portal_case_dirs", lambda cause: [tmp_path])
    assert storage.is_present_in_portal(item, "C1CR26206330") is True


def test_is_present_in_portal_still_reports_a_real_gap(tmp_path, monkeypatch):
    """A missing video must stay missing — the floor buys nothing if this fails."""
    item = _item("genuinely_absent.mp4", "3,476,025 KB")
    _write(tmp_path / "different_size.mp4", 1_111_111_111)
    monkeypatch.setattr(storage.config, "portal_case_dirs", lambda cause: [tmp_path])
    assert storage.is_present_in_portal(item, "C1CR26206330") is False
