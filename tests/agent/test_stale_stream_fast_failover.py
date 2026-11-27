from agent.chat_completion_helpers import _StreamingCall, _should_skip_fallback_candidate


class _DummyAgent:
    def __init__(self):
        self.provider = "vertex"
        self.model = "google/gemini-4-argon"
        self.base_url = "https://aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/global/endpoints/openapi"
        self._last_routed_base_url = "https://europe-west2-aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/europe-west2/endpoints/openapi"
        self.api_mode = "chat_completions"
        self._interrupt_requested = False
        self._stream_options_unsupported = False
        self._stream_5xx_probe_ts = None
        self.logged_retries = []
        self.buffered_statuses = []

    def _warning_presentation_enabled(self) -> bool:
        return False

    def _is_provider_stream_parse_error(self, _err: Exception) -> bool:
        return False

    def _emit_stream_drop(self, **_kwargs) -> None:
        pass

    def _log_stream_retry(self, **kwargs) -> None:
        self.logged_retries.append(kwargs)

    def _buffer_diagnostic_status(self, msg: str) -> None:
        self.buffered_statuses.append(msg)

    def _maybe_disable_streaming(self, _err: Exception) -> None:
        pass


def test_stale_killed_stream_skips_inner_retries_and_marks_error():
    """When _kill_stale_stream already killed the current attempt after 600s,
    _handle_stream_error must NOT loop through max_retries more 600s inner attempts,
    and must mark the propagated exception with _stale_stream_killed = True so
    turn_recovery treats it as a timeout and flips Vertex regional -> global immediately.
    """
    agent = _DummyAgent()
    call = _StreamingCall(agent, {"model": agent.model}, None)
    call.stream_attempt_state["current"] = 1
    call._stale_counted_attempts.add(1)  # Attempt 1 was killed by _kill_stale_stream

    err = ConnectionError("[Errno 32] Broken pipe")
    should_retry = call._handle_stream_error(err, attempt=0, max_retries=2)

    assert should_retry is False
    assert call.result["error"] is err
    assert getattr(err, "_stale_stream_killed", False) is True


def test_should_skip_fallback_candidate_uses_last_routed_base_url():
    """When acquire_vertex_hybrid_region routed the failed request to europe-west2
    (_last_routed_base_url) while agent.base_url remained locations/global, the
    fallback entry for locations/global must NOT be skipped as 'same backend'.
    """
    agent = _DummyAgent()
    fb = {
        "provider": "vertex",
        "model": "google/gemini-4-argon",
        "base_url": "https://aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/global/endpoints/openapi",
    }
    unavailable = set()
    assert _should_skip_fallback_candidate(
        agent,
        fb,
        ("vertex", "google/gemini-4-argon", fb["base_url"]),
        "vertex",
        "google/gemini-4-argon",
        unavailable,
    ) is False


def test_vertex_regional_ttfb_stale_cap_is_150s_while_post_chunk_and_global_stay_600s():
    """Regional Vertex endpoints (eu / europe-west2 / asia-southeast1) cap Phase-1
    pre-first-chunk TTFB stale timeout at 75s so wedged regional TPU slots fail over to
    locations/global quickly, locations/global caps Phase-1 TTFB at 150s (>2x observed
    gemini-4-neon high-effort TTFB), unbounded desktop Phase-2 retains 600s, and bounded
    cron/subagent/run_budget_seconds Phase-2 uses 240s (never the 75s regional TTFB cap).
    """
    from typing import Any
    from types import SimpleNamespace

    call: Any = _StreamingCall.__new__(_StreamingCall)
    call.agent = SimpleNamespace(
        provider="vertex",
        model="google/gemini-4-neon",
        base_url="https://aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/global/endpoints/openapi",
        _last_routed_base_url="https://europe-west2-aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/europe-west2/endpoints/openapi",
        _stream_stale_timeout=None,
        _stream_inter_event_timeout=None,
    )
    call.api_kwargs = {"model": "google/gemini-4-neon", "messages": [{"role": "user", "content": "hi"}]}
    diag: dict[str, float | None] = {"first_chunk_at": None}
    call.clients = SimpleNamespace(diag=diag)
    call._resolve_stale_timeout()

    assert call._stream_stale_timeout == 600.0
    # Regional Phase-1 (europe-west2 and eu REP) uses 75s fast failover cap.
    assert call._effective_stale_timeout() == 75.0
    call.agent._last_routed_base_url = "https://aiplatform.eu.rep.googleapis.com/v1beta1/projects/uop-atlas/locations/eu/endpoints/openapi"
    assert call._effective_stale_timeout() == 75.0

    # Once first chunk arrives (Phase 2), unbounded interactive desktop gets full 600s floor.
    diag["first_chunk_at"] = 1001.0
    assert call._effective_stale_timeout() == 600.0

    # On locations/global, Phase 1 uses the 150s global TTFB cap (never 75s), while Phase 2 retains 600s.
    diag["first_chunk_at"] = None
    call.agent._last_routed_base_url = call.agent.base_url
    assert call._effective_stale_timeout() == 150.0
    diag["first_chunk_at"] = 1002.0
    assert call._effective_stale_timeout() == 600.0

    # On cron / subagent / run_budget_seconds-bounded Vertex runs, Phase-2 post_chunk caps at 240s
    # (decoupled from the 75s regional TTFB cap so deep thinking is not self-killed mid-stream).
    call.agent.platform = "cron"
    assert call._effective_stale_timeout() == 240.0
    call.agent.platform = "subagent"
    assert call._effective_stale_timeout() == 240.0
    call.agent.platform = "desktop"
    call.agent.run_budget_seconds = 600.0
    assert call._effective_stale_timeout() == 240.0


def test_vertex_warmer_prompt_split_and_credential_refresh(monkeypatch):
    """Vertex implicit prefix caching never reports cache_write_tokens; _prompt_split treats
    the completed prompt (>=2048 tokens) as cached up to its 256-token boundary, and _send
    refreshes the Vertex OAuth token and uses _last_routed_base_url.
    """
    from types import SimpleNamespace
    import agent.prompt_cache_lifecycle as pcl
    import agent.vertex_adapter as va

    ew2_url = "https://europe-west2-aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/europe-west2/endpoints/openapi"
    captured_opts = {}

    class _VertexClient:
        def with_options(self, **kw):
            captured_opts.update(kw)
            return self

        @property
        def chat(self):
            return SimpleNamespace(completions=SimpleNamespace(create=lambda **_k: SimpleNamespace(choices=[], usage=None)))

    agent = SimpleNamespace(
        provider="vertex",
        model="google/gemini-4-argon",
        api_mode="chat_completions",
        base_url="https://aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/global/endpoints/openapi",
        _last_routed_base_url=ew2_url,
        api_key="stale-token",
        client=_VertexClient(),
        session_id="sess_vertex_warm",
        _session_reset_epoch=0,
    )
    warmer = pcl.PromptCacheWarmer(agent)
    resp = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=75_000,
            completion_tokens=100,
            total_tokens=75_100,
            prompt_tokens_details=SimpleNamespace(cached_tokens=30_000),
        )
    )
    cached, uncached = warmer._prompt_split(resp)
    assert cached == 74_752
    assert uncached == 248
    decision = warmer._decide(cached, uncached)
    assert decision.action == "warm"

    monkeypatch.setattr(va, "get_vertex_config", lambda **_kw: ("fresh-oauth-token", ew2_url))
    run: object = SimpleNamespace(
        client=agent.client,
        api_kwargs={"model": "google/gemini-4-argon", "messages": [{"role": "user", "content": "hi"}]},
        model="google/gemini-4-argon",
        api_mode="chat_completions",
        billing={"provider": "vertex", "base_url": agent.base_url, "api_key": "stale-token"},
    )
    warmer._send(run)  # type: ignore[arg-type]
    assert captured_opts.get("api_key") == "fresh-oauth-token"
    assert captured_opts.get("base_url") == ew2_url


def test_subagent_and_cron_never_acquire_regional_slot_via_bg_review_branch():
    """A subagent_* or cron_* session with interactive=False must never match
    is_bg_review_for_ew2_parent even if its session_id was pre-pinned in _SESSION_STICKY_REGION.
    """
    from typing import Any
    import agent.vertex_adapter as va

    va_any: Any = va
    for sid in ("subagent_child_1", "cron_job_1"):
        with va_any._EUROPE_WEST2_STATE_LOCK:
            va_any._session_region_affinity[sid] = va.EUROPE_WEST2_VERTEX_REGION
        try:
            region, held = va.acquire_vertex_hybrid_region(
                "google/gemini-4-argon",
                configured_region="global",
                interactive=False,
                session_id=sid,
            )
            assert region == "global"
            assert held is False
        finally:
            va.reset_vertex_sticky_region(sid)

    # Timestamped subagent/cron session IDs (20261119_*) acquired with interactive=False
    # are pinned to global and never trip europe-west2 regional cooldown on a global 429/timeout.
    va.reset_vertex_hybrid_router_state()
    ts_subagent_sid = "20261119_193800_3c709f"
    region_ts, held_ts = va.acquire_vertex_hybrid_region(
        "google/gemini-4-argon",
        configured_region="global",
        interactive=False,
        session_id=ts_subagent_sid,
    )
    assert (region_ts, held_ts) == ("global", False)
    assert va.get_session_vertex_region_affinity(ts_subagent_sid) == "global"
    va.record_vertex_regional_429(cooldown_seconds=15.0, session_id=ts_subagent_sid)
    assert va.vertex_regional_cooldown_remaining() == 0.0


def test_vertex_inline_stream_read_timeout_honors_150s_cap_while_desktop_keeps_615s():
    """Inline Vertex streams (cron, subagent) and run_budget_seconds-bounded runs
    must align httpx read timeout to VERTEX_POST_CHUNK_STALE_CAP_SECONDS + 15s (255s)
    so deep-thinking pauses up to 240s never hit ReadTimeout at 120s while a pre-header
    socket wedge still unwinds in 255s instead of hanging 615s+ past 3 stale kills.
    Unbounded interactive desktop turns keep read=615s for Phase-2 deep thinking pauses.
    """
    from typing import Any
    from types import SimpleNamespace

    def _make_call(platform: str, run_budget_seconds: float | None = None) -> Any:
        call: Any = _StreamingCall.__new__(_StreamingCall)
        call.agent = SimpleNamespace(
            provider="vertex",
            model="google/gemini-4-neon",
            api_mode="chat_completions",
            platform=platform,
            run_budget_seconds=run_budget_seconds,
            base_url="https://aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/global/endpoints/openapi",
            _last_routed_base_url="https://aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/global/endpoints/openapi",
            _stream_stale_timeout=None,
            _stream_inter_event_timeout=None,
        )
        call.api_kwargs = {"model": "google/gemini-4-neon", "messages": [{"role": "user", "content": "hi"}]}
        call.clients = SimpleNamespace(diag={"first_chunk_at": None})
        call._resolve_stale_timeout()
        return call

    cron_call = _make_call("cron")
    assert cron_call._stream_timeouts()[1] == 255.0

    sub_call = _make_call("subagent")
    assert sub_call._stream_timeouts()[1] == 255.0
    sub_call.clients.diag["first_chunk_at"] = 1001.0
    assert sub_call._effective_stale_timeout() == 240.0

    budget_call = _make_call("desktop", run_budget_seconds=600.0)
    assert budget_call._stream_timeouts()[1] == 255.0

    desktop_call = _make_call("desktop")
    assert desktop_call._stream_timeouts()[1] == 615.0


def test_is_vertex_regional_url_polarity():
    """Verify regional vs global Vertex URL classification across REP, single-region,
    locations/global, and empty base URLs."""
    from agent.chat_completion_stream_monitor import _is_vertex_regional_url

    assert _is_vertex_regional_url(
        "https://aiplatform.eu.rep.googleapis.com/v1beta1/projects/uop-atlas/locations/eu/endpoints/openapi"
    ) is True
    assert _is_vertex_regional_url(
        "https://aiplatform.us.rep.googleapis.com/v1beta1/projects/uop-atlas/locations/us/endpoints/openapi"
    ) is True
    assert _is_vertex_regional_url(
        "https://europe-west2-aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/europe-west2/endpoints/openapi"
    ) is True
    assert _is_vertex_regional_url(
        "https://asia-southeast1-aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/asia-southeast1/endpoints/openapi"
    ) is True
    assert _is_vertex_regional_url(
        "https://aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/global/endpoints/openapi"
    ) is False
    assert _is_vertex_regional_url("") is False



