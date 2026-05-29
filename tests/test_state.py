from datetime import UTC, datetime, timedelta

from optimizarr.state import SATISFIED, StateManager


def _mgr(tmp_path):
    return StateManager(str(tmp_path / "state.json"))


def test_missing_file_starts_empty(tmp_path):
    m = _mgr(tmp_path)
    assert m.get("radarr", 1) is None


def test_mark_satisfied_and_persist(tmp_path):
    path = tmp_path / "state.json"
    m = StateManager(str(path))
    m.mark_satisfied("radarr", 42)
    assert path.exists()

    reloaded = StateManager(str(path))
    entry = reloaded.get("radarr", 42)
    assert entry is not None
    assert entry.status == SATISFIED


def test_load_ignores_legacy_fields(tmp_path):
    # Old state files carried in_flight/file_id/guid; loading must not choke on them.
    path = tmp_path / "state.json"
    path.write_text(
        '{"radarr": {"7": {"status": "satisfied", "updated_at": "2026-05-28T00:00:00+00:00",'
        ' "file_id": 9, "guid": "x"}}}'
    )
    m = StateManager(str(path))
    entry = m.get("radarr", 7)
    assert entry is not None
    assert entry.status == SATISFIED


def test_is_active_lifecycle(tmp_path):
    m = _mgr(tmp_path)
    now = datetime.now(UTC)  # marks stamp wall-clock time; base offsets on real now

    # Unprocessed -> active
    assert m.is_active("radarr", 1, now, reevaluate_after_days=30)

    # Satisfied within window -> not active; past window -> active again
    m.mark_satisfied("radarr", 2)
    assert not m.is_active("radarr", 2, now + timedelta(days=10), reevaluate_after_days=30)
    assert m.is_active("radarr", 2, now + timedelta(days=31), reevaluate_after_days=30)
