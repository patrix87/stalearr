from datetime import UTC, datetime, timedelta

from optimizarr.state import IN_FLIGHT, SATISFIED, StateManager


def _mgr(tmp_path):
    return StateManager(str(tmp_path / "state.json"))


def test_missing_file_starts_empty(tmp_path):
    m = _mgr(tmp_path)
    assert m.get("radarr", 1) is None
    assert m.in_flight_ids("radarr") == set()


def test_mark_satisfied_and_persist(tmp_path):
    path = tmp_path / "state.json"
    m = StateManager(str(path))
    m.mark_satisfied("radarr", 42, file_id=99)
    assert path.exists()

    reloaded = StateManager(str(path))
    entry = reloaded.get("radarr", 42)
    assert entry is not None
    assert entry.status == SATISFIED
    assert entry.file_id == 99


def test_mark_in_flight_tracked(tmp_path):
    m = _mgr(tmp_path)
    m.mark_in_flight("sonarr", 7, guid="abc", file_id_at_grab=3)
    assert m.in_flight_ids("sonarr") == {7}
    entry = m.get("sonarr", 7)
    assert entry is not None
    assert entry.status == IN_FLIGHT
    assert entry.guid == "abc"
    assert entry.file_id_at_grab == 3


def test_clear_resets_to_unprocessed(tmp_path):
    m = _mgr(tmp_path)
    m.mark_in_flight("radarr", 1, guid="g", file_id_at_grab=None)
    m.clear("radarr", 1)
    assert m.get("radarr", 1) is None
    assert m.in_flight_ids("radarr") == set()


def test_is_active_lifecycle(tmp_path):
    m = _mgr(tmp_path)
    now = datetime(2026, 5, 28, tzinfo=UTC)

    # Unprocessed -> active
    assert m.is_active("radarr", 1, now, reevaluate_after_days=30)

    # In flight -> not active
    m.mark_in_flight("radarr", 1, guid="g", file_id_at_grab=None)
    assert not m.is_active("radarr", 1, now, reevaluate_after_days=30)

    # Satisfied within window -> not active
    m.mark_satisfied("radarr", 2, file_id=5)
    assert not m.is_active("radarr", 2, now + timedelta(days=10), reevaluate_after_days=30)

    # Satisfied past window -> active again
    assert m.is_active("radarr", 2, now + timedelta(days=31), reevaluate_after_days=30)
