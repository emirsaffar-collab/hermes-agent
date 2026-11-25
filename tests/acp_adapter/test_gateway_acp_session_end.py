"""ACP v0.9 has no per-session destroy: the last viewer leaving an idle ACP session is its end (#118216).

Real authority + SQLite. Without an ``ended_at`` writer on the gateway path, source='acp' rows stay
open forever and the ended-session guard keeps prune/archive away from them.
"""
from types import SimpleNamespace

import pytest
from tests.fixtures.storage_read import session_row

pytestmark = pytest.mark.usefixtures("authority_cwd_in_tmp")  # symbolic profile ids are relative homes


@pytest.mark.asyncio
async def test_last_viewer_or_shutdown_ends_only_idle_acp_sessions(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.run_runtime import settle_gateway_runtime
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Submission
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': [], 'acp': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store.persistence, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='owner')
    db = authority.persistence

    def viewer():
        return AuthorityConnection(authority, object(), {'user_id': 'human', 'profile_id': 'fixture'})

    owner = viewer().actor

    def create(source, request_id):
        ref = create_local_session(authority, owner, {'request_id': request_id, 'source': source,
                                                      'cwd': str(tmp_path), 'model': 'frozen', 'toolsets': []})
        return ref, db.bindings.receipt(ref.session_id).entry['session_id']

    def ended(target):
        row = session_row(db, target)
        return row['ended_at'] is not None, row['end_reason']

    async def resume(connection, ref):
        reply = await connection.dispatch({'id': 1, 'method': 'session.resume',
                                           'params': {'session_id': ref.session_id}})
        assert 'result' in reply, reply

    try:
        acp, acp_row = create('acp', 'editor')
        gui, gui_row = create('gui', 'desktop')
        busy, busy_row = create('acp', 'busy-editor')
        _unseen, unseen_row = create('acp', 'unseen-editor')
        first, second = viewer(), viewer()
        for connection in (first, second):
            await resume(connection, acp)
            await resume(connection, gui)
        await resume(first, busy)
        authority._schedule = lambda ref: None
        await authority.submit(first.actor, Submission('queued', busy, {'text': 'later'}, 'queue'))

        await first.close()
        assert ended(acp_row) == (False, None), 'another viewer still holds the ACP session'
        assert ended(busy_row) == (False, None), 'a queued admission keeps the ACP session open'
        await second.close()
        assert ended(acp_row) == (True, 'acp_disconnect')
        assert ended(gui_row) == (False, None), 'only ACP sessions end on disconnect'
        assert ended(busy_row) == (False, None)

        # Gateway shutdown ends the ACP sessions nobody is viewing (never attached this run included).
        await settle_gateway_runtime(runner)
        assert ended(unseen_row) == (True, 'acp_disconnect')
        assert ended(busy_row) == (False, None) and ended(gui_row) == (False, None)
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_acp_session_left_mid_turn_ends_once_its_fifo_drains(tmp_path, monkeypatch):
    """The last viewer leaving during a turn cannot end the session; the drain going idle must."""
    import asyncio
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Submission
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session

    release, started = asyncio.Event(), asyncio.Event()

    async def execute(authority, ref, row):  # the model turn; drain + settlement stay real
        started.set()
        await release.wait()
        return 'done'

    monkeypatch.setattr('gateway.session_finite.execute_finite_admission', execute)
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'acp': [], 'gui': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store.persistence, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='owner')
    try:
        rows = {}
        for source in ('acp', 'gui'):
            viewer = AuthorityConnection(authority, object(), {'user_id': 'human', 'profile_id': 'fixture'})
            ref = create_local_session(authority, viewer.actor, {'request_id': source, 'source': source,
                                       'cwd': str(tmp_path), 'model': 'frozen', 'toolsets': []})
            target = authority.persistence.bindings.receipt(ref.session_id).entry['session_id']
            reply = await viewer.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': ref.session_id}})
            assert 'result' in reply, reply
            started.clear()
            await authority.submit(viewer.actor, Submission(source + '-turn', ref, {'text': 'work'}, 'queue'))
            await asyncio.wait_for(started.wait(), 10)  # the turn is running, not merely queued
            await viewer.close()
            assert session_row(authority.persistence, target)['ended_at'] is None, 'running work keeps the session open'
            rows[source] = ref, target
        release.set()
        for ref, _ in rows.values():
            await authority.sessions[ref.session_id].task
        acp_row = session_row(authority.persistence, rows['acp'][1])
        assert (acp_row['ended_at'] is not None, acp_row['end_reason']) == (True, 'acp_disconnect')
        assert session_row(authority.persistence, rows['gui'][1])['ended_at'] is None, 'only ACP sessions end on idle'
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_shutdown_ends_acp_sessions_across_all_lifecycle_stages_via_indexed_query(tmp_path, monkeypatch):
    """Candidate C: ``end_idle_acp_sessions`` uses ``acp_session_ids()`` (never ``local_session_ids()``)
    and handles all 4 ACP lifecycle stages: fresh source='acp', post-turn source='local' with
    created_source='acp', /new reset child with chat_id=owner, and skipping ended/non-ACP rows."""
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.run_runtime import settle_gateway_runtime
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    from persistence.authority.bindings import SessionId

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': [], 'acp': [], 'gui': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store.persistence, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='owner')
    db = authority.persistence
    owner = AuthorityConnection(authority, object(), {'user_id': 'human', 'profile_id': 'fixture'}).actor

    def create(source, request_id):
        ref = create_local_session(authority, owner, {'request_id': request_id, 'source': source,
                                                      'cwd': str(tmp_path), 'model': 'frozen', 'toolsets': []})
        return ref, db.bindings.receipt(ref.session_id).entry['session_id']

    try:
        # Stage 1: fresh source='acp'
        s1_ref, s1_row = create('acp', 'stage1-fresh')
        # Stage 2: post-turn row where SessionStore overwrote source='local' while keeping created_source='acp'
        s2_ref, s2_row = create('acp', 'stage2-post-turn')
        # Stage 3: /new reset child row pointing back via chat_id=owner
        s3_ref, s3_row = create('acp', 'stage3-reset')
        # Stage 4: already-ended ACP row + non-ACP local row
        _s4_ref, s4_row = create('acp', 'stage4-already-ended')
        _gui_ref, gui_row = create('gui', 'stage4-non-acp-gui')

        # Simulate Stage 2 (post-turn source='local', created_source='acp'), Stage 3 (/new reset child),
        # and Stage 4 (pre-ended ACP row) directly on SQLite as runtime writes them.
        s3_child = f'{s3_row}-seg2'
        db.bindings.reset_target(
            epoch=authority.epoch,
            parent_session_id=SessionId(s3_row),
            entry={**dict(db.bindings.receipt(s3_ref.session_id).entry), 'session_id': s3_child},
        )
        def _seed_stages(conn):
            conn.execute("UPDATE sessions SET source='local', created_source='acp' WHERE id=?", (s2_row,))
            conn.execute("UPDATE sessions SET source='local', created_source='acp' WHERE id=?", (s3_child,))
            conn.execute("UPDATE sessions SET ended_at=1700000000.0, end_reason='acp_disconnect' WHERE id=?", (s4_row,))
        db.bindings._store.holder()._execute_write(_seed_stages)

        # Verify acp_session_ids() returns ONLY open ACP owners (Stages 1, 2, 3), never Stage 4 or GUI.
        found_acp = set(db.bindings.acp_session_ids())
        assert found_acp == {SessionId(s1_ref.session_id), SessionId(s2_ref.session_id), SessionId(s3_ref.session_id)}

        # Prove end_idle_acp_sessions NEVER calls full-table local_session_ids().
        def _forbid_full_scan():
            raise AssertionError("end_idle_acp_sessions must use acp_session_ids(), not local_session_ids()")
        monkeypatch.setattr(db.bindings, 'local_session_ids', _forbid_full_scan)

        await settle_gateway_runtime(runner)
        for target in (s1_row, s2_row, s3_child):
            row = session_row(db, target)
            assert row is not None
            assert (row['ended_at'] is not None, row['end_reason']) == (True, 'acp_disconnect'), target
        gui_after = session_row(db, gui_row)
        assert gui_after is not None and gui_after['ended_at'] is None
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_settle_gateway_runtime_stops_api_before_ending_idle_acp_sessions(tmp_path, monkeypatch):
    """Shutdown ordering: stop_gateway_api() must complete before end_idle_acp_sessions() runs."""
    from gateway import run, run_api, session_acp_lifecycle
    from gateway.config import GatewayConfig
    from gateway.run_runtime import settle_gateway_runtime
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'acp': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    events = []
    runner = SimpleNamespace(
        session_store=store,
        _session_db=store.persistence,
        adapters={},
        _draining=False,
        _cached_agent_for=lambda route: None,
        session_api=object(),
        session_ticket_store=SimpleNamespace(revoke=lambda: events.append('revoke')),
    )
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    await initialize_session_authority(runner, profile_id='fixture', instance_id='owner')

    async def fake_stop_api(handle):
        events.append('stop_gateway_api')

    def fake_end_acp(authority):
        events.append('end_idle_acp_sessions')
        return 0

    monkeypatch.setattr(run_api, 'stop_gateway_api', fake_stop_api)
    monkeypatch.setattr(session_acp_lifecycle, 'end_idle_acp_sessions', fake_end_acp)
    try:
        await settle_gateway_runtime(runner)
        assert events == ['revoke', 'stop_gateway_api', 'end_idle_acp_sessions']
    finally:
        store.close_all_db_handles()
