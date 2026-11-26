"""Tests for the google_meet plugin.

Covers the safety-gated pieces that don't require Playwright:

  * URL regex — only ``https://meet.google.com/`` URLs pass
  * Meeting-id extraction from Meet URLs
  * Status / transcript writes round-trip through the file-backed state
  * Tool handlers return well-formed JSON under all branches
  * Process manager refuses unsafe URLs and clears stale state cleanly
  * ``_on_session_end`` hook is defensive (no-ops when no bot active)

Does NOT spawn a real Chromium — we mock ``subprocess.Popen`` where needed.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


# ---------------------------------------------------------------------------
# URL safety gate
# ---------------------------------------------------------------------------

def test_is_safe_meet_url_accepts_standard_meet_codes():
    from plugins.google_meet.meet_bot import _is_safe_meet_url

    assert _is_safe_meet_url("https://meet.google.com/abc-defg-hij")
    assert _is_safe_meet_url("https://meet.google.com/abc-defg-hij?pli=1")
    assert _is_safe_meet_url("https://meet.google.com/new")
    assert _is_safe_meet_url("https://meet.google.com/lookup/ABC123")


def test_meeting_id_extraction():
    from plugins.google_meet.meet_bot import _meeting_id_from_url

    assert _meeting_id_from_url("https://meet.google.com/abc-defg-hij") == "abc-defg-hij"
    assert _meeting_id_from_url("https://meet.google.com/abc-defg-hij?pli=1") == "abc-defg-hij"
    # fallback for codes we can't parse (e.g. /new before redirect)
    fallback = _meeting_id_from_url("https://meet.google.com/new")
    assert fallback.startswith("meet-")


# ---------------------------------------------------------------------------
# _BotState — transcript + status file round-trip
# ---------------------------------------------------------------------------

def test_bot_state_dedupes_captions_and_flushes_status(tmp_path):
    from plugins.google_meet.meet_bot import _BotState

    out = tmp_path / "session"
    state = _BotState(out_dir=out, meeting_id="abc-defg-hij",
                      url="https://meet.google.com/abc-defg-hij")

    state.record_caption("Alice", "Hey everyone")
    state.record_caption("Alice", "Hey everyone")  # dup — ignored
    state.record_caption("Bob", "Let's start")

    transcript = (out / "transcript.txt").read_text()
    assert "Alice: Hey everyone" in transcript
    assert "Bob: Let's start" in transcript
    # dedup — Alice line appears exactly once
    assert transcript.count("Alice: Hey everyone") == 1

    status = json.loads((out / "status.json").read_text())
    assert status["meetingId"] == "abc-defg-hij"
    assert status["transcriptLines"] == 2
    assert status["transcriptPath"].endswith("transcript.txt")


def test_parse_duration():
    from plugins.google_meet.meet_bot import _parse_duration

    assert _parse_duration("30m") == 30 * 60
    assert _parse_duration("2h") == 2 * 3600
    assert _parse_duration("90s") == 90
    assert _parse_duration("90") == 90
    assert _parse_duration("") is None
    assert _parse_duration("bogus") is None


# ---------------------------------------------------------------------------
# process_manager — refuses unsafe URLs, manages active pointer
# ---------------------------------------------------------------------------

def test_start_refuses_unsafe_url():
    from plugins.google_meet import process_manager as pm

    res = pm.start("https://evil.example.com/abc-defg-hij")
    assert res["ok"] is False
    assert "refusing" in res["error"]


def test_status_reports_no_active_meeting():
    from plugins.google_meet import process_manager as pm

    assert pm.status()["ok"] is False
    assert pm.transcript()["ok"] is False
    assert pm.stop()["ok"] is False


def test_transcript_reads_last_n_lines(tmp_path):
    from plugins.google_meet import process_manager as pm

    meeting_dir = Path(os.environ["HERMES_HOME"]) / "workspace" / "meetings" / "abc-defg-hij"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / "transcript.txt").write_text(
        "[10:00:00] Alice: one\n"
        "[10:00:01] Bob: two\n"
        "[10:00:02] Alice: three\n"
    )
    pm._write_active({
        "pid": 0, "meeting_id": "abc-defg-hij",
        "out_dir": str(meeting_dir),
        "url": "https://meet.google.com/abc-defg-hij",
        "started_at": 0,
    })

    res = pm.transcript(last=2)
    assert res["ok"] is True
    assert res["total"] == 3
    assert len(res["lines"]) == 2
    assert res["lines"][-1].endswith("Alice: three")


def test_stop_signals_process_and_clears_pointer(tmp_path):
    from plugins.google_meet import process_manager as pm

    pm._write_active({
        "pid": 11111, "meeting_id": "x-y-z",
        "out_dir": str(tmp_path / "x-y-z"),
        "url": "https://meet.google.com/x-y-z",
        "started_at": 0,
    })

    alive_seq = iter([True, True, False])  # alive at first, gone after SIGTERM
    def _alive(pid):
        try:
            return next(alive_seq)
        except StopIteration:
            return False

    sent = []
    def _kill(pid, sig):
        sent.append((pid, sig))

    with patch.object(pm, "_pid_alive", side_effect=_alive), \
         patch.object(pm.os, "kill", side_effect=_kill), \
         patch.object(pm.time, "sleep", lambda _s: None):
        res = pm.stop()

    assert res["ok"] is True
    assert (11111, signal.SIGTERM) in sent
    # .active.json cleared
    assert pm._read_active() is None


# ---------------------------------------------------------------------------
# Tool handlers — JSON shape + safety gates
# ---------------------------------------------------------------------------

def test_meet_join_handler_missing_url_returns_error():
    from plugins.google_meet.tools import handle_meet_join

    out = json.loads(handle_meet_join({}))
    assert out["success"] is False
    assert out["error"]


# ---------------------------------------------------------------------------
# _on_session_end — defensive cleanup
# ---------------------------------------------------------------------------

def test_on_session_end_noop_when_nothing_active():
    from plugins.google_meet import _on_session_end
    # Should not raise and should not call stop().
    with patch("plugins.google_meet.pm.stop") as stop_mock:
        _on_session_end()
    stop_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Plugin register() — platform gating + tool registration
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# v2: process_manager.enqueue_say + realtime-mode passthrough
# ---------------------------------------------------------------------------

def test_enqueue_say_requires_text():
    from plugins.google_meet import process_manager as pm
    assert pm.enqueue_say("")["ok"] is False
    assert pm.enqueue_say("   ")["ok"] is False


# ---------------------------------------------------------------------------
# v3: NodeClient routing from tool handlers
# ---------------------------------------------------------------------------


def test_cli_register_includes_node_subcommand():
    """`hermes meet` argparse tree includes the node subtree."""
    import argparse
    from plugins.google_meet.cli import register_cli

    parser = argparse.ArgumentParser(prog="hermes meet")
    register_cli(parser)

    # Parse a known-good node invocation to prove the subtree is wired.
    ns = parser.parse_args(["node", "list"])
    assert ns.meet_command == "node"
    assert ns.node_cmd == "list"


# ---------------------------------------------------------------------------
# v2.1: new _BotState fields + status dict shape
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Admission detection + barge-in helper
# ---------------------------------------------------------------------------

def test_looks_like_human_speaker():
    from plugins.google_meet.meet_bot import _looks_like_human_speaker

    # Blank, "unknown", "you", and the bot's own name → not human (no barge-in)
    for s in ("", "   ", "Unknown", "unknown", "You", "you", "Hermes Agent", "hermes agent"):
        assert not _looks_like_human_speaker(s, "Hermes Agent"), f"{s!r} should NOT be human"
    # Real names → human (barge-in)
    for s in ("Alice", "Bob Lee", "@teknium"):
        assert _looks_like_human_speaker(s, "Hermes Agent"), f"{s!r} SHOULD be human"


def test_detect_admission_returns_false_on_error():
    from plugins.google_meet.meet_bot import _ADMISSION_PROBE_JS, _probe

    class _FakePage:
        def evaluate(self, _js): raise RuntimeError("boom")

    assert _probe(_FakePage(), _ADMISSION_PROBE_JS) is False


# ---------------------------------------------------------------------------
# Realtime join path: late Join button, muted mic, PCM pump fed after start-up (#80875)
# ---------------------------------------------------------------------------

class _Toggle:
    """Playwright-locator stand-in: visible when *present*, records clicks on *page*."""

    def __init__(self, page, present, label):
        self.page, self.present, self.label = page, present, label

    first = property(lambda self: self)
    def count(self): return 1 if self.present() else 0
    def is_visible(self): return self.present()
    def click(self, timeout=None): self.page.clicked.append(self.label)


def test_join_polls_for_late_button_and_admission_unmutes_mic(tmp_path):
    """Meet renders Join now / the mic toggle asynchronously; a one-shot click missed it (#80875).
    The mic check is driven through ``_drain_loop``'s admission branch — the production call site."""
    import time

    from plugins.google_meet.meet_bot import _ADMISSION_PROBE_JS, _BotConfig, _BotState, _drain_loop, _join

    class _Page:
        def __init__(self, ready_at, mic_muted, stop):
            self.ready_at, self.mic_muted, self.stop, self.clicked = ready_at, mic_muted, stop, []

        def locator(self, sel):
            muted = "Turn on microphone" in sel
            return _Toggle(self, lambda: self.mic_muted == muted, sel)

        def get_by_role(self, role, name=None, exact=False):
            return _Toggle(self, lambda: name == "Join now" and time.time() >= self.ready_at, name)

        def evaluate(self, js):  # admitted immediately; the caption drain ends the loop after one pass
            if js is _ADMISSION_PROBE_JS:
                return True
            self.stop["stop"] = True
            return []

        def is_closed(self): return False

    def admitted(mic_muted):
        stop = {"stop": False}
        page = _Page(ready_at=0, mic_muted=mic_muted, stop=stop)
        state = _BotState(tmp_path / str(mic_muted), "abc-defg-hij", "https://meet.google.com/abc-defg-hij")
        with patch("plugins.google_meet.meet_bot.time.sleep"):
            _drain_loop(page, _BotConfig(guest_name="Bot", duration_s=0, lobby_timeout=30), state,
                        {"session": None}, stop)
        assert state.in_call is True
        return page, state

    stop = {"stop": False}
    state = _BotState(tmp_path, "abc-defg-hij", "https://meet.google.com/abc-defg-hij")
    page = _Page(ready_at=time.time() + 0.6, mic_muted=True, stop=stop)
    _join(page, _BotConfig(guest_name="Bot"), state, timeout=5.0)
    assert page.clicked == ["Join now"]

    page, state = admitted(mic_muted=True)
    assert state.mic_state == "unmuted_clicked"
    assert any("Turn on microphone" in c for c in page.clicked)
    assert json.loads(state.status_path.read_text(encoding="utf-8"))["micState"] == "unmuted_clicked"
    # Control: an already-live mic is reported, never toggled off.
    live, state = admitted(mic_muted=False)
    assert state.mic_state == "unmuted" and live.clicked == []


def test_pcm_pump_receives_audio_appended_after_start(tmp_path, monkeypatch):
    """The pump used to read the empty speaker.pcm to EOF and exit before Realtime spoke (#80875)."""
    import subprocess
    import time

    from plugins.google_meet import meet_bot

    pcm, sink = tmp_path / "speaker.pcm", tmp_path / "device.bin"
    pcm.write_bytes(b"")
    real_popen = subprocess.Popen

    def cat_popen(cmd, **kw):  # `cat` stands in for paplay: same stdin / file-EOF semantics
        assert cmd[0] == "paplay" and cmd[-1] == "-"
        kw["stdout"] = open(sink, "wb")
        return real_popen(["cat"], **kw)

    monkeypatch.setattr(meet_bot.subprocess, "Popen", cat_popen)
    rt, stop = {}, {"stop": False}
    state = meet_bot._BotState(tmp_path, "abc-defg-hij", "https://meet.google.com/abc-defg-hij")
    meet_bot._start_pcm_pump(rt, {"platform": "linux", "write_target": "sink"}, pcm, state, stop)
    time.sleep(0.2)
    with open(pcm, "ab") as f:
        f.write(b"\x01\x02" * 2000)
    deadline = time.time() + 5
    while time.time() < deadline and sink.stat().st_size < 4000:
        time.sleep(0.05)
    assert rt["pcm_pump"].poll() is None
    assert sink.stat().st_size == 4000
    stop["stop"] = True
    meet_bot._teardown_realtime({**rt, "speaker_thread": None, "session": None, "bridge": None})
    assert not rt["pcm_tail_thread"].is_alive()
    assert state.mic_state is None and "micState" in state.status_path.read_text(encoding="utf-8")


def test_pcm_tail_loop_swallows_only_pipe_errors(tmp_path):
    """A closed pump pipe is expected and quiet; any other tail-thread bug must not be silenced."""
    from types import SimpleNamespace

    from plugins.google_meet.meet_bot import _pcm_tail_loop

    pcm = tmp_path / "speaker.pcm"
    pcm.write_bytes(b"\x00" * 16)

    def proc(exc):
        def write(_chunk): raise exc
        return SimpleNamespace(poll=lambda: None, stdin=SimpleNamespace(write=write, flush=lambda: None,
                                                                        close=lambda: None))

    _pcm_tail_loop(proc(BrokenPipeError()), pcm, {"stop": False})  # quiet
    with pytest.raises(RuntimeError):
        _pcm_tail_loop(proc(RuntimeError("bug")), pcm, {"stop": False})


# ---------------------------------------------------------------------------
# Realtime session counters + cancel_response (barge-in)
# ---------------------------------------------------------------------------

def test_realtime_session_cancel_response_when_disconnected():
    from plugins.google_meet.realtime.openai_client import RealtimeSession

    sess = RealtimeSession(api_key="sk-test", audio_sink_path=None)
    # No _ws yet — cancel should no-op and return False.
    assert sess.cancel_response() is False


# ---------------------------------------------------------------------------
# hermes meet install CLI
# ---------------------------------------------------------------------------




@pytest.mark.platforms("linux", "macos")
def test_cmd_install_uses_declared_dependencies_and_pm_chromium(monkeypatch, capsys):
    import pm

    calls = []
    monkeypatch.setattr(pm, "sync_venv", lambda extras, **kwargs: calls.append(("sync", extras, kwargs)))
    monkeypatch.setattr(pm, "ensure", lambda name, **kwargs: calls.append(("ensure", name, kwargs)))
    from plugins.google_meet import cli
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: pytest.fail("direct installer subprocess"))
    assert cli._cmd_install(realtime=False, assume_yes=True) == 0
    assert calls == [("sync", ["google-meet"], {"explicit": True}),
                     ("ensure", "chromium", {"explicit": True})]
    from pm.features import declared_extras

    assert set(calls[0][1]) <= set(declared_extras(Path(__file__).resolve().parents[2]))
    assert "hermes meet setup" in capsys.readouterr().out


@pytest.mark.platforms("linux", "macos")
def test_cmd_install_reports_dependency_failure(monkeypatch, capsys):
    import pm

    def fail(*args, **kwargs):
        raise RuntimeError("dependency resolution failed")

    monkeypatch.setattr(pm, "sync_venv", fail)
    from plugins.google_meet import cli
    assert cli._cmd_install(realtime=False, assume_yes=True) == 1
    assert "dependency resolution failed" in capsys.readouterr().out


def test_rtc_transcriber_thread_safe_whisper_init_and_queue_drain(tmp_path, monkeypatch):
    import sys
    import threading
    import time
    from types import SimpleNamespace
    from plugins.google_meet.meet_bot import _BotConfig, _BotState, _RtcTranscriber

    init_count = 0

    class _FakeWhisperModel:
        def __init__(self, *args, **kwargs):
            nonlocal init_count
            time.sleep(0.05)
            init_count += 1

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=_FakeWhisperModel))
    cfg = _BotConfig(out_dir=tmp_path, whisper_model="small")
    state = _BotState(tmp_path, "abc-defg-hij", "https://meet.google.com/abc-defg-hij")
    rtc = _RtcTranscriber(cfg, state, {"stop": False})

    threads = [threading.Thread(target=rtc._whisper) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert init_count == 1

    rtc._queue.extend([(2, 100), (3, 200), (2, 150)])
    assert rtc._drain_stream_queue(2) == 250
    assert list(rtc._queue) == [(3, 200)]


def test_ensure_ct2_int8_on_disk_converts_float16_model_bin(tmp_path):
    import struct
    np = pytest.importorskip("numpy")
    from plugins.google_meet.meet_bot import _ensure_ct2_int8_on_disk, _is_ct2_float16

    model_bin = tmp_path / "model.bin"
    arr = np.array([[1.0, -2.0], [0.5, 0.25]], dtype=np.float16)
    raw = arr.tobytes()

    with model_bin.open("wb") as f:
        def write_str(s: str):
            b = s.encode("utf-8")
            f.write(struct.pack("H", len(b) + 1))
            f.write(b)
            f.write(struct.pack("B", 0))
        f.write(struct.pack("I", 6))
        write_str("WhisperSpec")
        f.write(struct.pack("I", 3))
        f.write(struct.pack("I", 2))
        write_str("decoder/embeddings/weight")
        f.write(struct.pack("B", 2))
        f.write(struct.pack("I", 2))
        f.write(struct.pack("I", 2))
        f.write(struct.pack("B", 4))  # float16 -> float32 (non-quantizable embedding)
        f.write(struct.pack("I", len(raw)))
        f.write(raw)
        write_str("encoder/layers_0/self_attn/linear_0/weight")
        f.write(struct.pack("B", 2))
        f.write(struct.pack("I", 2))
        f.write(struct.pack("I", 2))
        f.write(struct.pack("B", 4))  # float16 -> int8 + scale
        f.write(struct.pack("I", len(raw)))
        f.write(raw)
        f.write(struct.pack("I", 1))
        write_str("decoder/projection/weight")
        write_str("decoder/embeddings/weight")

    assert _is_ct2_float16(model_bin) is True
    assert _ensure_ct2_int8_on_disk(str(tmp_path)) is True
    assert _is_ct2_float16(model_bin) is False
    assert _ensure_ct2_int8_on_disk(str(tmp_path)) is False

    from faster_whisper.vad import VadOptions, get_speech_timestamps
    from plugins.google_meet.meet_bot import _patch_silero_vad_batch_limit
    _patch_silero_vad_batch_limit(1500)
    silence = np.zeros(16000 * 2, dtype=np.float32)
    assert get_speech_timestamps(silence, VadOptions(threshold=0.5, min_speech_duration_ms=350)) == []


def test_rtc_transcriber_exact_cluster_cursor_mid_cluster_flushes(tmp_path, monkeypatch):
    from plugins.google_meet.meet_bot import _BotConfig, _BotState, _RtcTranscriber

    monkeypatch.setenv("HERMES_MEET_REALTIME_STANDUP", "1")
    cfg = _BotConfig(out_dir=tmp_path, whisper_model="small")
    state = _BotState(tmp_path, "abc-defg-hij", "https://meet.google.com/abc-defg-hij")
    rtc = _RtcTranscriber(cfg, state, {"stop": False})

    header = rtc._EBML_MAGIC + b"\x00" * 32
    cluster1_part1 = rtc._CLUSTER_ID + b"\xa3" * 100
    cluster1_part2 = b"\xa3" * 100
    cluster2_part1 = rtc._CLUSTER_ID + b"\xa3" * 100

    # Mock _decode_bytes so each byte after header yields 320 samples (20ms at 16kHz)
    def fake_decode_bytes(buf: bytes):
        assert buf.startswith(header + rtc._CLUSTER_ID), "Incremental decode must always start with header + Cluster ID"
        payload_len = len(buf) - len(header)
        return [0.1] * (payload_len * 320)

    monkeypatch.setattr(rtc, "_decode_bytes", fake_decode_bytes)
    full_decode_called = 0

    def fail_full_decode(_path):
        nonlocal full_decode_called
        full_decode_called += 1

    # Notice: we do NOT set "_decode" in rtc.__dict__, we patch on class or check full_decode_called
    monkeypatch.setattr(_RtcTranscriber, "_decode", staticmethod(fail_full_decode))

    merged = tmp_path / "stream_02.webm"
    # Flush 1: cluster 1 part 1
    merged.write_bytes(header + cluster1_part1)
    tail1, done1 = rtc._decode_incremental_tail(2, merged)
    assert tail1 is not None and done1 == 0
    assert len(tail1) == len(cluster1_part1) * 320

    # Flush 2: mid-cluster append (cluster1_part2 does NOT start with _CLUSTER_ID!)
    merged.write_bytes(header + cluster1_part1 + cluster1_part2)
    tail2, done2 = rtc._decode_incremental_tail(2, merged)
    assert tail2 is not None and done2 == len(tail1)
    assert len(tail2) == len(cluster1_part2) * 320

    # Flush 3: second cluster appended -> advances _last_cluster_off
    merged.write_bytes(header + cluster1_part1 + cluster1_part2 + cluster2_part1)
    tail3, done3 = rtc._decode_incremental_tail(2, merged)
    assert tail3 is not None and done3 == len(tail1) + len(tail2)
    assert len(tail3) == len(cluster2_part1) * 320
    assert rtc._last_cluster_off[2] == len(header) + len(cluster1_part1) + len(cluster1_part2)
    assert full_decode_called == 0


# ---------------------------------------------------------------------------
# Screenshare Brief Canvas & Voice Control
# ---------------------------------------------------------------------------

def test_is_safe_brief_url_accepts_pinned_origins():
    from plugins.google_meet.meet_bot import _is_safe_brief_url

    assert _is_safe_brief_url("https://meetings.uropenn.se/brief") is True
    assert _is_safe_brief_url("https://meetings.uropenn.se/studio/brief?mtg=abc") is True
    assert _is_safe_brief_url("http://100.117.81.127:8096/brief") is True
    assert _is_safe_brief_url("http://127.0.0.1:8096/studio/brief#section") is True

    # Disallowed domains / ports / paths
    assert _is_safe_brief_url("https://evil.example.com/brief") is False
    assert _is_safe_brief_url("http://100.117.81.127:8080/brief") is False
    assert _is_safe_brief_url("http://192.168.1.50:8096/brief") is False
    assert _is_safe_brief_url("https://meetings.uropenn.se/other") is False
    assert _is_safe_brief_url("") is False
    assert _is_safe_brief_url(None) is False  # type: ignore[arg-type]


def test_classify_voice_control_brief_presentation():
    from plugins.google_meet.meet_bot import _classify_voice_control

    assert _classify_voice_control("agenten, visa brief") == "present_brief"
    assert _classify_voice_control("botten, dela brief") == "present_brief"
    assert _classify_voice_control("roboten, presentera brief") == "present_brief"
    assert _classify_voice_control("notetaker, show brief") == "present_brief"
    assert _classify_voice_control("mr agent, dela agenda") == "present_brief"

    assert _classify_voice_control("agenten, sluta dela") == "stop_present_brief"
    assert _classify_voice_control("botten, sluta presentera") == "stop_present_brief"
    assert _classify_voice_control("roboten, stäng briefen") == "stop_present_brief"
    assert _classify_voice_control("notetaker, stop sharing") == "stop_present_brief"


def test_bot_state_brief_voice_commands(tmp_path):
    from plugins.google_meet.meet_bot import _BotState

    out = tmp_path / "session_brief"
    state = _BotState(out_dir=out, meeting_id="abc-defg-hij", url="https://meet.google.com/abc-defg-hij")

    assert state.brief_sharing is False
    assert state.brief_url is None

    state.record_caption("Alice", "agenten, visa brief")
    assert state.brief_present_requested is True
    assert state.brief_stop_requested is False

    state.record_caption("Bob", "agenten, sluta dela")
    assert state.brief_stop_requested is True
    assert state.brief_present_requested is False


def test_config_from_env_brief_settings(monkeypatch):
    from plugins.google_meet.meet_bot import _config_from_env

    monkeypatch.setenv("HERMES_MEET_BRIEF_URL", "https://meetings.uropenn.se/brief")
    monkeypatch.setenv("HERMES_MEET_BRIEF_AUTO_PRESENT", "1")
    monkeypatch.setenv("HERMES_MEET_BRIEF_MAX_SECONDS", "420")

    cfg = _config_from_env()
    assert cfg.brief_url == "https://meetings.uropenn.se/brief"
    assert cfg.brief_auto_present is True
    assert cfg.brief_max_seconds == 420.0


def test_rtc_merge_fragment_immediately_updates_last_audio_activity_above_dtx_floor(tmp_path):
    from plugins.google_meet.meet_bot import _BotConfig, _BotState, _RtcTranscriber, _PARTICIPANT_COUNT_JS

    cfg = _BotConfig(out_dir=tmp_path, whisper_model="small")
    state = _BotState(tmp_path, "abc-defg-hij", "https://meet.google.com/abc-defg-hij")
    rtc = _RtcTranscriber(cfg, state, {"stop": False})

    # DTX silence chunk (< 2000 bytes) does not mark speech activity
    dtx_frag = tmp_path / "frag_dtx.webm"
    dtx_frag.write_bytes(rtc._EBML_MAGIC + b"\x00" * 400)
    rtc._merge_fragment_immediately(1, dtx_frag)
    assert state.audio_fragments == 1
    assert state.last_audio_activity_at is None

    # Active speech WebRTC chunk (>= 2000 bytes) updates last_audio_activity_at immediately
    # even before the downstream GPU transcription worker finishes
    speech_frag = tmp_path / "frag_speech.webm"
    speech_frag.write_bytes(rtc._EBML_MAGIC + b"\x01" * 4096)
    rtc._merge_fragment_immediately(2, speech_frag)
    assert state.audio_fragments == 2
    assert state.last_audio_activity_at is not None
    assert "vid.videoWidth > 0" in _PARTICIPANT_COUNT_JS


def test_build_chromium_launch_kwargs_disables_audio_output_for_receive_only_rtc(monkeypatch, tmp_path):
    from plugins.google_meet.meet_bot import _BotConfig, _build_chromium_launch_kwargs

    monkeypatch.delenv("HERMES_MEET_ALLOW_AUDIO_OUTPUT", raising=False)
    monkeypatch.delenv("HERMES_MEET_NO_FAKE_DEVICE", raising=False)

    # Receive-only RTC recorder decouples from OS coreaudiod/mlock via --disable-audio-output
    cfg_rx = _BotConfig(out_dir=tmp_path, rtc_audio=True, headed=True)
    kw_rx = _build_chromium_launch_kwargs(cfg_rx, realtime_enabled=False)
    assert "--disable-audio-output" in kw_rx["args"]
    assert kw_rx["ignore_default_args"] == ["--mute-audio"]
    assert "--autoplay-policy=no-user-gesture-required" in kw_rx["args"]
    assert kw_rx["headless"] is False

    # Realtime mode keeps hardware/virtual audio output enabled
    kw_rt = _build_chromium_launch_kwargs(cfg_rx, realtime_enabled=True)
    assert "--disable-audio-output" not in kw_rt["args"]

    # Explicit escape hatch restores hardware audio output
    monkeypatch.setenv("HERMES_MEET_ALLOW_AUDIO_OUTPUT", "1")
    kw_opt = _build_chromium_launch_kwargs(cfg_rx, realtime_enabled=False)
    assert "--disable-audio-output" not in kw_opt["args"]

    # Non-RTC mode does not pass --disable-audio-output
    monkeypatch.delenv("HERMES_MEET_ALLOW_AUDIO_OUTPUT", raising=False)
    cfg_no_rtc = _BotConfig(out_dir=tmp_path, rtc_audio=False, headed=False)
    kw_no_rtc = _build_chromium_launch_kwargs(cfg_no_rtc, realtime_enabled=False)
    assert "--disable-audio-output" not in kw_no_rtc["args"]


def test_brief_rtc_hook_defines_update_frame_before_get_display_media():
    from plugins.google_meet.meet_bot import _RTC_HOOK_JS

    # RC-1 regression guard: __hermesUpdateBriefFrame must be defined before makeBriefDisplayTrack
    update_pos = _RTC_HOOK_JS.index("window.__hermesUpdateBriefFrame =")
    display_pos = _RTC_HOOK_JS.index("const makeBriefDisplayTrack =")
    assert update_pos < display_pos
    # RC-3 regression guards: CSP-safe createImageBitmap decode, periodic pump, prototype override
    assert "createImageBitmap(" in _RTC_HOOK_JS
    assert "setInterval(repaintBriefCanvas" in _RTC_HOOK_JS
    assert "MediaDevices.prototype" in _RTC_HOOK_JS


def test_start_brief_presentation_never_fakes_brief_sharing_without_gdm(tmp_path):
    import json
    from plugins.google_meet.meet_bot import _BotConfig, _BotState, _start_brief_presentation

    class _FakeMouse:
        def move(self, *_a, **_kw): pass
        def click(self, *_a, **_kw): pass

    class _FakeKeyboard:
        def press(self, *_a, **_kw): pass

    class _FakeLocator:
        @property
        def first(self):
            return self
        def click(self, **_kw):
            return None

    class _FakeBriefPage:
        def is_closed(self): return False
        def set_viewport_size(self, *_a, **_kw): pass
        def set_extra_http_headers(self, *_a, **_kw): pass
        def goto(self, *_a, **_kw): pass
        def evaluate(self, *_a, **_kw): return {"fits": True}
        def wait_for_timeout(self, *_a, **_kw): pass
        def screenshot(self, **_kw): return b"\xff\xd8\xff\xd9"

    class _FakeContext:
        def new_page(self): return _FakeBriefPage()

    class _FakePage:
        def __init__(self, btn_present: bool):
            self.context = _FakeContext()
            self.mouse = _FakeMouse()
            self.keyboard = _FakeKeyboard()
            self._btn_present = btn_present
            self.brought_to_front = 0
        def bring_to_front(self): self.brought_to_front += 1
        def wait_for_timeout(self, *_a, **_kw): pass
        def on(self, *_a, **_kw): pass
        def remove_listener(self, *_a, **_kw): pass
        def locator(self, *_a, **_kw): return _FakeLocator()
        def screenshot(self, **_kw): pass
        def evaluate(self, expr, *args):
            if "__hermesUpdateBriefFrame" in expr:
                return {"ok": True, "w": 1600, "h": 900, "centerRgba": [243, 247, 250, 255]}
            if "data-hermes-present-btn" in expr:
                return {"label": "Dela skärm", "x": 572, "y": 756, "w": 56, "h": 48} if self._btn_present else None
            if "__hermesBriefDebug" in expr:
                return {"getDisplayMediaCalls": 0, "framesDecoded": 1}
            return []

    cfg = _BotConfig(out_dir=tmp_path, brief_url="http://100.117.81.127:8096/brief?title=Test&screenshare=1")

    # Case A: Present button missing -> must return False and keep briefSharing False (RC-2)
    dir_a = tmp_path / "case_a"
    state_a = _BotState(out_dir=dir_a, meeting_id="abc-defg-hij", url="https://meet.google.com/abc-defg-hij")
    page_a = _FakePage(btn_present=False)
    assert _start_brief_presentation(page_a, {}, cfg, state_a) is False
    assert json.loads((dir_a / "status.json").read_text(encoding="utf-8"))["briefSharing"] is False
    assert page_a.brought_to_front >= 1

    # Case B: Present button clicked, but getDisplayMediaCalls remained 0 -> must still return False
    dir_b = tmp_path / "case_b"
    state_b = _BotState(out_dir=dir_b, meeting_id="abc-defg-hij", url="https://meet.google.com/abc-defg-hij")
    page_b = _FakePage(btn_present=True)
    assert _start_brief_presentation(page_b, {}, cfg, state_b) is False
    assert json.loads((dir_b / "status.json").read_text(encoding="utf-8"))["briefSharing"] is False


def test_brief_canvas_fit_and_human_presenting_probe_playwright():
    import base64
    from playwright.sync_api import sync_playwright
    from plugins.google_meet.meet_bot import (
        _BRIEF_FIT_TO_FRAME_JS,
        _HUMAN_PRESENTING_PROBE_JS,
        _RTC_HOOK_JS,
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            # 1. Verify _BRIEF_FIT_TO_FRAME_JS fits a tall 1300px brief cleanly inside 1600x900
            b_page = browser.new_page(viewport={"width": 1600, "height": 900})
            b_page.set_content(
                "<html><body style='background:#eef4ff;margin:0'>"
                "<div class='topbar' style='height:80px'>Toolbar</div>"
                "<main class='shell' style='width:1200px;height:1260px;background:#dfe8ff'>Tall Brief</main>"
                "</body></html>"
            )
            fit = b_page.evaluate(_BRIEF_FIT_TO_FRAME_JS)
            assert fit["fits"] is True, f"Expected tall brief to fit 1600x900, got {fit}"
            assert fit["visualBottom"] <= 900
            jpeg_b64 = base64.b64encode(b_page.screenshot(type="jpeg", quality=85)).decode("ascii")
            b_page.close()

            # 2. Verify _RTC_HOOK_JS decodes brief via createImageBitmap & feeds getDisplayMedia
            ctx = browser.new_context()
            ctx.add_init_script(_RTC_HOOK_JS)
            page = ctx.new_page()
            page.goto("about:blank")
            upd = page.evaluate("async b64 => await window.__hermesUpdateBriefFrame(b64)", jpeg_b64)
            assert upd["ok"] is True
            assert upd["centerRgba"][0] > 150 and upd["centerRgba"][1] > 150  # non-black pixel
            gdm = page.evaluate("""async () => {
              const s = await navigator.mediaDevices.getDisplayMedia({ video: true });
              const t = s.getVideoTracks()[0];
              return { readyState: t.readyState, settings: t.getSettings(), dbg: window.__hermesBriefDebug };
            }""")
            assert gdm["readyState"] == "live"
            assert gdm["settings"]["displaySurface"] == "monitor"
            assert gdm["dbg"]["getDisplayMediaCalls"] == 1
            assert gdm["dbg"]["attached"] is True

            # 3. Two-way polarity on _HUMAN_PRESENTING_PROBE_JS (RC-4 regression guard)
            for self_txt in (
                "Ur&Penn Mötesanteckningar (Du, presenterar) Avsluta presentationen",
                "Du presenterar för alla",
                "You're presenting to everyone Stop presenting",
            ):
                page.set_content(f"<html><body>{self_txt}</body></html>")
                assert page.evaluate(_HUMAN_PRESENTING_PROBE_JS) is False, self_txt

            for other_txt in (
                "Evelina Björninen presenterar",
                "Emir Saffar is presenting",
            ):
                page.set_content(f"<html><body>{other_txt}</body></html>")
                assert page.evaluate(_HUMAN_PRESENTING_PROBE_JS) is True, other_txt
            ctx.close()
        finally:
            browser.close()





