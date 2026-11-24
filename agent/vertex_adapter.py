"""Vertex AI (Google Cloud) auth + base-URL resolution for its OpenAI-compatible endpoint.

Requires ``google-auth`` (lazy-installed). Secrets: GOOGLE_APPLICATION_CREDENTIALS /
VERTEX_CREDENTIALS_PATH (SA JSON path; the latter wins), VERTEX_PROJECT_ID,
VERTEX_REGION. project_id/region may also live in config.yaml ``vertex:``;
env wins over config.
"""

from datetime import UTC, datetime
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from typing import Any, Optional

from agent.credential_source import VERTEX_OAUTH, tag_credential_source
from agent.secret_scope import get_secret as _get_secret, is_multiplex_active

try:
    import google.auth
    import google.auth.transport.requests
    from google.oauth2 import service_account
except ImportError:
    google = None  # type: ignore[assignment]


def _ensure_google_auth() -> bool:
    """Bind ``google.auth`` on first use, installing the [vertex] extra through PM if needed.

    The extra left [all] under the lazy-install policy (2026-05-12) so a plain ``hermes-agent``
    install still reaches Vertex after selecting a Gemini model. This runs at the first
    credential request, never at import: an import-time sync would rebuild the dependency
    environment of whatever process happens to import this module.
    """
    global google, service_account
    if google is not None:
        return True
    try:
        from pm import ensure_import
        ensure_import("vertex")
        import google.auth as _auth
        import google.auth.transport.requests
        from google.oauth2 import service_account as _service_account
    except Exception as exc:
        logger.warning("google-auth package not installed (%s). Cannot use Vertex AI.", exc)
        return False
    import google as _google
    google, service_account = _google, _service_account
    return True

logger = logging.getLogger(__name__)

DEFAULT_REGION = "global"
GLOBAL_VERTEX_REGION = "global"
EU_MULTI_REGION_VERTEX_REGION = "eu"
US_MULTI_REGION_VERTEX_REGION = "us"
MULTI_REGION_VERTEX_REGIONS = (
    EU_MULTI_REGION_VERTEX_REGION,
    US_MULTI_REGION_VERTEX_REGION,
)
EUROPE_WEST2_VERTEX_REGION = "europe-west2"
ASIA_SOUTHEAST1_VERTEX_REGION = "asia-southeast1"
US_CENTRAL1_VERTEX_REGION = "us-central1"
REGIONAL_VERTEX_REGIONS = (
    EUROPE_WEST2_VERTEX_REGION,
    ASIA_SOUTHEAST1_VERTEX_REGION,
)
EUROPE_WEST2_ARGON_MAX_CONCURRENCY = 5
REGIONAL_COLD_PREFILL_MAX_CONCURRENCY = 2
REGIONAL_COLD_PREFILL_MIN_TOKENS = 25_000
EUROPE_WEST2_MAX_ACTIVE_SESSIONS = 8
ASIA_SOUTHEAST1_MAX_ACTIVE_SESSIONS = 8
US_CENTRAL1_MAX_ACTIVE_SESSIONS = 8
EUROPE_WEST2_ACTIVE_SESSION_TTL_SECONDS = 180.0
EUROPE_WEST2_COOLDOWN_SECONDS = 15.0
REGIONAL_404_QUARANTINE_SECONDS = 3600.0
EUROPE_WEST2_COOLDOWN_TAIL_WAIT_SECONDS = 15.0
EUROPE_WEST2_COOLDOWN_MAX_TOTAL_WAIT_SECONDS = 45.0
EUROPE_WEST2_DEEP_SEMAPHORE_WAIT_SECONDS = 90.0
REGIONAL_429_STRIKE_WINDOW_SECONDS = 45.0
REGIONAL_429_MAX_STRIKES = 2
# carried-fix-anchor: EUROPE_WEST2_STICKY_WAIT_SECONDS = 3.0 (raised to 15.0s; deep 30.0s; initial 8.0s; min_tok 35k)
EUROPE_WEST2_INITIAL_WAIT_SECONDS = 8.0
EUROPE_WEST2_STICKY_WAIT_SECONDS = 15.0
EUROPE_WEST2_DEEP_EPOCH_WAIT_SECONDS = 30.0
GLOBAL_STICKY_EPOCH_MIN_TOKENS = 55_000

_prev_vertex_mod = sys.modules.get(__name__)
_EUROPE_WEST2_ARGON_SEMAPHORE = getattr(
    _prev_vertex_mod, "_EUROPE_WEST2_ARGON_SEMAPHORE", None
) or threading.BoundedSemaphore(EUROPE_WEST2_ARGON_MAX_CONCURRENCY)
_ASIA_SOUTHEAST1_ARGON_SEMAPHORE = getattr(
    _prev_vertex_mod, "_ASIA_SOUTHEAST1_ARGON_SEMAPHORE", None
) or threading.BoundedSemaphore(EUROPE_WEST2_ARGON_MAX_CONCURRENCY)
_US_CENTRAL1_ARGON_SEMAPHORE = getattr(
    _prev_vertex_mod, "_US_CENTRAL1_ARGON_SEMAPHORE", None
) or threading.BoundedSemaphore(EUROPE_WEST2_ARGON_MAX_CONCURRENCY)
_EUROPE_WEST2_STATE_LOCK = getattr(
    _prev_vertex_mod, "_EUROPE_WEST2_STATE_LOCK", None
) or threading.Lock()
_europe_west2_cooldown_until: float = float(
    getattr(_prev_vertex_mod, "_europe_west2_cooldown_until", 0.0) or 0.0
)
_asia_southeast1_cooldown_until: float = float(
    getattr(_prev_vertex_mod, "_asia_southeast1_cooldown_until", 0.0) or 0.0
)
_us_central1_cooldown_until: float = float(
    getattr(_prev_vertex_mod, "_us_central1_cooldown_until", 0.0) or 0.0
)
_eu_multi_region_cooldown_until: float = float(
    getattr(_prev_vertex_mod, "_eu_multi_region_cooldown_until", 0.0) or 0.0
)
_us_multi_region_cooldown_until: float = float(
    getattr(_prev_vertex_mod, "_us_multi_region_cooldown_until", 0.0) or 0.0
)
_prev_cold_in_flight = getattr(_prev_vertex_mod, "_cold_prefill_in_flight", None)
_cold_prefill_in_flight: dict[str, int] = (
    _prev_cold_in_flight if isinstance(_prev_cold_in_flight, dict) else {}
)
_prev_affinity = getattr(_prev_vertex_mod, "_session_region_affinity", None)
_session_region_affinity: dict[str, str] = (
    _prev_affinity if isinstance(_prev_affinity, dict) else {}
)
_prev_last_seen = getattr(_prev_vertex_mod, "_session_region_last_seen", None)
_session_region_last_seen: dict[str, float] = (
    _prev_last_seen if isinstance(_prev_last_seen, dict) else {}
)
for _stale_sid, _stale_reg in list(_session_region_affinity.items()):
    if _stale_reg not in (GLOBAL_VERTEX_REGION, *REGIONAL_VERTEX_REGIONS, *MULTI_REGION_VERTEX_REGIONS):
        _session_region_affinity.pop(_stale_sid, None)
        _session_region_last_seen.pop(_stale_sid, None)
_prev_strikes = getattr(_prev_vertex_mod, "_session_regional_429_strikes", None)
_session_regional_429_strikes: dict[str, tuple[int, float]] = (
    _prev_strikes if isinstance(_prev_strikes, dict) else {}
)
_prev_non_interactive = getattr(_prev_vertex_mod, "_non_interactive_session_ids", None)
_non_interactive_session_ids: set[str] = (
    _prev_non_interactive if isinstance(_prev_non_interactive, set) else set()
)
_SESSION_AFFINITY_CAP = 512
_VERTEX_BASE_URL_RE = re.compile(
    r"^https://(?:(?:[a-z0-9-]+-)?aiplatform\.googleapis\.com|aiplatform\.(?:eu|us)\.rep\.googleapis\.com)"
    r"/v1(?:beta1)?/projects/([^/]+)/locations/([^/]+)/endpoints/openapi/?$"
)
_CLOUD_PLATFORM_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

_creds_cache: dict = {}


def parse_vertex_base_url(base_url: Optional[str]) -> Optional[tuple[str, str]]:
    """Extract ``(project_id, region)`` from a Vertex OpenAI-compatible base URL, or None."""
    m = _VERTEX_BASE_URL_RE.match(str(base_url or "").strip())
    return (m.group(1), m.group(2)) if m else None


def is_vertex_argon_model(model: Optional[str]) -> bool:
    """Return True strictly for gemini-4-argon (never gemini-3.8-flash or gemini-3*)."""
    cleaned = str(model or "").strip().lower()
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    return cleaned == "gemini-4-argon" or cleaned.startswith("gemini-4-argon-")


def is_vertex_multi_region_supported_model(model: Optional[str]) -> bool:
    """Models that 404 on single regions (europe-west2) but support multi-region ``eu``/``us`` REP endpoints."""
    cleaned = str(model or "").strip().lower()
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    return (
        cleaned.startswith("gemini-4-neon")
        or cleaned.startswith("gemini-4-hydrogen")
        or cleaned.startswith("gemini-4-carbon")
    )


def is_vertex_global_only_model(model: Optional[str]) -> bool:
    """Models that return HTTP 404 on regional endpoints (e.g. europe-west2) and must use global."""
    cleaned = str(model or "").strip().lower()
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    return (
        cleaned.startswith("gemini-3.8-flash")
        or cleaned.startswith("gemini-3-")
        or cleaned.startswith("gemini-3.")
        or cleaned.startswith("gemini-4-neon")
        or cleaned.startswith("gemini-4-hydrogen")
        or cleaned.startswith("gemini-4-carbon")
    )


def _shared_router_state_path() -> str:
    env_p = os.environ.get("HERMES_VERTEX_SHARED_STATE_PATH")
    if env_p:
        return env_p
    return os.path.expanduser("~/.hermes/cache/scratch/vertex-regional-router-state.json")


_peer_cd_cache_ts: float = 0.0
_peer_cd_cache_data: dict[str, dict[str, float]] = {}


def _write_shared_cooldown(region: str, cooldown_seconds: float) -> None:
    global _peer_cd_cache_ts
    if cooldown_seconds <= 0.0:
        return
    path = _shared_router_state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data: dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
                if isinstance(loaded, dict):
                    data = loaded
        cd_map = data.get("cooldowns")
        if not isinstance(cd_map, dict):
            cd_map = {}
        cd_map[region] = {
            "until_wall": time.time() + float(cooldown_seconds),
            "pid": os.getpid(),
        }
        data["cooldowns"] = cd_map
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
        _peer_cd_cache_ts = 0.0
    except Exception:
        pass


def _clear_shared_cooldowns() -> None:
    global _peer_cd_cache_ts, _peer_cd_cache_data
    _peer_cd_cache_ts = 0.0
    _peer_cd_cache_data = {}
    path = _shared_router_state_path()
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _peer_cooldown_remaining(region: str) -> float:
    global _peer_cd_cache_ts, _peer_cd_cache_data
    now_mono = time.monotonic()
    if (now_mono - _peer_cd_cache_ts) > 0.25:
        _peer_cd_cache_ts = now_mono
        path = _shared_router_state_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                cd_map = loaded.get("cooldowns") if isinstance(loaded, dict) else None
                _peer_cd_cache_data = cd_map if isinstance(cd_map, dict) else {}
            else:
                _peer_cd_cache_data = {}
        except Exception:
            _peer_cd_cache_data = {}
    entry = _peer_cd_cache_data.get(region)
    if not isinstance(entry, dict):
        return 0.0
    if int(entry.get("pid", 0) or 0) == os.getpid():
        return 0.0
    until_wall = float(entry.get("until_wall", 0.0) or 0.0)
    return max(0.0, until_wall - time.time())


def record_vertex_regional_429(
    cooldown_seconds: float = EUROPE_WEST2_COOLDOWN_SECONDS,
    *,
    session_id: Optional[str] = None,
    last_prompt_tokens: int = 0,
    force_demote_to_global: bool = False,
) -> float:
    """Trip the regional 429 cooldown so subsequent calls route to locations/global."""
    global _europe_west2_cooldown_until, _asia_southeast1_cooldown_until, _us_central1_cooldown_until
    global _eu_multi_region_cooldown_until, _us_multi_region_cooldown_until
    now = time.monotonic()
    raw_sid = str(session_id or "").strip()
    canon_sid = _canonical_affinity_session_id(raw_sid) if raw_sid else None
    demote_repeat_striker = bool(force_demote_to_global)
    tripped_region: Optional[str] = None
    with _EUROPE_WEST2_STATE_LOCK:
        pinned_before = _session_region_affinity.get(canon_sid) if canon_sid else None
        is_ase1_session = pinned_before == ASIA_SOUTHEAST1_VERTEX_REGION
        is_usc1_session = pinned_before == US_CENTRAL1_VERTEX_REGION
        is_eu_rep_session = pinned_before == EU_MULTI_REGION_VERTEX_REGION
        is_us_rep_session = pinned_before == US_MULTI_REGION_VERTEX_REGION
        is_non_ew2_session = bool(
            pinned_before != EUROPE_WEST2_VERTEX_REGION
            and (
                raw_sid.startswith(("cron_", "subagent_"))
                or (bool(canon_sid) and canon_sid in _non_interactive_session_ids)
                or pinned_before in (
                    GLOBAL_VERTEX_REGION,
                    ASIA_SOUTHEAST1_VERTEX_REGION,
                    US_CENTRAL1_VERTEX_REGION,
                    EU_MULTI_REGION_VERTEX_REGION,
                    US_MULTI_REGION_VERTEX_REGION,
                )
            )
        )
        effective_cooldown = (
            max(float(cooldown_seconds), REGIONAL_404_QUARANTINE_SECONDS)
            if force_demote_to_global
            else max(0.0, float(cooldown_seconds))
        )
        target = now + effective_cooldown
        if is_ase1_session:
            _asia_southeast1_cooldown_until = max(_asia_southeast1_cooldown_until, target)
            tripped_region = ASIA_SOUTHEAST1_VERTEX_REGION
        elif is_usc1_session:
            _us_central1_cooldown_until = max(_us_central1_cooldown_until, target)
            tripped_region = US_CENTRAL1_VERTEX_REGION
        elif is_eu_rep_session:
            _eu_multi_region_cooldown_until = max(_eu_multi_region_cooldown_until, target)
            tripped_region = EU_MULTI_REGION_VERTEX_REGION
        elif is_us_rep_session:
            _us_multi_region_cooldown_until = max(_us_multi_region_cooldown_until, target)
            tripped_region = US_MULTI_REGION_VERTEX_REGION
        elif not is_non_ew2_session:
            _europe_west2_cooldown_until = max(_europe_west2_cooldown_until, target)
            tripped_region = EUROPE_WEST2_VERTEX_REGION
        if force_demote_to_global and tripped_region:
            for _sid_k, _reg_v in list(_session_region_affinity.items()):
                if _reg_v == tripped_region:
                    _session_region_affinity[_sid_k] = GLOBAL_VERTEX_REGION
                    _session_regional_429_strikes.pop(_sid_k, None)
        if canon_sid and pinned_before in REGIONAL_VERTEX_REGIONS:
            prev_count, first_ts = _session_regional_429_strikes.get(canon_sid, (0, now))
            if (now - first_ts) > REGIONAL_429_STRIKE_WINDOW_SECONDS:
                strikes = 1
                first_ts = now
            else:
                strikes = prev_count + 1
            _session_regional_429_strikes[canon_sid] = (strikes, first_ts)
            if strikes >= REGIONAL_429_MAX_STRIKES:
                demote_repeat_striker = True
        until = _europe_west2_cooldown_until
    if tripped_region:
        _write_shared_cooldown(tripped_region, effective_cooldown)
    if (
        raw_sid
        and ":bg-review:" not in raw_sid
        and ":bg_review" not in raw_sid
        and (
            int(last_prompt_tokens or 0) < GLOBAL_STICKY_EPOCH_MIN_TOKENS
            or demote_repeat_striker
        )
    ):
        _record_session_vertex_region_affinity(raw_sid, GLOBAL_VERTEX_REGION)
    return until


def vertex_regional_cooldown_remaining() -> float:
    """Seconds remaining on the europe-west2 429 cooldown (0.0 when ready)."""
    with _EUROPE_WEST2_STATE_LOCK:
        local_rem = max(0.0, _europe_west2_cooldown_until - time.monotonic())
    return max(local_rem, _peer_cooldown_remaining(EUROPE_WEST2_VERTEX_REGION))


def vertex_asia_southeast1_cooldown_remaining() -> float:
    """Seconds remaining on the asia-southeast1 429 cooldown (0.0 when ready)."""
    with _EUROPE_WEST2_STATE_LOCK:
        local_rem = max(0.0, _asia_southeast1_cooldown_until - time.monotonic())
    return max(local_rem, _peer_cooldown_remaining(ASIA_SOUTHEAST1_VERTEX_REGION))


def vertex_us_central1_cooldown_remaining() -> float:
    """Seconds remaining on the us-central1 429 cooldown (0.0 when ready)."""
    with _EUROPE_WEST2_STATE_LOCK:
        local_rem = max(0.0, _us_central1_cooldown_until - time.monotonic())
    return max(local_rem, _peer_cooldown_remaining(US_CENTRAL1_VERTEX_REGION))


def vertex_eu_multi_region_cooldown_remaining() -> float:
    """Seconds remaining on the eu multi-region 429/404 cooldown (0.0 when ready)."""
    with _EUROPE_WEST2_STATE_LOCK:
        local_rem = max(0.0, _eu_multi_region_cooldown_until - time.monotonic())
    return max(local_rem, _peer_cooldown_remaining(EU_MULTI_REGION_VERTEX_REGION))


def vertex_us_multi_region_cooldown_remaining() -> float:
    """Seconds remaining on the us multi-region 429/404 cooldown (0.0 when ready)."""
    with _EUROPE_WEST2_STATE_LOCK:
        local_rem = max(0.0, _us_multi_region_cooldown_until - time.monotonic())
    return max(local_rem, _peer_cooldown_remaining(US_MULTI_REGION_VERTEX_REGION))


def reset_vertex_hybrid_router_state() -> None:
    """Reset cooldown, session affinity, and semaphore state (used by unit tests)."""
    global _europe_west2_cooldown_until, _asia_southeast1_cooldown_until, _us_central1_cooldown_until
    global _eu_multi_region_cooldown_until, _us_multi_region_cooldown_until
    global _EUROPE_WEST2_ARGON_SEMAPHORE, _ASIA_SOUTHEAST1_ARGON_SEMAPHORE, _US_CENTRAL1_ARGON_SEMAPHORE
    with _EUROPE_WEST2_STATE_LOCK:
        _europe_west2_cooldown_until = 0.0
        _asia_southeast1_cooldown_until = 0.0
        _us_central1_cooldown_until = 0.0
        _eu_multi_region_cooldown_until = 0.0
        _us_multi_region_cooldown_until = 0.0
        _EUROPE_WEST2_ARGON_SEMAPHORE = threading.BoundedSemaphore(EUROPE_WEST2_ARGON_MAX_CONCURRENCY)
        _ASIA_SOUTHEAST1_ARGON_SEMAPHORE = threading.BoundedSemaphore(EUROPE_WEST2_ARGON_MAX_CONCURRENCY)
        _US_CENTRAL1_ARGON_SEMAPHORE = threading.BoundedSemaphore(EUROPE_WEST2_ARGON_MAX_CONCURRENCY)
        _cold_prefill_in_flight.clear()
        _session_region_affinity.clear()
        _session_region_last_seen.clear()
        _session_regional_429_strikes.clear()
        _non_interactive_session_ids.clear()
    _clear_shared_cooldowns()


def _canonical_affinity_session_id(session_id: Optional[str]) -> Optional[str]:
    """Strip ``review-`` prefix or ``:bg-review:<seq>`` / ``:bg_review`` suffix so background_review inherits parent session affinity."""
    if not session_id:
        return None
    raw = str(session_id).strip()
    if not raw:
        return None
    if raw.startswith("review-") and len(raw) > 7:
        raw = raw[7:].strip()
    base = re.split(r":bg[-_]review(?::|$)", raw, maxsplit=1)[0].strip()
    return base or raw


def reset_vertex_sticky_region(session_id: Optional[str], *, only_if_global: bool = False) -> None:
    """Clear sticky Vertex region for *session_id* (or only if pinned to global on compaction)."""
    sid = _canonical_affinity_session_id(session_id)
    if not sid:
        return
    with _EUROPE_WEST2_STATE_LOCK:
        if only_if_global and _session_region_affinity.get(sid) != GLOBAL_VERTEX_REGION:
            return
        _session_region_affinity.pop(sid, None)
        _session_region_last_seen.pop(sid, None)


clear_session_vertex_region = reset_vertex_sticky_region
clear_session_vertex_region_affinity = reset_vertex_sticky_region


def migrate_vertex_sticky_region(old_session_id: Optional[str], new_session_id: Optional[str]) -> None:
    """Carry regional sticky affinity (europe-west2 / asia-southeast1) across compaction child rotation."""
    old_sid = _canonical_affinity_session_id(old_session_id)
    new_sid = _canonical_affinity_session_id(new_session_id)
    if not old_sid:
        return
    now_mono = time.monotonic()
    with _EUROPE_WEST2_STATE_LOCK:
        prev = _session_region_affinity.pop(old_sid, None)
        _session_region_last_seen.pop(old_sid, None)
        if new_sid and prev in (*REGIONAL_VERTEX_REGIONS, *MULTI_REGION_VERTEX_REGIONS):
            _session_region_affinity[new_sid] = prev
            _session_region_last_seen[new_sid] = now_mono


def get_session_vertex_region_affinity(session_id: Optional[str]) -> Optional[str]:
    """Return the sticky Vertex region recorded for *session_id*, if any."""
    sid = _canonical_affinity_session_id(session_id)
    if not sid:
        return None
    with _EUROPE_WEST2_STATE_LOCK:
        reg = _session_region_affinity.get(sid)
        if reg is not None and reg not in (
            GLOBAL_VERTEX_REGION,
            *REGIONAL_VERTEX_REGIONS,
            *MULTI_REGION_VERTEX_REGIONS,
        ):
            _session_region_affinity.pop(sid, None)
            _session_region_last_seen.pop(sid, None)
            return None
        return reg


def get_active_europe_west2_session_count(now_monotonic: Optional[float] = None) -> int:
    """Return number of sessions pinned to europe-west2 active within the TTL window."""
    now_val = time.monotonic() if now_monotonic is None else float(now_monotonic)
    with _EUROPE_WEST2_STATE_LOCK:
        return sum(
            1
            for s, r in _session_region_affinity.items()
            if r == EUROPE_WEST2_VERTEX_REGION
            and (now_val - _session_region_last_seen.get(s, now_val)) <= EUROPE_WEST2_ACTIVE_SESSION_TTL_SECONDS
        )


def get_active_asia_southeast1_session_count(now_monotonic: Optional[float] = None) -> int:
    """Return number of sessions pinned to asia-southeast1 active within the TTL window."""
    now_val = time.monotonic() if now_monotonic is None else float(now_monotonic)
    with _EUROPE_WEST2_STATE_LOCK:
        return sum(
            1
            for s, r in _session_region_affinity.items()
            if r == ASIA_SOUTHEAST1_VERTEX_REGION
            and (now_val - _session_region_last_seen.get(s, now_val)) <= EUROPE_WEST2_ACTIVE_SESSION_TTL_SECONDS
        )


def get_active_us_central1_session_count(now_monotonic: Optional[float] = None) -> int:
    """Return number of sessions pinned to us-central1 active within the TTL window."""
    now_val = time.monotonic() if now_monotonic is None else float(now_monotonic)
    with _EUROPE_WEST2_STATE_LOCK:
        return sum(
            1
            for s, r in _session_region_affinity.items()
            if r == US_CENTRAL1_VERTEX_REGION
            and (now_val - _session_region_last_seen.get(s, now_val)) <= EUROPE_WEST2_ACTIVE_SESSION_TTL_SECONDS
        )


def _region_cooldown_remaining(region: str) -> float:
    if region == EUROPE_WEST2_VERTEX_REGION:
        return vertex_regional_cooldown_remaining()
    if region == ASIA_SOUTHEAST1_VERTEX_REGION:
        return vertex_asia_southeast1_cooldown_remaining()
    if region == US_CENTRAL1_VERTEX_REGION:
        return vertex_us_central1_cooldown_remaining()
    if region == EU_MULTI_REGION_VERTEX_REGION:
        return vertex_eu_multi_region_cooldown_remaining()
    if region == US_MULTI_REGION_VERTEX_REGION:
        return vertex_us_multi_region_cooldown_remaining()
    return 0.0


def _region_active_session_count(region: str) -> int:
    if region == EUROPE_WEST2_VERTEX_REGION:
        return get_active_europe_west2_session_count()
    if region == ASIA_SOUTHEAST1_VERTEX_REGION:
        return get_active_asia_southeast1_session_count()
    if region == US_CENTRAL1_VERTEX_REGION:
        return get_active_us_central1_session_count()
    return 0


def _region_max_active_sessions(region: str) -> int:
    if region == ASIA_SOUTHEAST1_VERTEX_REGION:
        return ASIA_SOUTHEAST1_MAX_ACTIVE_SESSIONS
    if region == US_CENTRAL1_VERTEX_REGION:
        return US_CENTRAL1_MAX_ACTIVE_SESSIONS
    return EUROPE_WEST2_MAX_ACTIVE_SESSIONS


def _region_semaphore(region: str) -> threading.BoundedSemaphore:
    if region == ASIA_SOUTHEAST1_VERTEX_REGION:
        return _ASIA_SOUTHEAST1_ARGON_SEMAPHORE
    if region == US_CENTRAL1_VERTEX_REGION:
        return _US_CENTRAL1_ARGON_SEMAPHORE
    return _EUROPE_WEST2_ARGON_SEMAPHORE


def _record_session_vertex_region_affinity(session_id: Optional[str], region: str) -> None:
    sid = _canonical_affinity_session_id(session_id)
    if not sid:
        return
    now_mono = time.monotonic()
    with _EUROPE_WEST2_STATE_LOCK:
        _session_region_affinity.pop(sid, None)
        _session_region_affinity[sid] = region
        _session_region_last_seen[sid] = now_mono
        if region == GLOBAL_VERTEX_REGION:
            _session_regional_429_strikes.pop(sid, None)
        elif region in REGIONAL_VERTEX_REGIONS and sid in _session_regional_429_strikes:
            _, _first_strike_ts = _session_regional_429_strikes[sid]
            if (now_mono - _first_strike_ts) > 25.0:
                _session_regional_429_strikes.pop(sid, None)
        while len(_session_region_affinity) > _SESSION_AFFINITY_CAP:
            oldest = next(iter(_session_region_affinity))
            del _session_region_affinity[oldest]
            _session_region_last_seen.pop(oldest, None)
            _session_regional_429_strikes.pop(oldest, None)


def acquire_vertex_hybrid_region(
    model: Optional[str],
    *,
    configured_region: Optional[str] = None,
    interactive: bool = True,
    session_id: Optional[str] = None,
    sticky_wait_seconds: Optional[float] = None,
    last_prompt_tokens: int = 0,
    uncached_prompt_tokens: int = 0,
) -> tuple[str, bool]:
    """Select the Vertex region for a single request and optionally acquire a regional slot.

    Returns ``(region, slot_acquired)``:
    - ``gemini-3.8-flash`` / ``gemini-3*`` -> always ``("global", False)`` (HTTP 404 on europe-west2).
    - ``gemini-4-argon`` when ``interactive=True`` (or ``:bg-review:`` for a regional parent)
      and configured region is ``global`` or one of ``REGIONAL_VERTEX_REGIONS`` -> tries checkout of
      a regional semaphore (max 5 per region across europe-west2, asia-southeast1, us-central1 = 24
      sticky active sessions total) when not in a 429 cooldown window. Cold prefills (>=25k uncached
      tokens on unpinned/rotated sessions) are capped at 2 concurrent per region before spilling to
      the next regional macro-cluster.
    - All other models/regions -> ``(configured_region or "global", False)``.
    """
    reg = (configured_region or DEFAULT_REGION).strip() or DEFAULT_REGION
    if is_vertex_multi_region_supported_model(model):
        target_mr: Optional[str] = None
        if reg in MULTI_REGION_VERTEX_REGIONS:
            target_mr = reg
        else:
            mode = _resolve_multi_region_policy()
            if mode in MULTI_REGION_VERTEX_REGIONS:
                target_mr = mode
            elif mode == "canary_50":
                canon = _canonical_affinity_session_id(str(session_id or ""))
                pinned_mr = get_session_vertex_region_affinity(session_id)
                if pinned_mr in (GLOBAL_VERTEX_REGION, *MULTI_REGION_VERTEX_REGIONS):
                    target_mr = pinned_mr
                elif canon:
                    bucket = hashlib.sha256(canon.encode("utf-8")).digest()[0] % 2
                    target_mr = EU_MULTI_REGION_VERTEX_REGION if bucket == 0 else GLOBAL_VERTEX_REGION
                    _record_session_vertex_region_affinity(session_id, target_mr)
        if target_mr in MULTI_REGION_VERTEX_REGIONS:
            if _region_cooldown_remaining(target_mr) > 0.0:
                if session_id:
                    _record_session_vertex_region_affinity(session_id, GLOBAL_VERTEX_REGION)
                return (GLOBAL_VERTEX_REGION, False)
            if session_id:
                _record_session_vertex_region_affinity(session_id, target_mr)
            return (target_mr, False)
        return (GLOBAL_VERTEX_REGION, False)
    if is_vertex_global_only_model(model):
        return (GLOBAL_VERTEX_REGION, False)
    prompt_tok = int(last_prompt_tokens or 0)
    uncached_tok = int(uncached_prompt_tokens or 0)
    raw_sid = str(session_id or "")
    pinned_region = get_session_vertex_region_affinity(session_id)
    if prompt_tok == -1:
        if pinned_region == GLOBAL_VERTEX_REGION or (
            pinned_region in REGIONAL_VERTEX_REGIONS
            and _region_active_session_count(pinned_region) > _region_max_active_sessions(pinned_region)
        ):
            reset_vertex_sticky_region(session_id)
            pinned_region = None
        prompt_tok = 0
    is_cold_prefill = bool(
        uncached_tok >= REGIONAL_COLD_PREFILL_MIN_TOKENS
        and (pinned_region is None or prompt_tok <= 0)
    )
    canon_sid = _canonical_affinity_session_id(raw_sid) if raw_sid else None
    is_bg_review_for_ew2_parent = (
        pinned_region in REGIONAL_VERTEX_REGIONS
        and (bool(raw_sid) or ":bg-review:" in raw_sid)
        and not raw_sid.startswith(("cron_", "subagent_"))
        and not (bool(canon_sid) and canon_sid in _non_interactive_session_ids)
    )
    if is_vertex_argon_model(model) and reg in (GLOBAL_VERTEX_REGION, *REGIONAL_VERTEX_REGIONS):
        if interactive and canon_sid:
            with _EUROPE_WEST2_STATE_LOCK:
                _non_interactive_session_ids.discard(canon_sid)
        elif not interactive and not is_bg_review_for_ew2_parent and canon_sid:
            with _EUROPE_WEST2_STATE_LOCK:
                _non_interactive_session_ids.add(canon_sid)
            if pinned_region is None:
                _record_session_vertex_region_affinity(session_id, GLOBAL_VERTEX_REGION)
        if (interactive or is_bg_review_for_ew2_parent) and pinned_region != GLOBAL_VERTEX_REGION:
            if sticky_wait_seconds is not None:
                wait_s = max(0.0, float(sticky_wait_seconds))
            elif pinned_region in REGIONAL_VERTEX_REGIONS and prompt_tok >= GLOBAL_STICKY_EPOCH_MIN_TOKENS:
                wait_s = EUROPE_WEST2_DEEP_EPOCH_WAIT_SECONDS
            elif pinned_region in REGIONAL_VERTEX_REGIONS:
                wait_s = EUROPE_WEST2_STICKY_WAIT_SECONDS
            elif pinned_region is None and interactive and bool(session_id):
                wait_s = EUROPE_WEST2_INITIAL_WAIT_SECONDS
            else:
                wait_s = 0.0
            target_region = (
                pinned_region
                if pinned_region in REGIONAL_VERTEX_REGIONS
                else reg
                if reg in REGIONAL_VERTEX_REGIONS
                else EUROPE_WEST2_VERTEX_REGION
            )
            if pinned_region is None:
                candidate_order = [target_region] + [
                    r for r in REGIONAL_VERTEX_REGIONS if r != target_region
                ]
                selected_region: Optional[str] = None
                for cand in candidate_order:
                    if _region_active_session_count(cand) >= _region_max_active_sessions(cand):
                        continue
                    if is_cold_prefill:
                        with _EUROPE_WEST2_STATE_LOCK:
                            if _cold_prefill_in_flight.get(cand, 0) >= REGIONAL_COLD_PREFILL_MAX_CONCURRENCY:
                                continue
                    selected_region = cand
                    break
                if selected_region is None:
                    if session_id:
                        _record_session_vertex_region_affinity(session_id, GLOBAL_VERTEX_REGION)
                    return (GLOBAL_VERTEX_REGION, False)
                target_region = selected_region
            cd_rem = _region_cooldown_remaining(target_region)
            is_deep_regional_sticky = (
                pinned_region in REGIONAL_VERTEX_REGIONS
                and prompt_tok >= GLOBAL_STICKY_EPOCH_MIN_TOKENS
            )
            cd_wait_cap = (
                EUROPE_WEST2_COOLDOWN_MAX_TOTAL_WAIT_SECONDS
                if sticky_wait_seconds is None
                else max(0.0, float(sticky_wait_seconds))
            )
            cd_slept_total = 0.0
            while (
                is_deep_regional_sticky
                and 0.0 < cd_rem <= EUROPE_WEST2_COOLDOWN_TAIL_WAIT_SECONDS
                and cd_slept_total < cd_wait_cap
            ):
                sleep_chunk = min(cd_rem, cd_wait_cap - cd_slept_total)
                if sleep_chunk <= 0.0:
                    break
                time.sleep(sleep_chunk)
                cd_slept_total += sleep_chunk
                if sticky_wait_seconds is not None:
                    wait_s = max(0.0, wait_s - sleep_chunk)
                cd_rem = _region_cooldown_remaining(target_region)
            if cd_rem <= 0.0:
                sem = _region_semaphore(target_region)
                sem_wait_s = (
                    max(wait_s, EUROPE_WEST2_DEEP_SEMAPHORE_WAIT_SECONDS)
                    if (is_deep_regional_sticky and sticky_wait_seconds is None)
                    else wait_s
                )
                if (
                    pinned_region in REGIONAL_VERTEX_REGIONS
                    or (pinned_region is None and interactive and bool(session_id))
                ) and sem_wait_s > 0.0:
                    acquired = sem.acquire(blocking=True, timeout=sem_wait_s)
                else:
                    acquired = sem.acquire(blocking=False)
                if not acquired and pinned_region is None and interactive and bool(session_id):
                    for alt_region in REGIONAL_VERTEX_REGIONS:
                        if alt_region == target_region:
                            continue
                        if _region_cooldown_remaining(alt_region) > 0.0:
                            continue
                        if _region_active_session_count(alt_region) >= _region_max_active_sessions(alt_region):
                            continue
                        if is_cold_prefill:
                            with _EUROPE_WEST2_STATE_LOCK:
                                if _cold_prefill_in_flight.get(alt_region, 0) >= REGIONAL_COLD_PREFILL_MAX_CONCURRENCY:
                                    continue
                        if _region_semaphore(alt_region).acquire(blocking=False):
                            target_region = alt_region
                            acquired = True
                            break
                if acquired:
                    if is_cold_prefill:
                        with _EUROPE_WEST2_STATE_LOCK:
                            _cold_prefill_in_flight[target_region] = (
                                _cold_prefill_in_flight.get(target_region, 0) + 1
                            )
                    _record_session_vertex_region_affinity(session_id, target_region)
                    return (target_region, True)
            if session_id and (
                pinned_region is None
                or (interactive and prompt_tok < GLOBAL_STICKY_EPOCH_MIN_TOKENS)
            ):
                _record_session_vertex_region_affinity(session_id, GLOBAL_VERTEX_REGION)
            elif session_id and pinned_region in REGIONAL_VERTEX_REGIONS:
                canon_sid = _canonical_affinity_session_id(session_id)
                if canon_sid:
                    with _EUROPE_WEST2_STATE_LOCK:
                        if _session_region_affinity.get(canon_sid) == pinned_region:
                            _session_region_last_seen[canon_sid] = time.monotonic()
        return (GLOBAL_VERTEX_REGION, False)
    return (reg, False)


def release_vertex_hybrid_slot(client: Any) -> bool:
    """Idempotently release a regional concurrency slot held on *client*."""
    if client is None or getattr(client, "_vertex_regional_slot_held", False) is not True:
        return False
    slot_region = getattr(client, "_vertex_regional_slot_region", None)
    if not slot_region:
        parsed = parse_vertex_base_url(
            getattr(client, "_vertex_routed_base_url", None)
            or getattr(client, "base_url", None)
        )
        if parsed:
            slot_region = parsed[1]
    try:
        client._vertex_regional_slot_held = False
        client._vertex_regional_slot_region = None
    except Exception:
        pass
    resolved_region = (
        slot_region if slot_region in REGIONAL_VERTEX_REGIONS else EUROPE_WEST2_VERTEX_REGION
    )
    sem = _region_semaphore(resolved_region)
    try:
        sem.release()
        with _EUROPE_WEST2_STATE_LOCK:
            cur_cold = _cold_prefill_in_flight.get(resolved_region, 0)
            if cur_cold > 0:
                _cold_prefill_in_flight[resolved_region] = cur_cold - 1
        return True
    except ValueError:
        return False


def _vertex_config() -> dict:
    """Return the ``vertex:`` section of config.yaml, or {} on any failure."""
    try:
        from hermes_cli.config import load_config

        section = load_config().get("vertex")
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _env_or_config(env_var: str, config_key: str) -> str:
    """Setting precedence: env/secret > config.yaml; "" when neither is set."""
    return (_get_secret(env_var) or "").strip() or str(_vertex_config().get(config_key) or "").strip()


def _resolve_region(explicit: Optional[str] = None) -> str:
    """Region precedence: explicit arg > VERTEX_REGION env > config.yaml > default."""
    return explicit or _env_or_config("VERTEX_REGION", "region") or DEFAULT_REGION


def _resolve_project_override() -> Optional[str]:
    """Project-ID override (VERTEX_PROJECT_ID env > config.yaml), or None to use the creds' embedded project_id."""
    return _env_or_config("VERTEX_PROJECT_ID", "project_id") or None


def _resolve_credentials_path(explicit: Optional[str]) -> Optional[str]:
    if explicit and os.path.exists(explicit):
        return explicit
    # get_secret, not os.environ: under a multiplex gateway os.environ reflects whichever
    # .env loaded at boot, and a raw read could mint (and bill) another profile's SA tokens.
    for env_var in ("VERTEX_CREDENTIALS_PATH", "GOOGLE_APPLICATION_CREDENTIALS"):
        path = _get_secret(env_var)
        if path and os.path.exists(path):
            return path
    if not is_multiplex_active():
        cfg_path = str(_vertex_config().get("credentials_path") or "").strip()
        if cfg_path:
            expanded = os.path.expanduser(cfg_path)
            if os.path.exists(expanded):
                return expanded
    return None


def _sa_snapshot(resolved_path: Optional[str]) -> tuple[Optional[bytes], tuple[Any, ...]]:
    """Resolve (bytes-or-None, cache key) for one credential attempt.

    - No path (ADC): (None, ("__adc__",)) sentinel key.
    - Readable file: (bytes, (path, sha256)).
    - Unreadable file: (None, (path,)) — the caller falls back to the SDK's own file read.

    The key fingerprints file CONTENT, not stat metadata (a metadata-preserving
    atomic replacement can swap the private key under an identical stat signature,
    and this cache guards an identity). Returning the bytes lets the caller build
    credentials from the SAME snapshot the key was computed from (no stat->read TOCTOU).
    """
    if not resolved_path:
        return None, ("__adc__",)
    try:
        with open(resolved_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None, (resolved_path,)
    return raw, (resolved_path, hashlib.sha256(raw).hexdigest())


def _load_credentials(resolved_path: Optional[str], sa_raw: Optional[bytes]) -> Optional[tuple[Any, Optional[str]]]:
    """Build (credentials, project_id) for a cache miss; None when ADC must be refused."""
    if resolved_path:
        if sa_raw is not None:
            creds = service_account.Credentials.from_service_account_info(json.loads(sa_raw), scopes=_CLOUD_PLATFORM_SCOPES)
        else:
            # Unreadable at key time: let the SDK try the file directly.
            creds = service_account.Credentials.from_service_account_file(resolved_path, scopes=_CLOUD_PLATFORM_SCOPES)
        return creds, creds.project_id
    # google.auth.default() reads GOOGLE_APPLICATION_CREDENTIALS from os.environ (set by whichever
    # profile loaded first); this profile doesn't define it, so refuse a stranger's identity.
    if is_multiplex_active() and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        logger.warning(
            "Vertex ADC skipped for this profile: GOOGLE_APPLICATION_CREDENTIALS is set in the process environment "
            "(from another profile's .env) but not in this profile's own config. Set VERTEX_CREDENTIALS_PATH in this "
            "profile's .env instead of relying on ADC."
        )
        return None
    return google.auth.default(scopes=_CLOUD_PLATFORM_SCOPES)


def _expiry_epoch(expiry: Any) -> Optional[float]:
    """UTC epoch seconds for ``creds.expiry``.

    ``google-auth`` stores ``creds.expiry`` as a naive UTC ``datetime`` (``tzinfo=None``); calling
    ``.timestamp()`` directly on a naive ``datetime`` interprets it in the host's local timezone
    (e.g. CET ``UTC+01:00`` shifts a freshly minted 3600 s token back by 3600 s to ``now - 1 s``,
    making ``_needs_refresh`` True on every call).
    """
    if not isinstance(expiry, datetime):
        return None
    if expiry.tzinfo is None:
        return expiry.replace(tzinfo=UTC).timestamp()
    return expiry.timestamp()


def _needs_refresh(creds) -> bool:
    """No token, expired, or within 5 minutes of expiry."""
    expiry_ts = _expiry_epoch(getattr(creds, "expiry", None))
    return (
        not getattr(creds, "token", None)
        or getattr(creds, "expired", False)
        or (expiry_ts is not None and (expiry_ts - time.time()) < 300)
    )


def _cached_credentials(resolved_path: Optional[str], sa_raw: Optional[bytes], cache_key: tuple[Any, ...]):
    """(credentials, project_id) from the per-content cache, loading on a miss; None when refused."""
    cached = _creds_cache.get(cache_key)
    if cached is None:
        cached = _load_credentials(resolved_path, sa_raw)
        if cached is None:
            return None
        _creds_cache[cache_key] = cached
        # A rotation leaves the old signature's entry behind; keep at most
        # one Credentials per file so stale identities can't be reused.
        for k in [k for k in _creds_cache if k != cache_key and k[0] == cache_key[0]]:
            _creds_cache.pop(k, None)
    return cached


def get_vertex_credentials(
    credentials_path: Optional[str] = None, *, force_refresh: bool = False
) -> tuple[Optional[str], Optional[str]]:
    """Return (fresh access_token, project_id) or (None, None); Credentials cached per file content."""
    if not _ensure_google_auth():
        return None, None

    resolved_path = _resolve_credentials_path(credentials_path)
    # One read serves both the cache key and credential construction (creds always match the fingerprint).
    sa_raw, cache_key = _sa_snapshot(resolved_path)

    try:
        cached = _cached_credentials(resolved_path, sa_raw, cache_key)
        if cached is None:
            return None, None
        creds, project_id = cached
        if force_refresh or _needs_refresh(creds):
            creds.refresh(google.auth.transport.requests.Request())
        return creds.token, _resolve_project_override() or project_id
    except Exception as e:
        logger.error(f"Failed to resolve Vertex AI credentials: {e}")
        _creds_cache.pop(cache_key, None)
        # If ADC failed (e.g. expired refresh token), try the SA file before giving
        # up — it may have been added after startup. Keyed on this attempt being ADC.
        sa_path = None if resolved_path else _resolve_credentials_path(credentials_path)
        if sa_path:
            logger.info("ADC failed, retrying with service account: %s", sa_path)
            return get_vertex_credentials(sa_path, force_refresh=force_refresh)
        return None, None


def _resolve_multi_region_policy() -> str:
    """Policy for Gemini 4 multi-region models (Neon/Hydrogen/Carbon):
    ``global`` (default), ``eu``, ``us``, or ``canary_50`` (deterministic 50/50 sticky split).
    Precedence: HERMES_VERTEX_NEON_REGION env > config.yaml vertex.neon_region > 'global'.
    """
    raw = (
        os.environ.get("HERMES_VERTEX_NEON_REGION")
        or str(_vertex_config().get("neon_region") or "")
    ).strip().lower()
    if raw in ("eu", "us", "canary_50", "global"):
        return raw
    return GLOBAL_VERTEX_REGION


def build_vertex_base_url(project_id: str, region: str = DEFAULT_REGION) -> str:
    """OpenAI-compatible Vertex base URL.

    - ``global`` -> ``aiplatform.googleapis.com``
    - ``eu`` / ``us`` multi-region -> ``aiplatform.{region}.rep.googleapis.com``
    - single region (e.g. ``europe-west2``) -> ``{region}-aiplatform.googleapis.com``
    """
    if region == GLOBAL_VERTEX_REGION:
        host = "aiplatform.googleapis.com"
    elif region in MULTI_REGION_VERTEX_REGIONS:
        host = f"aiplatform.{region}.rep.googleapis.com"
    else:
        host = f"{region}-aiplatform.googleapis.com"
    return f"https://{host}/v1beta1/projects/{project_id}/locations/{region}/endpoints/openapi"


def get_vertex_config(
    credentials_path: Optional[str] = None, region: Optional[str] = None, *, force_refresh: bool = False
) -> tuple[Optional[str], Optional[str]]:
    """Resolve (access_token, base_url) for Vertex AI, or (None, None) on failure."""
    token, project_id = get_vertex_credentials(credentials_path, force_refresh=force_refresh)
    if not token or not project_id:
        return None, None
    return token, build_vertex_base_url(project_id, _resolve_region(region))


def get_vertex_base_url(credentials_path: Optional[str] = None, region: Optional[str] = None) -> Optional[str]:
    """The Vertex base_url WITHOUT minting a token (no OAuth exchange), or None when the credentials
    cannot be loaded or no project resolves. For processes that route but never send — the
    gateway-backed CLI, whose gateway session mints its own token for the turn."""
    if not _ensure_google_auth():
        return None
    resolved_path = _resolve_credentials_path(credentials_path)
    sa_raw, cache_key = _sa_snapshot(resolved_path)
    try:
        cached = _cached_credentials(resolved_path, sa_raw, cache_key)
    except Exception:  # health: allow BLE001 -- same boundary as get_vertex_credentials: unloadable credentials are main's AuthError copy
        logger.debug("Vertex AI credentials could not be loaded", exc_info=True)
        _creds_cache.pop(cache_key, None)
        return None
    project_id = _resolve_project_override() or (cached[1] if cached else None)
    return build_vertex_base_url(project_id, _resolve_region(region)) if cached and project_id else None


def vertex_access_token() -> str:
    """Callable API key: a fresh Vertex token, minted on first use and reused until near expiry.
    One stable object, so a credential re-resolution never reads as a key change."""
    token, _project_id = get_vertex_credentials()
    if not token:
        raise RuntimeError("Vertex AI OAuth token could not be minted (see the Vertex credential settings)")
    return token


tag_credential_source(vertex_access_token, VERTEX_OAUTH)


def has_vertex_credentials() -> bool:
    """Fast check (no network): a resolvable SA JSON path, or an explicit project ID (implies ADC)."""
    return bool(_resolve_credentials_path(None) or _resolve_project_override())


def has_explicit_vertex_config() -> bool:
    """True only when the user deliberately pointed Hermes at Vertex.

    Stricter than :func:`has_vertex_credentials`: an ambient ``GOOGLE_APPLICATION_CREDENTIALS``
    must NOT gate the model picker open (unknowing spend). Only Hermes-scoped signals count.
    """
    if _resolve_project_override():
        return True
    sa_path = _get_secret("VERTEX_CREDENTIALS_PATH")
    return bool(sa_path and os.path.isfile(sa_path) and os.access(sa_path, os.R_OK))


def _flatten_openai_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return str(content or "")


def call_vertex_native_flex(
    *,
    messages: list[dict[str, Any]],
    model: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stop: Optional[list[str]] = None,
    timeout: float = 90.0,
) -> Any:
    """Call Vertex native ``:generateContent`` with Sync Flex (0.50x list price).

    Only supported on eligible models (verified live on ``gemini-3.8-flash``; Gemini 4
    models reject Flex with HTTP 400). Returns an OpenAI-chat-completions-compatible
    ``SimpleNamespace`` carrying ``service_tier='flex'`` when Vertex confirms
    ``usageMetadata.trafficType == 'ON_DEMAND_FLEX'``.
    """
    from types import SimpleNamespace
    import urllib.request

    token, project_id = get_vertex_credentials()
    if not token or not project_id:
        raise RuntimeError("Vertex credentials unavailable for Sync Flex request")

    bare_model = str(model or "").strip()
    if "/" in bare_model:
        bare_model = bare_model.rsplit("/", 1)[-1]
    if "@" in bare_model:
        bare_model = bare_model.split("@", 1)[0]

    system_chunks: list[str] = []
    contents: list[dict[str, Any]] = []
    for msg in messages or []:
        role = str(msg.get("role") or "user").strip().lower()
        text = _flatten_openai_message_content(msg.get("content"))
        if not text:
            continue
        if role == "system":
            system_chunks.append(text)
            continue
        vertex_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == vertex_role:
            contents[-1]["parts"].append({"text": text})
        else:
            contents.append({"role": vertex_role, "parts": [{"text": text}]})
    if not contents:
        contents.append({"role": "user", "parts": [{"text": ""}]})

    # Thinking tokens share maxOutputTokens on gemini-3.8-flash; enforce >= 4096 floor
    # so reasoning never starves the handoff summary.
    gen_cfg: dict[str, Any] = {
        "maxOutputTokens": max(4096, int(max_tokens)) if max_tokens else 8192,
    }
    if temperature is not None:
        gen_cfg["temperature"] = float(temperature)
    if top_p is not None:
        gen_cfg["topP"] = float(top_p)
    if stop:
        gen_cfg["stopSequences"] = list(stop)

    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": gen_cfg,
    }
    if system_chunks:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_chunks)}]}

    url = (
        f"https://aiplatform.googleapis.com/v1beta1/projects/{project_id}"
        f"/locations/global/publishers/google/models/{bare_model}:generateContent"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Vertex-AI-LLM-Shared-Request-Type": "flex",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Vertex Sync Flex returned no candidates: {str(data)[:240]}")
    cand0 = candidates[0]
    parts = ((cand0.get("content") or {}).get("parts") or [])
    text_out = "".join(
        str(p.get("text") or "")
        for p in parts
        if isinstance(p, dict) and not p.get("thought")
    )
    raw_finish = str(cand0.get("finishReason") or "STOP").upper()
    finish_map = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
    }
    finish_reason = finish_map.get(raw_finish, raw_finish.lower())

    um = data.get("usageMetadata") or {}
    prompt_tok = int(um.get("promptTokenCount") or 0)
    cached_tok = int(um.get("cachedContentTokenCount") or 0)
    cand_tok = int(um.get("candidatesTokenCount") or 0)
    thought_tok = int(um.get("thoughtsTokenCount") or 0)
    total_tok = int(um.get("totalTokenCount") or (prompt_tok + cand_tok + thought_tok))
    traffic_type = str(um.get("trafficType") or "").upper()
    service_tier = "flex" if traffic_type == "ON_DEMAND_FLEX" else "default"
    resp_id = str(data.get("responseId") or f"flex_{int(time.time() * 1000)}")

    return SimpleNamespace(
        id=f"vflex-{resp_id}",
        model=model,
        service_tier=service_tier,
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(role="assistant", content=text_out),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tok,
            completion_tokens=cand_tok + thought_tok,
            total_tokens=total_tok,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tok),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=thought_tok),
        ),
    )
