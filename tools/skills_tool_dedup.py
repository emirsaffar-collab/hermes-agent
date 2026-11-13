"""skill_view repeat-view dedup registry: per-task cache of (resolved skill name, file_path) ->
digest of the skill_view result served. A repeat view whose result is byte-identical returns a short
stub — the earlier tool result already carries the content verbatim. Content digests need no file
timestamps, so a same-size rewrite in one coarse mtime tick (or on a host whose clock disagrees with
the filesystem's) is never mistaken for "unchanged". Cleared via ``reset_skill_view_dedup()`` on
context compression AND on a committed proactive tool-result prune, because both replace the
original content with a one-line marker.
"""

import hashlib
import json
import threading

_skill_view_tracker: dict[str, dict[tuple, str]] = globals().get("_skill_view_tracker") or {}
_skill_view_tracker_lock = globals().get("_skill_view_tracker_lock") or threading.Lock()
_SKILL_VIEW_DEDUP_CAP = 200

_SKILL_VIEW_DEDUP_MESSAGE = (
    "Skill content unchanged since it was loaded earlier in this "
    "conversation — refer to the earlier skill_view result; it is still "
    "current and complete. (Re-issued after context compression, this "
    "returns the full content again.)")


def dedup_skill_view(task_id, name, file_path, payload: dict, result: str) -> str | None:
    """The "unchanged" stub when this task was already served exactly ``result`` for this skill
    file; otherwise record ``result``'s digest and return None (serve the full result)."""
    # Never dedup setup-needed views: readiness depends on config/env state that
    # changes without the file changing; the model must see the refreshed status.
    if (not task_id or payload.get("setup_needed")
            or payload.get("readiness_status") == "setup_needed"):
        return None
    resolved = str(payload.get("name") or name)
    key = (resolved, file_path or "")
    digest = hashlib.sha256(result.encode("utf-8")).hexdigest()
    with _skill_view_tracker_lock:
        cache = _skill_view_tracker.setdefault(str(task_id), {})
        if cache.get(key) == digest:
            return json.dumps({
                "success": True, "status": "unchanged", "name": resolved,
                "file": file_path or "SKILL.md", "dedup": True, "content_returned": False,
                "message": _SKILL_VIEW_DEDUP_MESSAGE}, ensure_ascii=False)
        cache.pop(key, None)  # re-insert at the end: FIFO eviction keeps the recently served
        cache[key] = digest
        while len(cache) > _SKILL_VIEW_DEDUP_CAP:  # FIFO eviction
            del cache[next(iter(cache))]
    return None


_JSON_DECODER = json.JSONDecoder()


def parse_unpruned_skill_view_payload(raw: str | None) -> tuple[dict, str] | None:
    """Parse the leading JSON object from a ``skill_view`` tool result string.

    Uses ``json.JSONDecoder().raw_decode`` rather than ``json.loads`` so trailing
    blocks appended by post-tool hooks (``[skill-error-router]...``) or tool-loop
    guardrails (``[Tool loop warning: ...]``) do not raise ``JSONDecodeError: Extra data``.
    Returns ``(payload, json_prefix)`` for full unpruned skill/reference bodies, or ``None``.
    """
    if not isinstance(raw, str):
        return None
    stripped = raw.lstrip()
    if not stripped.startswith("{") or '"success": true' not in stripped[:120]:
        return None
    try:
        payload, end = _JSON_DECODER.raw_decode(stripped)
    except Exception:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("success") is not True
        or payload.get("dedup")
        or payload.get("setup_needed")
        or payload.get("readiness_status") == "setup_needed"
        or not isinstance(payload.get("content"), str)
        or not ("skill_dir" in payload or "linked_files" in payload or "file_type" in payload)
    ):
        return None
    return payload, stripped[:end]


def seed_skill_view_dedup_from_messages(task_id: str | None, messages: list | None) -> None:
    """Populate the per-task dedup cache from unpruned ``skill_view`` tool results in *messages*.

    Called after compaction or proactive prune so skills whose full JSON bodies survived in the
    protected tail stay deduplicated, while skills pruned to ``[SKILL_PRUNED: ...]`` markers are
    absent and serve full content on reload.
    """
    if not task_id or not isinstance(messages, list):
        return
    entries: list[tuple[tuple[str, str], str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            parsed = parse_unpruned_skill_view_payload(msg.get("content"))
            if parsed is None:
                continue
            payload, json_prefix = parsed
            resolved = str(payload.get("name") or "")
            if not resolved:
                continue
            key = (resolved, str(payload.get("file") or ""))
            digest = hashlib.sha256(json_prefix.encode("utf-8")).hexdigest()
            entries.append((key, digest))
        elif role == "user":
            raw_user = msg.get("content")
            if not isinstance(raw_user, str) or "[IMPORTANT: The " not in raw_user[:64]:
                continue
            try:
                from agent.skill_commands import skills_loaded_by_scaffold
                from agent.skill_utils import normalize_skill_lookup_name
                from tools.skills_tool import skill_view
                for sname in skills_loaded_by_scaffold(raw_user):
                    norm_name = normalize_skill_lookup_name(sname)
                    sv_raw = skill_view(norm_name, task_id=task_id, preprocess=True)
                    parsed_sv = parse_unpruned_skill_view_payload(sv_raw)
                    if parsed_sv is None:
                        continue
                    sv_payload, sv_prefix = parsed_sv
                    sv_body = str(sv_payload.get("content") or "").strip()
                    if not sv_body or sv_body not in raw_user:
                        continue
                    resolved = str(sv_payload.get("name") or norm_name)
                    if resolved:
                        entries.append(((resolved, ""), hashlib.sha256(sv_prefix.encode("utf-8")).hexdigest()))
            except Exception:
                continue
    if not entries:
        return
    with _skill_view_tracker_lock:
        cache = _skill_view_tracker.setdefault(str(task_id), {})
        for key, digest in entries:
            cache.pop(key, None)
            cache[key] = digest
            while len(cache) > _SKILL_VIEW_DEDUP_CAP:
                del cache[next(iter(cache))]


def reset_skill_view_dedup(
    task_id: str | None = None, *, surviving_messages: list | None = None
) -> None:
    """Clear the dedup cache (all tasks when task_id is None); called on context compression.
    When *surviving_messages* is provided, re-seeds unpruned ``skill_view`` results that survived
    in the kept tail so redundant reloads of still-live skills return the unchanged stub."""
    with _skill_view_tracker_lock:
        if task_id is None:
            _skill_view_tracker.clear()
        else:
            _skill_view_tracker.pop(str(task_id), None)
    if task_id is not None and surviving_messages:
        seed_skill_view_dedup_from_messages(task_id, surviving_messages)
