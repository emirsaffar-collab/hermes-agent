"""The hand-off of a local conversation whose current target a messaging lane took over, run by the routing verbs
inside their transactions: the receipts that moved, their release, and the move of the conversation's other segments
onto the lane. Each is a module function on the caller's open connection."""

from __future__ import annotations

import json

from .._schema import _title_rank
from .._schema_ddl import NONDEFAULT_POLICY_WHERE
from ..conversation._store.local import POLICY_PREFIX
from ..conversation._store.local_migration import LEGACY_PREFIX


def moved_receipts(conn, target_ids=None, sources=None):
    """``{owner id: (moved row id, its source)}`` of local receipts whose current physical target row
    now carries another lane's route. An explicit repoint (``/handoff``, a platform ``/resume``)
    rewrote that row's routing identity, so the receipt can never restore the conversation again
    (the stored identity check in ``gateway.session_local_recovery._checked_entry``). Only the
    current target counts: an earlier ``/new`` segment that moved leaves the conversation the
    receipt guards local, and releasing it would strand that transcript. *target_ids* narrows the
    scan to the rows one repoint rewrote; *sources* narrows to rows whose current source is in
    *sources*. Uses direct PK lookups for default receipts (``entry.session_id == owner``) and the
    partial index ``idx_state_meta_local_policy_special`` (``NONDEFAULT_POLICY_WHERE``) for
    repointed receipts, avoiding full ``state_meta`` scans while preserving ``local-*`` handoffs."""
    if target_ids is not None:
        target_tuple = tuple(dict.fromkeys(target_ids))
        if not target_tuple:
            return {}
    else:
        target_tuple = None
    if sources is not None:
        source_tuple = tuple(dict.fromkeys(sources))
        if not source_tuple:
            return {}
    else:
        source_tuple = None

    found = {}

    # Branch 1: default receipts where key = POLICY_PREFIX || s.id and entry.session_id = s.id.
    where_s = ["s.session_key IS NOT NULL"]
    params_1: list = [POLICY_PREFIX]
    if target_tuple is not None:
        where_s.append(f"s.id IN ({','.join('?' * len(target_tuple))})")
        params_1.extend(target_tuple)
    if source_tuple is not None:
        where_s.append(f"s.source IN ({','.join('?' * len(source_tuple))})")
        params_1.extend(source_tuple)
    rows_default = conn.execute(
        "SELECT s.id, s.source FROM sessions s "
        "CROSS JOIN state_meta m ON m.key = ? || s.id "
        f"WHERE {' AND '.join(where_s)} "
        "AND json_valid(m.value) "
        "AND json_extract(m.value, '$.entry.session_id') = s.id "
        "AND s.session_key IS NOT json_extract(m.value, '$.route')",
        params_1,
    ).fetchall()
    for sid, source in rows_default:
        found[sid] = (sid, source)

    # Branch 2: non-default receipts (repointed /new or /compress target, lineage, derived ids)
    # via the partial index on NONDEFAULT_POLICY_WHERE.
    where_2 = [
        NONDEFAULT_POLICY_WHERE,
        "s.session_key IS NOT NULL",
        "s.session_key IS NOT json_extract(m.value, '$.route')",
    ]
    params_2: list = [len(POLICY_PREFIX) + 1]
    if target_tuple is not None:
        where_2.append(f"s.id IN ({','.join('?' * len(target_tuple))})")
        params_2.extend(target_tuple)
    if source_tuple is not None:
        where_2.append(f"s.source IN ({','.join('?' * len(source_tuple))})")
        params_2.extend(source_tuple)
    rows_nondefault = conn.execute(
        "SELECT substr(m.key, ?), s.id, s.source FROM state_meta m "
        "JOIN sessions s ON s.id = json_extract(m.value, '$.entry.session_id') "
        f"WHERE {' AND '.join(where_2)}",
        params_2,
    ).fetchall()
    for owner, target, source in rows_nondefault:
        found[owner] = (target, source)

    return found


def release_receipts(conn, owners):
    """Retire *owners*' local receipts (creation policy and legacy binding): the row they guarded now
    belongs to a messaging lane, which restores it from its route like any other platform session."""
    conn.executemany('DELETE FROM state_meta WHERE key IN (?,?)',
                     [(POLICY_PREFIX + owner, LEGACY_PREFIX + owner) for owner in owners])


_LANE_IDENTITY = 'session_key, source, user_id, chat_id, chat_type, thread_id, display_name, origin_json, transport_profile'


def hand_off_conversations(conn, moved):
    """Retire the receipts in *moved* (``moved_receipts``: a messaging lane took over each one's
    current target) and move the rest of each conversation onto that lane with it. Its earlier
    ``/new`` segments take the lane's identity, so they open there as read-only history (a lane
    moved past them) instead of staying local rows no receipt restores; the conversation's name
    follows its head, which the lane continues as its own conversation. Returns the receipts it
    handed off: one whose lineage is malformed stays as it is (its own reads refuse it, logged)."""
    handed = {}
    for owner, (target, source) in moved.items():
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + owner,)).fetchone()
        lineage = (_receipt(saved[0]) or {}).get('lineage', [owner]) if saved else [owner]
        if not isinstance(lineage, list) or not all(isinstance(sid, str) for sid in lineage):
            continue
        segments = list(dict.fromkeys([owner, *lineage]))
        conn.execute(f'UPDATE sessions SET ({_LANE_IDENTITY}) = (SELECT {_LANE_IDENTITY} FROM sessions WHERE id=?) '
                     'WHERE id IN (SELECT value FROM json_each(?)) AND id != ?', (target, json.dumps(segments), target))
        _title_follows_head(conn, owner, _segment_root(conn, segments, target))
        handed[owner] = (target, source)
    release_receipts(conn, handed)
    return handed


def _receipt(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _segment_root(conn, segments, target):
    """The first row of *target*'s ``/new`` segment: back through the compressions behind it."""
    if target not in segments:
        return target
    index = segments.index(target)
    while index > 0 and (row := conn.execute('SELECT end_reason FROM sessions WHERE id=?',
                                               (segments[index - 1],)).fetchone()) and row[0] == 'compression':
        index -= 1
    return segments[index]


def _title_follows_head(conn, owner, head):
    """The conversation's name (a Bot Chat's registry title included) moves to its head, over a
    title of lower provenance there (a generated one); a name of equal rank the head carries stays
    (``_title_rank``: a pre-provenance title ranks as the user's)."""
    row = conn.execute('SELECT title, title_source, hidden FROM sessions WHERE id=?', (owner,)).fetchone()
    if head == owner or row is None or not row[0]:
        return
    kept = conn.execute('SELECT title, title_source FROM sessions WHERE id=?', (head,)).fetchone()
    if kept is None or (kept[0] and _title_rank(kept[1]) >= _title_rank(row[1])):
        return
    conn.execute('UPDATE sessions SET title=NULL WHERE id IN (?,?)', (owner, head))
    conn.execute('UPDATE sessions SET title=?, title_source=?, hidden=? WHERE id=?', (*row, head))
