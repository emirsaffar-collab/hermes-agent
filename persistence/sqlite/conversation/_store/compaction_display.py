"""Display reads paint a compacted tool result once, as its archived original."""


from __future__ import annotations
from typing import Any, Iterable
from session_rules.turns import MODEL_ONLY_DISPLAY_METADATA_KEY
from persistence.sqlite._schema_ddl import _sql_json_extract


_ORIGINAL_VISIBLE_SQL = (
    f"COALESCE({_sql_json_extract('original.display_metadata', '$.' + MODEL_ONLY_DISPLAY_METADATA_KEY)}, 0) = 0")


def not_a_later_tool_copy_sql(alias: str) -> str:
    """Filter that drops *alias* rows that are a later copy of an archived tool result."""
    return (f" AND NOT ({alias}.role = 'tool' AND {alias}.message_uid IS NOT NULL AND EXISTS ("
            f"SELECT 1 FROM messages AS original WHERE original.session_id = {alias}.session_id "
            f"AND original.timestamp = {alias}.timestamp AND original.message_uid = {alias}.message_uid "
            "AND original.role = 'tool' AND original.active = 0 AND original.compacted = 1 "
            f"AND (+original.display_order < {alias}.display_order "
            f"OR (+original.display_order = {alias}.display_order AND original.id < original.display_order "
            f"AND original.id < {alias}.id)) AND {_ORIGINAL_VISIBLE_SQL}))")


def later_tool_copy_ids(db: Any, rows: Iterable[Any]) -> set[int]:
    """Python twin of :func:`not_a_later_tool_copy_sql` over display-set *rows*: the ids it skips.
    Empty when the rows carry no slot (``display_order``) or archive flag (``compacted``)."""
    from persistence.sqlite.conversation._store.messages import _is_model_only_row
    tools = [row for row in rows if row["role"] == "tool"]
    if not tools or "compacted" not in tools[0].keys():
        return set()
    candidates = [row for row in tools if row["active"] or row["compacted"]]
    if not candidates or "display_order" not in candidates[0].keys():
        return set()

    def owner(row):
        return row["session_id"], row["message_uid"], row["timestamp"]

    first_orig: dict[Any, tuple[int, int]] = {}
    for row in candidates:
        slot = row["display_order"]
        if row["message_uid"] and slot is not None and not row["active"] and not _is_model_only_row(db, row):
            cur = first_orig.get(owner(row))
            cand = (slot, row["id"])
            if cur is None or cand < cur:
                first_orig[owner(row)] = cand
    skip: set[int] = set()
    for row in candidates:
        slot = row["display_order"]
        if not row["message_uid"] or slot is None:
            continue
        orig = first_orig.get(owner(row))
        if orig is not None and (orig[0] < slot or (orig[0] == slot and orig[1] < orig[0] and orig[1] < row["id"])):
            skip.add(row["id"])
    return skip
