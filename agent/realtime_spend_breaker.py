"""Cross-process WAL SQLite AI spend ledger & multi-tier fast leak circuit breaker.

Tracks real-time net EUR AI spend across all Hermes processes (Desktop, Gateway,
Cron, Subagents, background_review) and providers (Vertex AI after -50% promo credit,
OpenRouter, etc.) with <1s shared visibility via WAL-mode SQLite.

Enforces quality-neutral multi-tier leak stopping:
- Tier 0: Exempts critical 24/7 operational responders (chat-247-*, evelina-*,
  alert-triage, pos-sentinel, secret-403-fastresponder, incident-*) from daily caps.
- Tier 1: Halts runaway non-interactive sessions (> €4.00 net in 15m or > €10.00 in 1h
  or >= 4 cache floor-resets with > €2.50 in 15m) and stalled/non-productive Desktop
  loops (>= 35 consecutive calls since last human message with making_productive_progress=False
  and > €6.00 net in 15m).
- Tier 2: Suppresses background_review and non-Tier-0 bulk crons when 15m net > €8.00
  or today net > €150.00 (75% of the €200/day May 2027 AI spend target).
- Tier 3: Hard-halts all non-Tier-0 non-interactive calls (cron, subagent,
  background_review) when today net > €195.00 (€200/day = €6,000/mo May 2027 target),
  while never blocking productive interactive Desktop turns.
"""
from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_STOCKHOLM_TZ = ZoneInfo("Europe/Stockholm")

# Vertex AI net EUR rates per 1M tokens on uop-atlas (after -50% promotional credit)
# USD list -> -50% promo -> EUR at 0.8981 EUR/USD (verified against BigQuery billing_export 202611)
# Global SKUs: 3BAE-62B7-C9B9 (in), 3B3E-7322-E883 (cache), 1E84-F91D-8B95 (out)
VERTEX_ARGON_UNCACHED_IN_EUR_PER_M = 1.796200
VERTEX_ARGON_CACHED_IN_EUR_PER_M = 0.089810
VERTEX_ARGON_OUT_EUR_PER_M = 8.981000
VERTEX_ARGON_LIST_UNCACHED_IN_EUR_PER_M = 3.592400
VERTEX_ARGON_LIST_CACHED_IN_EUR_PER_M = 0.179620
VERTEX_ARGON_LIST_OUT_EUR_PER_M = 17.962000
ARGON_PROMO_EXPIRY_EPOCH_UTC = 1796112000.0  # 2026-12-01T08:00:00Z (2026-11-30 23:59:59 PT)

# Regional europe-west2, asia-southeast1 & eu multi-region SKUs carry 1.10x multiplier
# Verified via bipartite KV-cache sharing graph + BigQuery SKU correlation + live EU rep probe
VERTEX_EU_WEST2_REGIONAL_MULTIPLIER = 1.10
VERTEX_FLEX_DISCOUNT_MULTIPLIER = 0.50
_EU_WEST2_MACRO_CLUSTERS: frozenset[str] = frozenset(
    {
        # europe-west2 regional macro clusters
        "0L8P", "9_oP", "6-wP", "6_gP", "08QP", "4ATb", "l-oP", "9wMP",
        # asia-southeast1 regional macro clusters (enabled 2026-11-18 00:00 CET)
        "k7QP", "z7sP", "i9sP", "7PkP", "3LUP", "4vEP", "4t4P", "tOkP",
        # eu multi-region (aiplatform.eu.rep.googleapis.com) macro clusters (verified 2026-11-24)
        "2s4P", "ndYP", "wdYP",
    }
)

# Gemini 4 non-Argon flat EUR rates per 1M tokens (NO -50% promo; USD list * 0.8981 EUR/USD)
# Verified against Vertex Model Garden rate card 2026-11-24:
#   gemini-4-neon:     $3.00 in / $0.15 cached / $15.00 out (95% cache read discount)
#   gemini-4-hydrogen: $1.50 in / $0.075 cached / $7.50 out (95% cache read discount)
#   gemini-4-carbon:   $2.00 in / $0.10 cached / $10.00 out (95% cache read discount)
# Jan 12, 2027 00:00 PT (2027-01-12T08:00:00Z): 1.25x uncached input write surcharge on Neon/Hydrogen
#   gemini-4-neon write cliff:     $3.75 in ($3.367875 EUR/M)
#   gemini-4-hydrogen write cliff: $1.875 in ($1.683938 EUR/M)
VERTEX_NEON_UNCACHED_IN_EUR_PER_M = 2.694300
VERTEX_NEON_UNCACHED_IN_JAN12_EUR_PER_M = 3.367875
VERTEX_NEON_CACHED_IN_EUR_PER_M = 0.134715
VERTEX_NEON_OUT_EUR_PER_M = 13.471500
VERTEX_HYDROGEN_UNCACHED_IN_EUR_PER_M = 1.347150
VERTEX_HYDROGEN_UNCACHED_IN_JAN12_EUR_PER_M = 1.683938
VERTEX_HYDROGEN_CACHED_IN_EUR_PER_M = 0.067358
VERTEX_HYDROGEN_OUT_EUR_PER_M = 6.735750
VERTEX_CARBON_UNCACHED_IN_EUR_PER_M = 1.796200
VERTEX_CARBON_CACHED_IN_EUR_PER_M = 0.089810
VERTEX_CARBON_OUT_EUR_PER_M = 8.981000
GEMINI4_WRITE_SURCHARGE_EPOCH_UTC = 1799740800.0  # 2027-01-12T08:00:00Z (2027-01-12 00:00:00 PT)

# Gemini 3.5 Flash-Lite flat EUR rates per 1M tokens ($0.30 in / $0.03 cached / $2.50 out * 0.8981)
VERTEX_FLASH_LITE_UNCACHED_IN_EUR_PER_M = 0.269430
VERTEX_FLASH_LITE_CACHED_IN_EUR_PER_M = 0.026943
VERTEX_FLASH_LITE_OUT_EUR_PER_M = 2.245250

# Global Flash SKUs: 2A35-AC68-14D6 (in), 8658-6939-8BEC (cache), 90FD-DC5F-DC55 (out)
VERTEX_FLASH_UNCACHED_IN_EUR_PER_M = 0.673573
VERTEX_FLASH_CACHED_IN_EUR_PER_M = 0.067357
VERTEX_FLASH_OUT_EUR_PER_M = 3.367844
VERTEX_FLASH_LIST_UNCACHED_IN_EUR_PER_M = 1.347146
VERTEX_FLASH_LIST_CACHED_IN_EUR_PER_M = 0.134714
VERTEX_FLASH_LIST_OUT_EUR_PER_M = 6.735688
FLASH_PROMO_EXPIRY_EPOCH_UTC = 1798790400.0  # 2027-01-01T08:00:00Z (2026-12-31 23:59:59 PT)


def _is_gemini4_vertex_model(model: str) -> bool:
    mod = (model or "").lower()
    return any(k in mod for k in ("argon", "neon", "hydrogen", "carbon"))


def resolve_vertex_rates(model: str, ts: float | None = None) -> tuple[float, float, float]:
    now_ts = float(ts if ts is not None else time.time())
    mod = (model or "").lower()
    if "argon" in mod:
        if now_ts >= ARGON_PROMO_EXPIRY_EPOCH_UTC:
            return (
                VERTEX_ARGON_LIST_UNCACHED_IN_EUR_PER_M,
                VERTEX_ARGON_LIST_CACHED_IN_EUR_PER_M,
                VERTEX_ARGON_LIST_OUT_EUR_PER_M,
            )
        return (
            VERTEX_ARGON_UNCACHED_IN_EUR_PER_M,
            VERTEX_ARGON_CACHED_IN_EUR_PER_M,
            VERTEX_ARGON_OUT_EUR_PER_M,
        )
    if "neon" in mod:
        in_r = (
            VERTEX_NEON_UNCACHED_IN_JAN12_EUR_PER_M
            if now_ts >= GEMINI4_WRITE_SURCHARGE_EPOCH_UTC
            else VERTEX_NEON_UNCACHED_IN_EUR_PER_M
        )
        return (
            in_r,
            VERTEX_NEON_CACHED_IN_EUR_PER_M,
            VERTEX_NEON_OUT_EUR_PER_M,
        )
    if "hydrogen" in mod:
        in_r = (
            VERTEX_HYDROGEN_UNCACHED_IN_JAN12_EUR_PER_M
            if now_ts >= GEMINI4_WRITE_SURCHARGE_EPOCH_UTC
            else VERTEX_HYDROGEN_UNCACHED_IN_EUR_PER_M
        )
        return (
            in_r,
            VERTEX_HYDROGEN_CACHED_IN_EUR_PER_M,
            VERTEX_HYDROGEN_OUT_EUR_PER_M,
        )
    if "carbon" in mod:
        return (
            VERTEX_CARBON_UNCACHED_IN_EUR_PER_M,
            VERTEX_CARBON_CACHED_IN_EUR_PER_M,
            VERTEX_CARBON_OUT_EUR_PER_M,
        )
    if "flash-lite" in mod or "flash_lite" in mod:
        return (
            VERTEX_FLASH_LITE_UNCACHED_IN_EUR_PER_M,
            VERTEX_FLASH_LITE_CACHED_IN_EUR_PER_M,
            VERTEX_FLASH_LITE_OUT_EUR_PER_M,
        )
    if now_ts >= FLASH_PROMO_EXPIRY_EPOCH_UTC:
        return (
            VERTEX_FLASH_LIST_UNCACHED_IN_EUR_PER_M,
            VERTEX_FLASH_LIST_CACHED_IN_EUR_PER_M,
            VERTEX_FLASH_LIST_OUT_EUR_PER_M,
        )
    return (
        VERTEX_FLASH_UNCACHED_IN_EUR_PER_M,
        VERTEX_FLASH_CACHED_IN_EUR_PER_M,
        VERTEX_FLASH_OUT_EUR_PER_M,
    )

USD_TO_EUR = 0.8981

# Budget & breaker thresholds (EUR net) aligned with May 2027 Sparplan (€6,000/mo = €200/day)
MAY_2027_DAILY_TARGET_EUR = 200.0
MAY_2027_MONTHLY_AI_TARGET_EUR = 6000.0
MAY_2027_MONTHLY_INFRA_TARGET_EUR = 10500.0

TIER1_NON_INTERACTIVE_15M_CAP_EUR = 4.00
TIER1_NON_INTERACTIVE_1H_CAP_EUR = 10.00
TIER1_NON_INTERACTIVE_CACHE_RESET_COUNT = 4
TIER1_NON_INTERACTIVE_CACHE_RESET_MIN_EUR = 0.60
TIER0_CRITICAL_CACHE_RESET_COUNT = 8
TIER0_CRITICAL_CACHE_RESET_MIN_EUR = 1.50

TIER1_STALLED_DESKTOP_MIN_CALLS = 35
TIER1_STALLED_DESKTOP_15M_CAP_EUR = 6.00
TIER1_RUNAWAY_DESKTOP_TURN_MAX_CALLS = 90
TIER1_RUNAWAY_DESKTOP_15M_CAP_EUR = 10.00
TIER1_RUNAWAY_DESKTOP_1H_CAP_EUR = 20.00

_SESSION_PARENT_MAP: dict[str, str] = {}


def record_session_parent(child_session_id: str, parent_session_id: str, now: float | None = None) -> None:
    if not (child_session_id and parent_session_id and child_session_id != parent_session_id):
        return
    _SESSION_PARENT_MAP[child_session_id] = parent_session_id
    ts_now = float(now if now is not None else time.time())
    try:
        con = _connect()
        try:
            with con:
                con.execute(
                    """
                    INSERT OR REPLACE INTO session_parent_lineage (
                        child_session_id, parent_session_id, updated_at
                    ) VALUES (?, ?, ?)
                    """,
                    (child_session_id, parent_session_id, ts_now),
                )
        finally:
            con.close()
    except Exception:
        pass


def _lookup_persisted_parent_session_id(child_session_id: str) -> str:
    if not child_session_id:
        return ""
    cached = _SESSION_PARENT_MAP.get(child_session_id, "")
    if cached:
        return cached
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT parent_session_id FROM session_parent_lineage WHERE child_session_id = ? LIMIT 1",
                (child_session_id,),
            ).fetchone()
            if row and row[0]:
                parent_sid = str(row[0]).strip()
                if parent_sid and parent_sid != child_session_id:
                    _SESSION_PARENT_MAP[child_session_id] = parent_sid
                    return parent_sid
        finally:
            con.close()
    except Exception:
        pass
    if not os.environ.get("HERMES_AI_SPEND_DB_PATH"):
        state_db = Path.home() / ".hermes" / "state.db"
        if state_db.is_file():
            try:
                scon = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=1.0)
                try:
                    srow = scon.execute(
                        "SELECT parent_session_id FROM sessions WHERE id = ? LIMIT 1",
                        (child_session_id,),
                    ).fetchone()
                    if srow and srow[0]:
                        parent_sid = str(srow[0]).strip()
                        if parent_sid and parent_sid != child_session_id:
                            record_session_parent(child_session_id, parent_sid)
                            return parent_sid
                finally:
                    scon.close()
            except Exception:
                pass
    return ""


def _resolve_lineage_session_ids(
    session_id: str,
    parent_session_id: str = "",
    lineage_session_ids: tuple[str, ...] | list[str] | set[str] = (),
    max_depth: int = 128,
) -> tuple[str, ...]:
    if parent_session_id and session_id and session_id != parent_session_id:
        record_session_parent(session_id, parent_session_id)
    sids: list[str] = [session_id] if session_id else []
    seen = set(sids)
    cur_sid = parent_session_id or _lookup_persisted_parent_session_id(session_id)
    while cur_sid and cur_sid not in seen and len(sids) < max_depth:
        sids.append(cur_sid)
        seen.add(cur_sid)
        cur_sid = _lookup_persisted_parent_session_id(cur_sid)
    for extra in lineage_session_ids or ():
        es = str(extra or "").strip()
        if es and es not in seen and len(sids) < max_depth:
            sids.append(es)
            seen.add(es)
    return tuple(sids) or (session_id,)

TIER0_CRITICAL_CRON_15M_SAFETY_CAP_EUR = 12.00

TIER2_ESTATE_15M_BURST_EUR = 8.00
TIER2_DAILY_SOFT_CAP_EUR = 150.00
TIER3_DAILY_HARD_CAP_EUR = 195.00
_CF92_ITER0_FIX_REV = 3

_TIER0_CRITICAL_PREFIXES: tuple[str, ...] = (
    "chat-247",
    "chat247-",
    "nw-chat-",
    "newintra-",
    "workos-",
    "medius-",
    "lager-",
    "2izii-",
    "post-deploy-verify-",
    "evelina-",
    "emir-morning-routine",
    "morning-preemptive-audit",
    "daily-retrospective",
    "night-driver",
    "saffar-",
    "atlas-steward",
    "atlas-freshness",
    "atlas-answer-drafts",
    "atlas-qa-probe",
    "atlas-decision-sync",
    "atlas-doc-review",
    "atlas-estate-sync",
    "hiss-",
    "archive-done-jobs",
    "iz-",
    "label-hygiene",
    "monthly-orphaned-access-reconciliation",
    "itsupport-digest-",
    "ops-triage",
    "delta-sweep",
    "subagent-selfheal",
    "one-dev-freshness",
    "optimize-measure-after",
    "workspace-policy-snapshot",
    "quarterly-db-acl-sweep",
    "memory-",
    "gauth-",
    "secret-leak-",
    "cert-expiry-",
    "prod-smoke",
    "solidpos-",
    "wiki-weekly-lint",
    "decision-log-pipeline",
    "daily-db-backup-drill",
    "alert-triage",
    "alerts-digest",
    "pos-sentinel",
    "secret-403",
    "incident-",
    "estate-doctor",
    "svea-",
    "cart-",
    "store-support-",
    "update-autorescue",
    "hermes-update",
    "hermes-carried",
    "carried-fixes",
    "cron-failure-watch",
    "italerts-",
    "checkpoints-prune",
    "plane-triage",
    "verify-nutanix",
    "sync-hermes",
    "pr-corpus",
    "voice-register",
)

_LAST_STATUS_WRITE_TS: float = 0.0


def _db_path() -> Path:
    env_p = os.environ.get("HERMES_AI_SPEND_DB_PATH")
    if env_p:
        return Path(env_p)
    return Path.home() / ".hermes" / "state" / "ai-spend-realtime.db"


def _status_path() -> Path:
    env_p = os.environ.get("HERMES_AI_SPEND_STATUS_PATH")
    if env_p:
        return Path(env_p)
    return Path.home() / ".hermes" / "state" / "ai-cost-realtime-status.json"


def _emergency_brake_flag_path() -> Path:
    env_p = os.environ.get("HERMES_AI_EMERGENCY_BRAKE_FLAG_PATH")
    if env_p:
        return Path(env_p)
    return Path.home() / ".hermes" / "state" / "ai-emergency-brake-override.flag"


def _spend_handbrake_flag_path() -> Path:
    env_p = os.environ.get("HERMES_AI_SPEND_HANDBRAKE_FLAG_PATH")
    if env_p:
        return Path(env_p)
    return Path.home() / ".hermes" / "state" / "ai-spend-handbrake.flag"


def _is_flag_file_active(fpath: Path) -> bool:
    try:
        if not fpath.exists():
            return False
        raw = fpath.read_text(encoding="utf-8", errors="ignore").strip()
        if not raw:
            return False
        if raw.lower() in ("off", "false", "0", "no"):
            return False
        if raw.startswith("{"):
            data = json.loads(raw)
            if isinstance(data, dict):
                return bool(data.get("active", True))
        return True
    except Exception:
        return False


def _budget_cap_path() -> Path:
    env_p = os.environ.get("HERMES_AI_BUDGET_CAP_PATH")
    if env_p:
        return Path(env_p)
    return Path.home() / ".hermes" / "state" / "ai-budget-cap.json"


def get_active_budget_cap(now: float | None = None) -> dict[str, Any] | None:
    ts_now = float(now if now is not None else time.time())
    try:
        cpath = _budget_cap_path()
        if not cpath.exists():
            return None
        data = json.loads(cpath.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("active", True):
            return None
        cap_ts = float(data.get("ts") or 0.0)
        exp = float(data.get("expires_at") or (cap_ts + 4 * 3600.0))
        if cap_ts > 0 and exp > cap_ts + 86400.0:
            exp = cap_ts + 86400.0
        if exp > 0 and ts_now >= exp:
            return None
        has_hourly_cap = any(
            k in data for k in ("cap_usd_hr", "cap_usd", "cap_eur_hr", "cap_eur_15m")
        )
        cap_usd_hr = float(data.get("cap_usd_hr") or data.get("cap_usd") or 15.0)
        cap_eur_hr = float(data.get("cap_eur_hr") or round(cap_usd_hr * 0.90, 2))
        cap_eur_15m = float(data.get("cap_eur_15m") or round(cap_eur_hr / 4.0, 2))
        tier2_soft_eur = float(
            data.get("tier2_daily_soft_cap_eur")
            or data.get("daily_soft_cap_eur")
            or TIER2_DAILY_SOFT_CAP_EUR
        )
        tier3_hard_eur = float(
            data.get("tier3_daily_hard_cap_eur")
            or data.get("daily_hard_cap_eur")
            or max(TIER3_DAILY_HARD_CAP_EUR, round(tier2_soft_eur * 1.30, 2))
        )
        tier2_burst_15m_eur = float(
            data.get("tier2_estate_15m_burst_eur")
            or data.get("non_interactive_15m_burst_eur")
            or TIER2_ESTATE_15M_BURST_EUR
        )
        return {
            "has_hourly_cap": has_hourly_cap,
            "cap_usd_hr": cap_usd_hr,
            "cap_eur_hr": cap_eur_hr,
            "cap_eur_15m": cap_eur_15m,
            "tier2_daily_soft_cap_eur": tier2_soft_eur,
            "tier3_daily_hard_cap_eur": tier3_hard_eur,
            "tier2_estate_15m_burst_eur": tier2_burst_15m_eur,
            "expires_at": exp,
            "operator": str(data.get("operator") or "ui"),
        }
    except Exception:
        return None


def is_manual_emergency_brake_active() -> bool:
    try:
        if os.environ.get("HERMES_AI_EMERGENCY_BRAKE_FLAG_PATH"):
            return _is_flag_file_active(_emergency_brake_flag_path())
        return _is_flag_file_active(_emergency_brake_flag_path()) or _is_flag_file_active(
            _spend_handbrake_flag_path()
        )
    except Exception:
        return False


def _day_cet(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=_STOCKHOLM_TZ).strftime("%Y-%m-%d")


def _day_start_epoch_cet(ts: float) -> float:
    dt = datetime.datetime.fromtimestamp(ts, tz=_STOCKHOLM_TZ)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=2.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=2000")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_call_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            day_cet TEXT NOT NULL,
            session_id TEXT NOT NULL,
            source TEXT NOT NULL,
            job_name TEXT DEFAULT '',
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            uncached_in INTEGER NOT NULL,
            cached_in INTEGER NOT NULL,
            out_tokens INTEGER NOT NULL,
            net_eur REAL NOT NULL,
            cluster_id TEXT DEFAULT '',
            cache_reset INTEGER DEFAULT 0,
            request_id TEXT UNIQUE,
            region TEXT DEFAULT '',
            service_tier TEXT DEFAULT ''
        )
        """
    )
    existing_cols = {row[1] for row in con.execute("PRAGMA table_info(llm_call_ledger)").fetchall()}
    if "region" not in existing_cols:
        con.execute("ALTER TABLE llm_call_ledger ADD COLUMN region TEXT DEFAULT ''")
    if "service_tier" not in existing_cols:
        con.execute("ALTER TABLE llm_call_ledger ADD COLUMN service_tier TEXT DEFAULT ''")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ledger_ts ON llm_call_ledger(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ledger_day ON llm_call_ledger(day_cet)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ledger_sess_ts ON llm_call_ledger(session_id, ts)")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS quarantined_sessions (
            session_id TEXT PRIMARY KEY,
            quarantined_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            reason TEXT NOT NULL,
            net_eur_window REAL NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS session_parent_lineage (
            child_session_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    return con


def extract_vertex_cluster_id(request_id: str | None) -> str:
    if not request_id or not isinstance(request_id, str):
        return ""
    rid = request_id.strip().removeprefix("chatcmpl-").removeprefix("vflex-")
    if len(rid) in (20, 22) and not rid.startswith("*"):
        try:
            pad = "=" * ((4 - len(rid) % 4) % 4)
            raw = base64.urlsafe_b64decode(rid + pad)
            if len(raw) in (15, 16):
                if raw[4] < 0x80 and len(raw) == 15:
                    return base64.urlsafe_b64encode(b"\x00\x00" + raw).decode("ascii")[10:16]
                if raw[4] >= 0x80 and raw[5] < 0x80 and (len(raw) <= 11 or raw[11] != 0x0f):
                    return base64.urlsafe_b64encode(b"\x00" + raw).decode("ascii")[10:16]
        except Exception:
            pass
    if len(rid) >= 16:
        return rid[10:16]
    return rid[:16]


def compute_call_net_eur(
    *,
    provider: str,
    model: str,
    prompt_tokens: int,
    uncached_input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float | None = None,
    cluster_id: str | None = None,
    region: str | None = None,
    service_tier: str | None = None,
    ts: float | None = None,
) -> float:
    prov = (provider or "").lower()
    mod = (model or "").lower()
    uncached = max(0, int(uncached_input_tokens or 0))
    cached = max(0, int(cache_read_tokens or 0))
    out = max(0, int(output_tokens or 0))
    cid = str(cluster_id or "")
    macro = cid[2:6] if len(cid) >= 6 else ""
    reg_norm = str(region or "").strip().lower()
    is_non_global_region = bool(reg_norm and reg_norm != "global")
    reg_mult = (
        VERTEX_EU_WEST2_REGIONAL_MULTIPLIER
        if (is_non_global_region or macro in _EU_WEST2_MACRO_CLUSTERS)
        else 1.0
    )
    tier_norm = str(service_tier or "").strip().lower()
    flex_mult = (
        VERTEX_FLEX_DISCOUNT_MULTIPLIER
        if tier_norm in ("flex", "on_demand_flex")
        else 1.0
    )

    if _is_gemini4_vertex_model(mod) and (prov in ("vertex", "", "google") or estimated_cost_usd is None):
        in_r, cache_r, out_r = resolve_vertex_rates(mod, ts=ts)
        return round(
            reg_mult
            * (uncached * in_r + cached * cache_r + out * out_r)
            / 1_000_000.0,
            6,
        )
    if "flash" in mod and ("gemini" in mod or prov in ("vertex", "google")):
        in_r, cache_r, out_r = resolve_vertex_rates(mod, ts=ts)
        return round(
            reg_mult
            * flex_mult
            * (uncached * in_r + cached * cache_r + out * out_r)
            / 1_000_000.0,
            6,
        )
    if estimated_cost_usd is not None and estimated_cost_usd > 0:
        return round(float(estimated_cost_usd) * USD_TO_EUR, 6)
    return 0.0


_HC_JOBS_CACHE: tuple[float, frozenset[str]] = (0.0, frozenset())


def _hc_monitored_job_names() -> frozenset[str]:
    """Auto-discover cron job names/IDs from ~/.hermes/cron/jobs.json that reference
    hc-ping.sh or hc.uropenn.se, cached by file mtime so no deadman-monitored job can
    ever drift out of Tier-0 protection."""
    global _HC_JOBS_CACHE
    try:
        jobs_path = Path.home() / ".hermes" / "cron" / "jobs.json"
        if not jobs_path.exists():
            return _HC_JOBS_CACHE[1]
        mtime = jobs_path.stat().st_mtime
        if mtime == _HC_JOBS_CACHE[0] and _HC_JOBS_CACHE[1]:
            return _HC_JOBS_CACHE[1]
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
        scripts_dir = Path.home() / ".hermes" / "scripts"
        names: set[str] = set()
        for j in data.get("jobs", []):
            if not str(j.get("prompt") or "").strip():
                continue
            blob = " ".join(str(j.get(k) or "") for k in ("prompt", "script", "monitor_script"))
            if "hc-ping.sh" not in blob and "hc.uropenn.se" not in blob:
                cand_files: set[Path] = set()
                for m in re.findall(r"(?:~/\.hermes/scripts/|/Users/emirsaffar/\.hermes/scripts/)([A-Za-z0-9_.-]+)", blob):
                    cand_files.add(scripts_dir / m)
                for sk in ("script", "monitor_script"):
                    sv = str(j.get(sk) or "").strip()
                    if not sv:
                        continue
                    for tok in sv.split():
                        if tok.startswith("-") or tok.endswith(("python", "python3", "python3.11", "bash", "sh")):
                            continue
                        cand_files.add(
                            Path(os.path.expanduser(tok)) if ("/" in tok or tok.startswith("~")) else (scripts_dir / tok)
                        )
                for sp in cand_files:
                    if sp.is_file():
                        try:
                            blob += " " + sp.read_text(encoding="utf-8", errors="ignore")
                        except Exception:
                            pass
            jname_low = str(j.get("name") or "").strip().lower()
            explicit_tier = str(j.get("spend_tier") if j.get("spend_tier") is not None else j.get("tier", "")).strip().lower()
            is_explicit_tier2 = explicit_tier in ("2", "tier2", "bulk") or j.get("critical") is False
            if "builder-owner" in jname_low or is_explicit_tier2:
                continue
            is_explicit_tier0 = (
                explicit_tier in ("0", "tier0", "critical")
                or j.get("critical") is True
            )
            is_prefix_tier0 = bool(
                jname_low and any(jname_low.startswith(p) or p in jname_low for p in _TIER0_CRITICAL_PREFIXES)
            )
            if is_explicit_tier0 or "hc-ping.sh" in blob or "hc.uropenn.se" in blob or is_prefix_tier0:
                for key in ("name", "id"):
                    val = str(j.get(key) or "").strip().lower()
                    if val:
                        names.add(val)
                        if key == "id":
                            names.add(f"cron_{val}")
        _HC_JOBS_CACHE = (mtime, frozenset(names))
        return _HC_JOBS_CACHE[1]
    except Exception:
        return _HC_JOBS_CACHE[1]


def is_tier0_critical_cron(job_name: str | None, session_id: str | None = None) -> bool:
    ctx_name = ""
    if not job_name:
        try:
            from cron.execution_identity import current_cron_execution
            ctx = current_cron_execution()
            if isinstance(ctx, dict):
                ctx_name = str(ctx.get("job_name") or "")
            elif ctx is not None:
                ctx_name = str(getattr(ctx, "job_name", "") or "")
        except Exception:
            pass
    hc_names = _hc_monitored_job_names()
    for candidate in (job_name or "", ctx_name, session_id or ""):
        low = candidate.strip().lower()
        if not low:
            continue
        if "builder-owner" in low:
            return False
        if low in hc_names:
            return True
        if any(low.startswith(p) or p in low for p in _TIER0_CRITICAL_PREFIXES):
            return True
    return False


def record_realtime_llm_call(
    *,
    session_id: str,
    source: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    uncached_input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    request_id: str | None = None,
    job_name: str = "",
    estimated_cost_usd: float | None = None,
    region: str | None = None,
    service_tier: str | None = None,
    ts: float | None = None,
) -> dict[str, Any]:
    now = float(ts if ts is not None else time.time())
    day_cet = _day_cet(now)
    uncached = max(0, int(uncached_input_tokens or 0))
    cached = max(0, int(cache_read_tokens or 0))
    out = max(0, int(output_tokens or 0))
    prompt = int(prompt_tokens or (uncached + cached))
    cluster_id = extract_vertex_cluster_id(request_id)
    effective_tier = service_tier or ("flex" if isinstance(request_id, str) and request_id.startswith("vflex-") else None)
    is_live_call = (
        str(source) != "log_tail"
        and not str(source).startswith("aux:")
        and str(source) != "background_review"
        and not str(session_id).startswith("log_tail")
    )
    is_argon_live = (
        is_live_call
        and prompt > 55_000
        and _is_gemini4_vertex_model(model)
    )
    cache_reset = (
        1
        if (is_argon_live and prompt > 110_000 and cached < 42_000 and uncached > 70_000)
        else 0
    )
    net_eur = compute_call_net_eur(
        provider=provider,
        model=model,
        prompt_tokens=prompt,
        uncached_input_tokens=uncached,
        cache_read_tokens=cached,
        output_tokens=out,
        estimated_cost_usd=estimated_cost_usd,
        cluster_id=cluster_id,
        region=region,
        service_tier=effective_tier,
        ts=now,
    )
    req_key = request_id or f"{session_id}:{now:.6f}:{prompt}:{out}"

    row_info = {
        "ts": now,
        "day_cet": day_cet,
        "session_id": session_id or "unknown",
        "source": source or "unknown",
        "job_name": job_name or "",
        "provider": provider or "unknown",
        "model": model or "unknown",
        "prompt_tokens": prompt,
        "uncached_in": uncached,
        "cached_in": cached,
        "out_tokens": out,
        "net_eur": net_eur,
        "cluster_id": cluster_id,
        "cache_reset": cache_reset,
        "request_id": req_key,
        "region": str(region or "").strip().lower(),
        "service_tier": str(effective_tier or "").strip().lower(),
    }
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("HERMES_AI_SPEND_DB_PATH"):
        return row_info
    try:
        con = _connect()
        try:
            if is_live_call and not cache_reset and (
                is_argon_live or (prompt >= 45_000 and cached == 0 and uncached >= 45_000)
            ):
                prev_rows = con.execute(
                    """
                    SELECT prompt_tokens, cached_in
                    FROM llm_call_ledger
                    WHERE session_id = ? AND ts >= ? AND request_id != ?
                      AND source NOT LIKE 'aux:%' AND source != 'background_review'
                    ORDER BY ts DESC, id DESC LIMIT 2
                    """,
                    (row_info["session_id"], now - 1800.0, req_key),
                ).fetchall()
                if prev_rows:
                    prev_prompt, prev_cached = int(prev_rows[0][0] or 0), int(prev_rows[0][1] or 0)
                    if (
                        is_argon_live
                        and (
                            (
                                prev_cached >= 42_000
                                and cached < 42_000
                                and prompt >= prev_prompt * 0.85
                            )
                            or (
                                prompt >= prev_prompt
                                and prev_cached >= 35_000
                                and (prev_cached - cached) >= 16_384
                                and cached < prompt * 0.50
                            )
                        )
                    ) or (
                        len(prev_rows) >= 2
                        and cached < 10_000
                        and uncached >= 45_000
                        and all(
                            int(r[0] or 0) >= 40_000 and int(r[1] or 0) < 10_000
                            for r in prev_rows
                        )
                    ):
                        cache_reset = 1
                        row_info["cache_reset"] = 1
            with con:
                con.execute(
                    """
                    INSERT INTO llm_call_ledger (
                        ts, day_cet, session_id, source, job_name, provider, model,
                        prompt_tokens, uncached_in, cached_in, out_tokens, net_eur,
                        cluster_id, cache_reset, request_id, region, service_tier
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(request_id) DO UPDATE SET
                        session_id = CASE WHEN llm_call_ledger.source = 'log_tail' AND excluded.source != 'log_tail' THEN excluded.session_id ELSE llm_call_ledger.session_id END,
                        source = CASE WHEN llm_call_ledger.source = 'log_tail' AND excluded.source != 'log_tail' THEN excluded.source ELSE llm_call_ledger.source END,
                        job_name = CASE WHEN llm_call_ledger.job_name = '' AND excluded.job_name != '' THEN excluded.job_name ELSE llm_call_ledger.job_name END,
                        out_tokens = MAX(llm_call_ledger.out_tokens, excluded.out_tokens),
                        net_eur = MAX(llm_call_ledger.net_eur, excluded.net_eur),
                        region = CASE WHEN llm_call_ledger.region = '' AND excluded.region != '' THEN excluded.region ELSE llm_call_ledger.region END,
                        service_tier = CASE WHEN llm_call_ledger.service_tier = '' AND excluded.service_tier != '' THEN excluded.service_tier ELSE llm_call_ledger.service_tier END
                    """,
                    (
                        now,
                        day_cet,
                        row_info["session_id"],
                        row_info["source"],
                        row_info["job_name"],
                        row_info["provider"],
                        row_info["model"],
                        prompt,
                        uncached,
                        cached,
                        out,
                        net_eur,
                        cluster_id,
                        cache_reset,
                        req_key,
                        row_info["region"],
                        row_info["service_tier"],
                    ),
                )
        finally:
            con.close()
        global _LAST_STATUS_WRITE_TS
        if now - _LAST_STATUS_WRITE_TS >= 10.0:
            _LAST_STATUS_WRITE_TS = now
            write_realtime_status_snapshot(now=now)
    except Exception as exc:
        logger.debug("record_realtime_llm_call failed open: %s", exc)
    return row_info


def _sum_tier2_non_tier0_spend(cur: sqlite3.Cursor, day_cet: str, t_15m: float) -> tuple[float, float]:
    cur.execute(
        """
        SELECT source, job_name, session_id, day_cet, ts, COALESCE(net_eur, 0.0)
        FROM llm_call_ledger
        WHERE (day_cet = ? OR ts >= ?)
          AND (source = 'cron' OR (source = 'background_review' AND (session_id LIKE 'local-%' OR session_id LIKE 'cron%')))
        """,
        (day_cet, t_15m),
    )
    eur_today = 0.0
    eur_15m = 0.0
    for r_src, r_job, r_sid, r_day, r_ts, r_eur in cur.fetchall():
        if r_src == "cron" and is_tier0_critical_cron(r_job, r_sid):
            continue
        val = float(r_eur or 0.0)
        if str(r_day or "") == day_cet:
            eur_today += val
        if float(r_ts or 0.0) >= t_15m:
            eur_15m += val
    return eur_today, eur_15m


def get_realtime_spend_summary(now: float | None = None) -> dict[str, Any]:
    ts_now = float(now if now is not None else time.time())
    day_cet = _day_cet(ts_now)
    t_15m = ts_now - 900.0
    t_1h = ts_now - 3600.0
    try:
        con = _connect()
        try:
            cur = con.cursor()
            cur.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(net_eur), 0.0),
                       COALESCE(SUM(CASE WHEN source = 'cron' OR (source = 'background_review' AND (session_id LIKE 'local-%' OR session_id LIKE 'cron%')) THEN net_eur ELSE 0.0 END), 0.0),
                       COALESCE(SUM(uncached_in), 0), COALESCE(SUM(cached_in), 0),
                       COALESCE(SUM(cache_reset), 0)
                FROM llm_call_ledger WHERE ts >= ? AND source != 'log_tail'
                """,
                (t_15m,),
            )
            c15, eur15, _eur15_cron_bg, u15, ca15, cr15_total = cur.fetchone()

            cur.execute(
                """
                SELECT COALESCE(MAX(s_cr), 0)
                FROM (
                    SELECT COALESCE(SUM(cache_reset), 0) AS s_cr
                    FROM llm_call_ledger
                    WHERE ts >= ? AND source != 'log_tail'
                    GROUP BY session_id
                )
                """,
                (t_15m,),
            )
            cr15 = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(net_eur), 0.0),
                       COALESCE(SUM(uncached_in), 0), COALESCE(SUM(cached_in), 0),
                       COALESCE(SUM(cache_reset), 0)
                FROM llm_call_ledger WHERE ts >= ? AND source != 'log_tail'
                """,
                (t_1h,),
            )
            c1h, eur1h, u1h, ca1h, cr1h = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(net_eur), 0.0),
                       COALESCE(SUM(CASE WHEN source NOT IN ('desktop', 'mobile', 'tui', 'tui_gateway', 'gui', 'web', 'cli', 'interactive', 'log_tail') THEN net_eur ELSE 0.0 END), 0.0),
                       COALESCE(SUM(CASE WHEN source = 'cron' OR (source = 'background_review' AND (session_id LIKE 'local-%' OR session_id LIKE 'cron%')) THEN net_eur ELSE 0.0 END), 0.0),
                       COALESCE(SUM(uncached_in), 0), COALESCE(SUM(cached_in), 0),
                       COALESCE(SUM(cache_reset), 0)
                FROM llm_call_ledger WHERE day_cet = ? AND source != 'log_tail'
                """,
                (day_cet,),
            )
            cday, eurday, eurday_non_int, eurday_cron_bg, uday, caday, crday = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(net_eur), 0.0),
                       COALESCE(SUM(uncached_in), 0), COALESCE(SUM(cached_in), 0)
                FROM llm_call_ledger WHERE day_cet = ?
                """,
                (day_cet,),
            )
            cday_all, eurday_all, uday_all, caday_all = cur.fetchone()

            cur.execute(
                "SELECT COUNT(*) FROM quarantined_sessions WHERE expires_at > ?",
                (ts_now,),
            )
            q_count = cur.fetchone()[0]
            tier2_today_f, tier2_15m_f = _sum_tier2_non_tier0_spend(cur, day_cet, t_15m)
        finally:
            con.close()
    except Exception as exc:
        logger.debug("get_realtime_spend_summary failed open: %s", exc)
        return {
            "ts": ts_now,
            "day_cet": day_cet,
            "calls_15m": 0,
            "net_eur_15m": 0.0,
            "cache_hit_pct_15m": 100.0,
            "cache_resets_15m": 0,
            "calls_1h": 0,
            "net_eur_1h": 0.0,
            "cache_hit_pct_1h": 100.0,
            "cache_resets_1h": 0,
            "calls_today": 0,
            "net_eur_today": 0.0,
            "calls_today_all": 0,
            "net_eur_today_all": 0.0,
            "cache_hit_pct_today_all": 100.0,
            "net_eur_today_non_interactive": 0.0,
            "net_eur_today_tier2_pool": 0.0,
            "cache_hit_pct_today": 100.0,
            "cache_resets_today": 0,
            "active_quarantined_sessions": 0,
            "may_2027_daily_target_eur": MAY_2027_DAILY_TARGET_EUR,
        }

    hit15 = round(100.0 * ca15 / max(1, u15 + ca15), 2) if (u15 + ca15) > 0 else 100.0
    hit1h = round(100.0 * ca1h / max(1, u1h + ca1h), 2) if (u1h + ca1h) > 0 else 100.0
    hitday = round(100.0 * caday / max(1, uday + caday), 2) if (uday + caday) > 0 else 100.0
    hitday_all = round(100.0 * caday_all / max(1, uday_all + caday_all), 2) if (uday_all + caday_all) > 0 else 100.0
    manual_brake = is_manual_emergency_brake_active()
    eurday_f = float(eurday or 0.0)
    eur15_f = float(eur15 or 0.0)
    eurday_cron_bg_f = float(eurday_cron_bg or 0.0)
    q_int = int(q_count or 0)
    active_cap = get_active_budget_cap(ts_now)
    eff_tier3_hard_eur = (
        float(active_cap.get("tier3_daily_hard_cap_eur") or TIER3_DAILY_HARD_CAP_EUR)
        if active_cap is not None
        else TIER3_DAILY_HARD_CAP_EUR
    )
    eff_tier2_soft_eur = (
        float(active_cap.get("tier2_daily_soft_cap_eur") or TIER2_DAILY_SOFT_CAP_EUR)
        if active_cap is not None
        else TIER2_DAILY_SOFT_CAP_EUR
    )
    eff_tier2_burst_15m_eur = (
        float(active_cap.get("tier2_estate_15m_burst_eur") or TIER2_ESTATE_15M_BURST_EUR)
        if active_cap is not None
        else TIER2_ESTATE_15M_BURST_EUR
    )
    if manual_brake:
        breaker_state = "MANUAL_EMERGENCY_BRAKE"
    elif eurday_cron_bg_f >= eff_tier3_hard_eur:
        breaker_state = "TIER3_DAILY_HARD_CAP"
    elif tier2_today_f >= eff_tier2_soft_eur or tier2_15m_f >= eff_tier2_burst_15m_eur:
        breaker_state = "TIER2_DAILY_SOFT_CAP"
    elif q_int > 0:
        breaker_state = "TIER1_SESSION_QUARANTINED"
    else:
        breaker_state = "ARMED_OK"

    return {
        "ts": ts_now,
        "day_cet": day_cet,
        "breaker_state": breaker_state,
        "manual_emergency_brake_active": manual_brake,
        "calls_15m": int(c15 or 0),
        "net_eur_15m": round(eur15_f, 4),
        "cache_hit_pct_15m": hit15,
        "cache_resets_15m": int(cr15 or 0),
        "cache_resets_15m_total": int(cr15_total or 0),
        "calls_1h": int(c1h or 0),
        "net_eur_1h": round(float(eur1h or 0.0), 4),
        "cache_hit_pct_1h": hit1h,
        "cache_resets_1h": int(cr1h or 0),
        "calls_today": int(cday or 0),
        "net_eur_today": round(eurday_f, 4),
        "calls_today_all": int(cday_all or 0),
        "net_eur_today_all": round(float(eurday_all or 0.0), 4),
        "cache_hit_pct_today_all": hitday_all,
        "net_eur_today_non_interactive": round(float(eurday_non_int or 0.0), 4),
        "net_eur_today_tier2_pool": round(tier2_today_f, 4),
        "cache_hit_pct_today": hitday,
        "cache_resets_today": int(crday or 0),
        "active_quarantined_sessions": q_int,
        "may_2027_daily_target_eur": MAY_2027_DAILY_TARGET_EUR,
    }


_PRESERVED_WATCHDOG_KEYS: tuple[str, ...] = (
    "bq_mtd",
    "parity",
    "promo_expiry_guard",
    "log_tail_ingest",
    "openrouter_credits_remaining_usd",
    "may_2027_sparplan_targets",
    "gcp_anomaly_detector_credit_treatment_audit",
    "gcp_budgets_sync",
    "ha_60s_sync",
)


def write_realtime_status_snapshot(now: float | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = get_realtime_spend_summary(now=now)
    spath = _status_path()
    if extra is None and spath.exists():
        try:
            prev = json.loads(spath.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                for k in _PRESERVED_WATCHDOG_KEYS:
                    if k in prev and k not in summary:
                        summary[k] = prev[k]
        except Exception:
            pass
    if extra:
        summary.update(extra)
    try:
        spath.parent.mkdir(parents=True, exist_ok=True)
        tmp = spath.with_suffix(".tmp")
        tmp.write_text(json.dumps(summary, indent=2, sort_keys=True))
        tmp.replace(spath)
    except Exception as exc:
        logger.debug("write_realtime_status_snapshot failed open: %s", exc)
    return summary


def quarantine_session(session_id: str, reason: str, net_eur_window: float, ttl_s: float = 3600.0, now: float | None = None) -> None:
    ts_now = float(now if now is not None else time.time())
    try:
        con = _connect()
        try:
            with con:
                con.execute(
                    """
                    INSERT OR REPLACE INTO quarantined_sessions (
                        session_id, quarantined_at, expires_at, reason, net_eur_window
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_id, ts_now, ts_now + ttl_s, reason, float(net_eur_window)),
                )
        finally:
            con.close()
    except Exception as exc:
        logger.debug("quarantine_session failed open: %s", exc)


def evaluate_realtime_spend_breaker(
    *,
    session_id: str,
    source: str,
    model: str = "",
    provider: str = "",
    job_name: str = "",
    making_productive_progress: bool = True,
    consecutive_calls_since_user: int = 0,
    parent_session_id: str = "",
    parent_source: str = "",
    lineage_session_ids: tuple[str, ...] | list[str] | set[str] = (),
    now: float | None = None,
) -> dict[str, Any]:
    ts_now = float(now if now is not None else time.time())
    day_cet = _day_cet(ts_now)
    src = (source or "unknown").strip().lower()
    p_src = (parent_source or "").strip().lower()
    p_sid = (parent_session_id or "").strip()
    _interactive_surfaces = ("desktop", "mobile", "tui", "tui_gateway", "gui", "web", "cli", "interactive")
    is_interactive = src in _interactive_surfaces
    is_interactive_subagent = (
        src == "subagent"
        and p_src in ("desktop", "mobile", "tui", "tui_gateway", "gui", "web")
        and bool(p_sid)
    )
    is_tier0 = is_tier0_critical_cron(job_name, session_id)
    is_bg_review = src == "background_review"
    cron_job_tag = str(job_name or "").strip() if src == "cron" else ""
    effective_session_id = f"background_review:{session_id}" if is_bg_review else session_id
    all_lineage_sids = (
        (effective_session_id,)
        if is_bg_review
        else _resolve_lineage_session_ids(
            session_id,
            p_sid,
            tuple(lineage_session_ids) + ((f"cron_job:{cron_job_tag}",) if cron_job_tag else ()),
        )
    )

    try:
        con = _connect()
        try:
            cur = con.cursor()
            if src == "subagent" and p_sid and not p_src:
                cur.execute(
                    """
                    SELECT source FROM llm_call_ledger
                    WHERE session_id = ? AND source NOT IN ('log_tail', 'background_review', 'subagent')
                    ORDER BY ts DESC LIMIT 1
                    """,
                    (p_sid,),
                )
                p_row = cur.fetchone()
                if p_row and p_row[0]:
                    p_src = str(p_row[0]).strip().lower()
            is_interactive_subagent = (
                src == "subagent"
                and p_src in ("desktop", "mobile", "tui", "tui_gateway", "gui", "web")
                and bool(p_sid)
            )
            family_job_tag = (
                f"parent:{p_sid}"
                if (src == "subagent" and p_sid)
                else cron_job_tag
            )
            spend_sids = (
                (session_id,)
                if (is_interactive_subagent or is_bg_review)
                else all_lineage_sids
            )

            # 1) Check explicit quarantine table (including full parent/compaction lineage)
            q_placeholders = ",".join("?" for _ in all_lineage_sids)
            cur.execute(
                f"SELECT reason, net_eur_window FROM quarantined_sessions WHERE session_id IN ({q_placeholders}) AND expires_at > ? ORDER BY expires_at DESC LIMIT 1",
                (*all_lineage_sids, ts_now),
            )
            q_row = cur.fetchone()
            # 2) Per-session (and subagent-family / compression-rotation lineage) 15m and 1h spend + cache resets
            s_placeholders = ",".join("?" for _ in spend_sids)
            if is_bg_review:
                cur.execute(
                    f"""
                    SELECT COALESCE(SUM(net_eur), 0.0), COALESCE(SUM(cache_reset), 0), COUNT(*)
                    FROM llm_call_ledger
                    WHERE session_id IN ({s_placeholders}) AND source = 'background_review' AND ts >= ?
                    """,
                    (*spend_sids, ts_now - 900.0),
                )
            elif family_job_tag:
                cur.execute(
                    f"""
                    SELECT COALESCE(SUM(net_eur), 0.0),
                           COALESCE(SUM(CASE WHEN session_id IN ({s_placeholders}) THEN cache_reset ELSE 0 END), 0),
                           COUNT(*)
                    FROM llm_call_ledger
                    WHERE (session_id IN ({s_placeholders}) OR job_name = ?) AND source != 'background_review' AND ts >= ?
                    """,
                    (
                        *spend_sids,
                        *spend_sids,
                        family_job_tag,
                        ts_now - 900.0,
                    ),
                )
            else:
                cur.execute(
                    f"""
                    SELECT COALESCE(SUM(net_eur), 0.0), COALESCE(SUM(cache_reset), 0), COUNT(*)
                    FROM llm_call_ledger WHERE session_id IN ({s_placeholders}) AND source != 'background_review' AND ts >= ?
                    """,
                    (*spend_sids, ts_now - 900.0),
                )
            s_eur_15m, s_resets_15m, s_calls_15m = cur.fetchone()
            s_eur_15m = float(s_eur_15m or 0.0)
            s_resets_15m = int(s_resets_15m or 0)
            s_calls_15m = int(s_calls_15m or 0)

            if is_bg_review:
                cur.execute(
                    f"SELECT COALESCE(SUM(net_eur), 0.0), COUNT(*) FROM llm_call_ledger WHERE session_id IN ({s_placeholders}) AND source = 'background_review' AND ts >= ?",
                    (*spend_sids, ts_now - 3600.0),
                )
            elif family_job_tag:
                cur.execute(
                    f"SELECT COALESCE(SUM(net_eur), 0.0), COUNT(*) FROM llm_call_ledger WHERE (session_id IN ({s_placeholders}) OR job_name = ?) AND source != 'background_review' AND ts >= ?",
                    (*spend_sids, family_job_tag, ts_now - 3600.0),
                )
            else:
                cur.execute(
                    f"SELECT COALESCE(SUM(net_eur), 0.0), COUNT(*) FROM llm_call_ledger WHERE session_id IN ({s_placeholders}) AND source != 'background_review' AND ts >= ?",
                    (*spend_sids, ts_now - 3600.0),
                )
            r_1h_row = cur.fetchone()
            s_eur_1h = float(r_1h_row[0] or 0.0)
            s_calls_1h = int(r_1h_row[1] or 0)

            # 3) Estate 15m and Today spend (plus non-interactive pool so interactive spikes do not starve crons)
            cur.execute(
                "SELECT COALESCE(SUM(net_eur), 0.0) FROM llm_call_ledger WHERE ts >= ? AND source != 'log_tail'",
                (ts_now - 900.0,),
            )
            estate_15m = float(cur.fetchone()[0] or 0.0)

            cur.execute(
                "SELECT COALESCE(SUM(net_eur), 0.0) FROM llm_call_ledger WHERE day_cet = ? AND source != 'log_tail'",
                (day_cet,),
            )
            estate_today = float(cur.fetchone()[0] or 0.0)

            _ni_clause = (
                "source IN ('cron', 'background_review')"
                if src == "background_review"
                else "(source = 'cron' OR (source = 'background_review' AND (session_id LIKE 'local-%' OR session_id LIKE 'cron%')))"
            )
            cur.execute(
                f"SELECT COALESCE(SUM(net_eur), 0.0) FROM llm_call_ledger WHERE ts >= ? AND {_ni_clause}",
                (ts_now - 900.0,),
            )
            non_interactive_15m = float(cur.fetchone()[0] or 0.0)

            cur.execute(
                f"SELECT COALESCE(SUM(net_eur), 0.0) FROM llm_call_ledger WHERE day_cet = ? AND {_ni_clause}",
                (day_cet,),
            )
            non_interactive_today = float(cur.fetchone()[0] or 0.0)
            tier2_non_tier0_today, tier2_non_tier0_15m = _sum_tier2_non_tier0_spend(cur, day_cet, ts_now - 900.0)
        finally:
            con.close()
    except Exception as exc:
        logger.debug("evaluate_realtime_spend_breaker failed open: %s", exc)
        return {"allowed": True, "reason": "fail_open_db_error"}

    # 0A) Explicit UI Quarantine / Pause / Throttle & Active Quarantine Check
    if q_row is not None:
        q_reason = str(q_row[0] or "")
        q_net_eur = float(q_row[1] or 0.0)
        if q_reason.startswith("manual_ui_pause:") or "Pausad manuellt" in q_reason:
            return {
                "allowed": False,
                "reason": f"quarantined:{q_reason}",
                "session_net_eur_15m": max(s_eur_15m, q_net_eur),
            }
        if q_reason.startswith("manual_ui_throttle:") or "Strypt manuellt" in q_reason:
            if s_eur_15m >= 1.50 or s_eur_1h >= 4.00 or s_resets_15m >= 1:
                return {
                    "allowed": False,
                    "reason": f"throttled:{q_reason} (€{s_eur_15m:.2f}/15m, €{s_eur_1h:.2f}/1h)",
                    "session_net_eur_15m": s_eur_15m,
                }
        elif not is_interactive or (
            not making_productive_progress
            and int(consecutive_calls_since_user or 0) > 2
        ) or (
            q_reason.startswith("tier1_runaway_autonomous_turn")
            and int(consecutive_calls_since_user or 0) >= 6
        ):
            return {
                "allowed": False,
                "reason": f"quarantined:{q_reason}",
                "session_net_eur_15m": max(s_eur_15m, q_net_eur),
            }

    # 0B) Manual UI Hourly Spend Cap (e.g. $15/h -> €13.50/h -> €3.38/15m via ai-budget-cap.json)
    active_cap = get_active_budget_cap(ts_now)
    if active_cap is not None and active_cap.get("has_hourly_cap", True) and not is_tier0:
        cap_15m_eur = float(active_cap["cap_eur_15m"])
        cap_usd_hr = float(active_cap["cap_usd_hr"])
        if not is_interactive:
            if estate_15m >= cap_15m_eur:
                return {
                    "allowed": False,
                    "reason": (
                        f"manual_budget_cap_15m (estate €{estate_15m:.2f} >= €{cap_15m_eur:.2f} "
                        f"under ${cap_usd_hr:.0f}/h UI cap)"
                    ),
                    "session_net_eur_15m": s_eur_15m,
                    "estate_net_eur_15m": estate_15m,
                }
        elif (
            int(consecutive_calls_since_user or 0) >= 6
            and s_eur_15m >= max(2.00, round(cap_15m_eur * 0.75, 2))
        ):
            return {
                "allowed": False,
                "reason": (
                    f"manual_budget_cap_interactive_throttle (calls={consecutive_calls_since_user}, "
                    f"session €{s_eur_15m:.2f}/15m under ${cap_usd_hr:.0f}/h UI cap)"
                ),
                "session_net_eur_15m": s_eur_15m,
                "estate_net_eur_15m": estate_15m,
            }

    # Tier 1A: Non-interactive per-session (and subagent-family / cron-job-family) runaway breaker
    if not is_interactive:
        cap_15m = TIER0_CRITICAL_CRON_15M_SAFETY_CAP_EUR if is_tier0 else TIER1_NON_INTERACTIVE_15M_CAP_EUR
        if s_eur_15m >= cap_15m:
            reason = f"tier1_session_15m_cap (€{s_eur_15m:.2f} >= €{cap_15m:.2f})"
            quarantine_session(effective_session_id, reason, s_eur_15m, ttl_s=1800.0, now=ts_now)
            if p_sid and src == "subagent" and not is_interactive_subagent:
                quarantine_session(p_sid, reason, s_eur_15m, ttl_s=1800.0, now=ts_now)
            if cron_job_tag:
                quarantine_session(f"cron_job:{cron_job_tag}", reason, s_eur_15m, ttl_s=1800.0, now=ts_now)
            return {"allowed": False, "reason": reason, "session_net_eur_15m": s_eur_15m}
        if not is_tier0 and s_eur_1h >= TIER1_NON_INTERACTIVE_1H_CAP_EUR:
            reason = f"tier1_session_1h_cap (€{s_eur_1h:.2f} >= €{TIER1_NON_INTERACTIVE_1H_CAP_EUR:.2f})"
            quarantine_session(effective_session_id, reason, s_eur_1h, ttl_s=3600.0, now=ts_now)
            if p_sid and src == "subagent" and not is_interactive_subagent:
                quarantine_session(p_sid, reason, s_eur_1h, ttl_s=3600.0, now=ts_now)
            if cron_job_tag:
                quarantine_session(f"cron_job:{cron_job_tag}", reason, s_eur_1h, ttl_s=3600.0, now=ts_now)
            return {"allowed": False, "reason": reason, "session_net_eur_1h": s_eur_1h}
        reset_cap = (
            TIER0_CRITICAL_CACHE_RESET_COUNT
            if is_tier0
            else TIER1_NON_INTERACTIVE_CACHE_RESET_COUNT
        )
        reset_min_eur = (
            TIER0_CRITICAL_CACHE_RESET_MIN_EUR
            if is_tier0
            else TIER1_NON_INTERACTIVE_CACHE_RESET_MIN_EUR
        )
        if (
            s_resets_15m >= reset_cap
            and s_eur_15m >= reset_min_eur
        ):
            reason = f"tier1_cache_reset_storm (resets={s_resets_15m}, €{s_eur_15m:.2f} in 15m)"
            quarantine_session(effective_session_id, reason, s_eur_15m, ttl_s=1800.0, now=ts_now)
            if p_sid and src == "subagent" and not is_interactive_subagent:
                quarantine_session(p_sid, reason, s_eur_15m, ttl_s=1800.0, now=ts_now)
            return {"allowed": False, "reason": reason, "session_net_eur_15m": s_eur_15m}

    # Tier 1B: Progress-aware Desktop stalled loop breaker + hard single-turn / compaction-chain runaway ceiling
    if is_interactive:
        compaction_hops = max(0, len(all_lineage_sids) - 1)
        effective_runaway_calls = (
            max(int(consecutive_calls_since_user or 0), s_calls_15m)
            if (int(consecutive_calls_since_user or 0) >= 6 and compaction_hops >= 1)
            else int(consecutive_calls_since_user or 0)
        )
        if (
            not making_productive_progress
            and effective_runaway_calls >= TIER1_STALLED_DESKTOP_MIN_CALLS
            and s_eur_15m >= TIER1_STALLED_DESKTOP_15M_CAP_EUR
        ):
            reason = (
                f"tier1_stalled_desktop_loop (calls={effective_runaway_calls}, "
                f"€{s_eur_15m:.2f} in 15m with no productive progress)"
            )
            for l_sid in all_lineage_sids:
                quarantine_session(l_sid, reason, s_eur_15m, ttl_s=900.0, now=ts_now)
            return {"allowed": False, "reason": reason, "session_net_eur_15m": s_eur_15m}
        if (
            effective_runaway_calls >= TIER1_RUNAWAY_DESKTOP_TURN_MAX_CALLS
            and s_eur_15m >= TIER1_RUNAWAY_DESKTOP_15M_CAP_EUR
        ):
            reason = (
                f"tier1_runaway_autonomous_turn (calls={effective_runaway_calls} without user input, "
                f"€{s_eur_15m:.2f} in 15m across {len(all_lineage_sids)} lineage session(s))"
            )
            for l_sid in all_lineage_sids:
                quarantine_session(l_sid, reason, s_eur_15m, ttl_s=900.0, now=ts_now)
            return {"allowed": False, "reason": reason, "session_net_eur_15m": s_eur_15m}
        if (
            compaction_hops >= 2
            and int(consecutive_calls_since_user or 0) >= 6
            and s_calls_1h >= TIER1_RUNAWAY_DESKTOP_TURN_MAX_CALLS
            and s_eur_1h >= TIER1_RUNAWAY_DESKTOP_1H_CAP_EUR
        ):
            reason = (
                f"tier1_runaway_autonomous_turn (calls_1h={s_calls_1h} across {len(all_lineage_sids)} compacted sessions, "
                f"€{s_eur_1h:.2f} in 1h >= €{TIER1_RUNAWAY_DESKTOP_1H_CAP_EUR:.2f})"
            )
            for l_sid in all_lineage_sids:
                quarantine_session(l_sid, reason, s_eur_1h, ttl_s=900.0, now=ts_now)
            return {"allowed": False, "reason": reason, "session_net_eur_15m": s_eur_15m, "session_net_eur_1h": s_eur_1h}
        # Interactive Desktop sessions with productive progress are NEVER blocked by Tier 2 or Tier 3
        return {
            "allowed": True,
            "reason": "interactive_allowed",
            "session_net_eur_15m": s_eur_15m,
            "estate_net_eur_today": estate_today,
        }

    # Tier-0 critical operational crons are exempt from Tier 2 & Tier 3 estate caps
    if is_tier0:
        return {
            "allowed": True,
            "reason": "tier0_critical_cron_exempt",
            "session_net_eur_15m": s_eur_15m,
            "estate_net_eur_today": estate_today,
        }

    # Manual Home Assistant Emergency Brake Override blocks all non-Tier-0 non-interactive sessions
    if is_manual_emergency_brake_active():
        return {
            "allowed": False,
            "reason": "manual_ha_emergency_brake (Home Assistant Emergency Brake switch is ON)",
            "estate_net_eur_today": estate_today,
        }

    # Bounded subagents spawned by an active interactive Desktop/Mobile/TUI parent
    # (already vetted by Tier 1A per-session and family €4/15m & €10/1h caps above)
    # are allowed so the 190k-token parent does not have to run child work inline.
    # Note: on iteration 0 (before the first LLM/tool round), ToolGuardrailController
    # has _turn_productive_calls == 0 so making_productive_progress is False; allow
    # initial turn entry (consecutive_calls_since_user <= 1) while blocking stalled loops.
    if is_interactive_subagent and (
        making_productive_progress
        or int(consecutive_calls_since_user or 0) <= 1
    ):
        return {
            "allowed": True,
            "reason": "interactive_subagent_allowed",
            "session_net_eur_15m": s_eur_15m,
            "estate_net_eur_today": estate_today,
        }

    # Tier 3: Daily Hard Cap (€195/day net default, overridable via ai-budget-cap.json)
    # - cron and background_review evaluate against non_interactive_today so interactive Desktop spikes never starve crons.
    # - non-exempt subagents (unparented/non-Tier-0 cron subagents or stalled Desktop subagents that fell past line 825)
    #   evaluate against max(non_interactive_today, estate_today).
    eff_tier3_hard_eur = (
        float(active_cap.get("tier3_daily_hard_cap_eur") or TIER3_DAILY_HARD_CAP_EUR)
        if active_cap is not None
        else TIER3_DAILY_HARD_CAP_EUR
    )
    eff_tier2_soft_eur = (
        float(active_cap.get("tier2_daily_soft_cap_eur") or TIER2_DAILY_SOFT_CAP_EUR)
        if active_cap is not None
        else TIER2_DAILY_SOFT_CAP_EUR
    )
    eff_tier2_burst_15m_eur = (
        float(active_cap.get("tier2_estate_15m_burst_eur") or TIER2_ESTATE_15M_BURST_EUR)
        if active_cap is not None
        else TIER2_ESTATE_15M_BURST_EUR
    )
    tier3_pool_today = (
        non_interactive_today
        if src in ("cron", "background_review")
        else max(non_interactive_today, estate_today)
    )
    if tier3_pool_today >= eff_tier3_hard_eur:
        return {
            "allowed": False,
            "reason": f"tier3_daily_hard_cap (pool today €{tier3_pool_today:.2f} >= €{eff_tier3_hard_eur:.2f})",
            "estate_net_eur_today": estate_today,
            "non_interactive_net_eur_today": non_interactive_today,
        }

    # Tier 2: Daily Soft Cap (€150/day net default) or 15m Burst (€8/15m default on non-Tier-0 pool) suppresses background_review and non-Tier-0 crons
    tier2_pool_today = non_interactive_today if src == "background_review" else tier2_non_tier0_today
    tier2_pool_15m = non_interactive_15m if src == "background_review" else tier2_non_tier0_15m
    if src in ("background_review", "cron") and (
        tier2_pool_today >= eff_tier2_soft_eur or tier2_pool_15m >= eff_tier2_burst_15m_eur
    ):
        return {
            "allowed": False,
            "reason": (
                f"tier2_daily_soft_cap (non-interactive today €{tier2_pool_today:.2f} / 15m €{tier2_pool_15m:.2f})"
            ),
            "estate_net_eur_today": estate_today,
            "non_interactive_net_eur_today": non_interactive_today,
            "tier2_pool_net_eur_today": tier2_pool_today,
        }

    return {
        "allowed": True,
        "reason": "ok",
        "session_net_eur_15m": s_eur_15m,
        "estate_net_eur_15m": estate_15m,
        "estate_net_eur_today": estate_today,
    }
