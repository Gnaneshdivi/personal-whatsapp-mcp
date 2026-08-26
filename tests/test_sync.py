from __future__ import annotations

from wa_mcp.whatsapp.sync import Phase, SyncTracker


def tracker(clock=None) -> SyncTracker:
    t = SyncTracker()
    if clock is not None:
        t._clock = clock
    return t


def test_a_fresh_install_is_not_ready():
    t = tracker()
    assert t.state.phase is Phase.UNPAIRED
    assert t.state.ready is False


def test_the_gate_stays_shut_during_sync():
    t = tracker()
    t.connecting()
    t.connected()
    t.offline_preview(total=1200, Message=900, Receipts=300)
    assert t.state.phase is Phase.SYNCING
    assert t.state.ready is False

    t.saw_event(600)
    assert t.state.ready is False

    t.offline_completed(1200)
    assert t.state.ready is True


def test_percent_tracks_the_offline_queue():
    t = tracker()
    t.connected()
    t.offline_preview(total=1000)
    t.saw_event(250)
    assert t.state.percent == 25.0
    t.saw_event(250)
    assert t.state.percent == 50.0


def test_percent_never_shows_100_before_it_is_done():
    t = tracker()
    t.connected()
    t.offline_preview(total=10)
    t.saw_event(10)
    assert t.state.percent == 99.0
    assert t.state.ready is False
    t.offline_completed(10)
    assert t.state.percent == 100.0


def test_history_progress_is_the_fallback_denominator():
    t = tracker()
    t.connected()
    t.history_chunk("INITIAL_BOOTSTRAP", progress=40.0, conversations=12)
    assert t.state.percent == 40.0
    assert t.state.history_type == "INITIAL_BOOTSTRAP"
    assert t.state.chats_synced == 12
    t.history_chunk("INITIAL_BOOTSTRAP", progress=80.0, conversations=9)
    assert t.state.percent == 80.0
    assert t.state.chats_synced == 21


def test_offline_queue_wins_over_history_progress():
    t = tracker()
    t.connected()
    t.offline_preview(total=100)
    t.history_chunk("RECENT", progress=90.0)
    t.saw_event(10)
    assert t.state.percent == 10.0


def test_sync_that_never_completes_still_settles():
    now = [1000.0]
    t = tracker(clock=lambda: now[0])
    t.connecting()
    t.connected()
    t.offline_preview(total=50)

    now[0] += 30
    t.tick()
    assert t.state.ready is False

    now[0] += 120
    t.tick()
    assert t.state.ready is True
    assert "timeout" in t.state.detail


def test_tick_does_nothing_once_ready():
    now = [1000.0]
    t = tracker(clock=lambda: now[0])
    t.connected()
    t.offline_completed(0)
    assert t.state.ready
    now[0] += 10_000
    t.tick()
    assert t.state.ready


def test_events_before_sync_starts_are_not_counted():
    t = tracker()
    t.saw_event(5)
    assert t.state.done == 0


def test_logout_reopens_the_gate():
    t = tracker()
    t.connected()
    t.offline_completed(0)
    assert t.state.ready
    t.logged_out()
    assert t.state.phase is Phase.LOGGED_OUT
    assert t.state.ready is False


def test_reconnect_does_not_reset_a_ready_session():
    t = tracker()
    t.connected()
    t.offline_completed(0)
    t.connected()
    assert t.state.ready is True


def test_public_shape_is_what_the_ui_polls():
    t = tracker()
    t.connecting()
    t.connected()
    t.offline_preview(total=800, Message=800)
    t.saw_event(200)
    p = t.state.public()
    assert p["phase"] == "syncing"
    assert p["ready"] is False
    assert p["percent"] == 25.0
    assert p["total"] == 800 and p["done"] == 200
    assert "800" in p["detail"]
