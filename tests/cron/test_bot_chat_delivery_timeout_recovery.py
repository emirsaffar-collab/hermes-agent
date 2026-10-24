"""A raise AFTER the RPC left the socket must not read as a delivery failure.

The producer computes a deterministic delivery id (sha256 over source home, job id, run
id, target home) and reads that receipt BEFORE dialing. The authority commits the same
receipt when it ADMITS the turn, which can be minutes after a 30 s ``rpc`` budget lapses,
so the exception branch is a three-way decision, not a binary one:

* a record for THIS payload → its status is the disposition (``settled`` → delivered,
  ``queued``/``claimed`` → completion unverified, ``ambiguous`` → the detail branch);
* a record for ANOTHER payload → the id belongs to something else: a definite failure;
* no record after the grace → ``unknown``: the caller keeps that out of ``delivery_errors``
  and books it as unverified, because the payload may still be queued in front of the owner;
* a raise from before the dial (or the helper's own ``PreDialError``) → a definite failure.

The cause string must never publish as an empty tail: ``str(TimeoutError())`` is ``''`` on
CPython, so ``f"... unverified: {exc}"`` used to name no cause at all.
"""
import pytest

GRACE = 0.05


def _install_authority(monkeypatch, mailbox, *, write_receipt=True, status="queued",
                       message=None, raises=None, category=None):
    """Stand in for the profile authority: optionally admit the turn, then lose the answer."""

    def fake_deliver(home, owner, message_, *, delivery_id, notification_category="result"):
        if write_receipt:
            record = {
                "delivery_id": delivery_id,
                "id": delivery_id,
                "status": status,
                "message": message_ if message is None else message,
                "created_at": 1,
                "sequence": 1,
            }
            if category is not None:
                record["notification_category"] = category
            with mailbox._locked(home) as root:
                mailbox._write(root / f"{delivery_id}.json", record)
        raise raises if raises is not None else TimeoutError()  # str(TimeoutError()) == ""

    monkeypatch.setattr(mailbox, "find_canonical_live_owner",
                        lambda home: {"session_id": "bot"})
    monkeypatch.setattr(mailbox, "deliver_to_live_owner", fake_deliver)
    # The grace is a wall-clock wait: pin it so the corpus runs in milliseconds. Tolerant of its
    # absence ON PURPOSE: run against pre-fix HEAD the corpus must fail on the BEHAVIOUR it asserts,
    # never on a missing fixture attribute, or the polarity receipt proves only that the test
    # harness knows the new symbol.
    from cron import scheduler_delivery
    if hasattr(scheduler_delivery, "BOT_CHAT_ADMISSION_GRACE_SECONDS"):
        monkeypatch.setattr(scheduler_delivery, "BOT_CHAT_ADMISSION_GRACE_SECONDS", GRACE)


def _deliver(job):
    from cron.scheduler_delivery import _deliver_to_bot_chat
    return _deliver_to_bot_chat(job, "the payload", "")


def _receipts(job) -> dict:
    return job.get("_bot_chat_delivery_receipts") or {}


def test_timeout_after_admission_is_not_a_delivery_failure(tmp_path, monkeypatch):
    """Admission survives a lost answer: the job carries a queued receipt, and the caller's
    ``delivery_errors`` rule therefore keeps it out of ``delivery_failed``."""
    from tools import bot_live_delivery as mailbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_authority(monkeypatch, mailbox)

    job = {"id": "j-timeout", "name": "n", "execution_id": "r1"}
    outcome = _deliver(job)

    assert outcome is not None, "an unverified admission is not a settled reply"
    assert _receipts(job)["bot-chat:(own)"]["status"] == "queued"
    assert "unverified: " not in outcome, "the empty-cause tail must never ship"
    assert "completion unverified; do not resend" in outcome


def test_timeout_without_a_receipt_is_unknown_not_a_failure(tmp_path, monkeypatch):
    """Nothing on disk after the grace is not proof of non-delivery: the authority may still be
    mid-turn. The job carries ``unknown`` so the caller books it as unverified, not as failed."""
    from tools import bot_live_delivery as mailbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_authority(monkeypatch, mailbox, write_receipt=False)

    job = {"id": "j-unknown", "name": "n", "execution_id": "r1"}
    outcome = _deliver(job)

    assert _receipts(job)["bot-chat:(own)"]["status"] == "unknown"
    assert outcome is not None, "unknown is still an unverified status string, never None"
    assert outcome.startswith("bot-chat:(own) unknown (receipt "), outcome
    assert "admission unconfirmed: TimeoutError" in outcome, "the class must be named"
    assert not outcome.endswith("unverified: "), "an empty tail is never acceptable"


def test_pre_dial_failure_stays_definite(tmp_path, monkeypatch):
    """``PreDialError`` means nothing reached the authority: that is a real delivery failure."""
    from tools import bot_live_delivery as mailbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_authority(monkeypatch, mailbox, write_receipt=False,
                       raises=mailbox.PreDialError("no session file lock"))

    job = {"id": "j-predial", "name": "n", "execution_id": "r1"}
    outcome = _deliver(job)

    assert _receipts(job) == {}, "a definite failure invents no receipt"
    assert outcome is not None
    assert outcome.endswith("unverified: failed before the dial: PreDialError: no session file lock"), outcome


def test_settled_receipt_recovered_after_a_raise_reads_as_delivered(tmp_path, monkeypatch):
    """The owner settled inside the grace: no error at all."""
    from tools import bot_live_delivery as mailbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_authority(monkeypatch, mailbox, status="settled")

    job = {"id": "j-settled", "name": "n", "execution_id": "r1"}
    assert _deliver(job) is None
    assert _receipts(job)["bot-chat:(own)"]["status"] == "settled"


def test_a_receipt_for_a_different_payload_is_never_recovered(tmp_path, monkeypatch):
    """The delivery id is deterministic per run; a record holding another payload is a
    different admission and must not launder this failure."""
    from tools import bot_live_delivery as mailbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_authority(monkeypatch, mailbox, message="some other payload")

    job = {"id": "j-mismatch", "name": "n", "execution_id": "r1"}
    outcome = _deliver(job)

    assert _receipts(job) == {}
    assert outcome is not None
    assert "another payload owns this delivery id" in outcome, outcome
    assert "unverified: " in outcome


def test_a_diagnostic_receipt_does_not_admit_a_result_payload(tmp_path, monkeypatch):
    """Same text, different category = different admission. The success path compares both, so
    the recovery must too, or a muted notice would vouch for a result that never went out."""
    from tools import bot_live_delivery as mailbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_authority(monkeypatch, mailbox, category="diagnostic")

    job = {"id": "j-category", "name": "n", "execution_id": "r1"}
    outcome = _deliver(job)

    assert _receipts(job) == {}
    assert outcome is not None
    assert "another payload owns this delivery id" in outcome, outcome


def test_an_unreadable_receipt_is_unknown_not_a_failure(tmp_path, monkeypatch):
    """A receipt the grace could not read is a blind spot, not proof of non-delivery: the class
    of the read error is logged, and the job still reports ``unknown``."""
    from tools import bot_live_delivery as mailbox

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _install_authority(monkeypatch, mailbox, write_receipt=False)

    def unreadable(home, delivery_id, timeout):
        raise OSError("receipt store unreadable")

    monkeypatch.setattr(mailbox, "await_delivery", unreadable)

    job = {"id": "j-blind", "name": "n", "execution_id": "r1"}
    outcome = _deliver(job)

    assert _receipts(job)["bot-chat:(own)"]["status"] == "unknown"
    assert outcome is not None
    assert "admission unconfirmed" in outcome, outcome
