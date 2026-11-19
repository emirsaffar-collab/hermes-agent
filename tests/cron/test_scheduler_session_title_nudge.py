"""Tests for cron/scheduler.py — cron session titling and the repeated-failure review nudge."""

from unittest.mock import MagicMock, patch


class TestSetCronSessionTitle:
    """Robust cron session titling: #50535/#50536/#50537."""

    def test_dedupes_on_duplicate_title(self):
        # First write collides (ValueError); helper falls back to lineage #N.
        from cron.scheduler import _set_cron_session_title
        sessions = MagicMock()
        sessions.set_title.side_effect = [ValueError("in use"), True]
        sessions.next_title_in_lineage.return_value = "Nightly Synthesis #2"
        out = _set_cron_session_title(sessions, "sess-1", "Nightly Synthesis")
        assert out == "Nightly Synthesis #2"
        sessions.next_title_in_lineage.assert_called_once_with("Nightly Synthesis")

    def test_early_owner_exit_stamps_session_title(self):
        from types import SimpleNamespace
        from cron.scheduler import _stamp_early_owner_session_title
        sessions = MagicMock()
        sessions.set_title.return_value = True
        authority = SimpleNamespace(persistence=SimpleNamespace(sessions=sessions))
        owner_exec = (authority, "local-early-123", "job-42", "exec-1")
        _stamp_early_owner_session_title(owner_exec, "job-42", "chat247-responder")
        sessions.set_title.assert_called_once()
        sid, title = sessions.set_title.call_args[0]
        assert sid == "local-early-123"
        assert title.startswith("chat247-responder · ")

    def test_finalize_cron_session_stamps_compressed_ancestors(self):
        from cron.scheduler import _finalize_cron_session
        session_db = MagicMock()
        session_db.lineage.tip.return_value = "20261119_tip"
        session_db.lineage.chain.return_value = ("local-root", "20261119_mid", "20261119_tip")
        session_db.sessions.set_title.return_value = True
        session_db.sessions.lifecycle_statuses.return_value = {}
        _finalize_cron_session(session_db, MagicMock(), "job-99", "lager-builder-owner", "local-root")
        titled = {call.args[0]: call.args[1] for call in session_db.sessions.set_title.call_args_list}
        assert "20261119_tip" in titled
        assert titled["local-root"] == f"{titled['20261119_tip']} (seg 1)"
        assert titled["20261119_mid"] == f"{titled['20261119_tip']} (seg 2)"

class TestFailureStreakNudge:
    """Poke-inspired repeated-failure review nudge (_failure_streak_nudge)."""

    def _job(self, streak, kind="cron", name="scout"):
        return {
            "id": "j1",
            "name": name,
            "failure_streak": streak,
            "schedule": {"kind": kind},
        }

    def test_silent_below_threshold(self):
        from cron.scheduler import _failure_streak_nudge
        with patch("cron.scheduler.load_config", return_value={}):
            assert _failure_streak_nudge(self._job(0)) == ""
            assert _failure_streak_nudge(self._job(1)) == ""

    def test_oneshot_never_nudges(self):
        from cron.scheduler import _failure_streak_nudge
        with patch("cron.scheduler.load_config", return_value={}):
            assert _failure_streak_nudge(self._job(10, kind="once")) == ""

    def test_config_threshold_and_disable(self):
        from cron.scheduler import _failure_streak_nudge
        cfg5 = {"cron": {"failure_nudge_threshold": 5}}
        with patch("cron.scheduler.load_config", return_value=cfg5):
            assert _failure_streak_nudge(self._job(3)) == ""
            assert _failure_streak_nudge(self._job(4))
        with patch("cron.scheduler.load_config", return_value={"cron": {"failure_nudge_threshold": 0}}):
            assert _failure_streak_nudge(self._job(50)) == ""

    def test_missing_streak_field_backcompat(self):
        from cron.scheduler import _failure_streak_nudge
        job = {"id": "old", "schedule": {"kind": "interval"}}  # pre-field job
        with patch("cron.scheduler.load_config", return_value={}):
            assert _failure_streak_nudge(job) == ""
