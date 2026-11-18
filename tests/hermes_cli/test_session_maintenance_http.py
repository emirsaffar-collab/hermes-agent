"""Legacy bulk maintenance must exclude the real gateway owner reservation."""
import httpx
import pytest
from fastapi import FastAPI

from gateway.runtime_ownership import ProfileOwnership
from hermes_cli.web_routers.sessions import manage_router
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_read import session_row
from tests.fixtures.storage_seed import seed_session


@pytest.mark.asyncio
async def test_maintenance_routes_refuse_live_owner_without_changing_rows(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    app = FastAPI()
    app.include_router(manage_router)
    with open_persistence(profile_scope(home=tmp_path)) as store:
        seed_session(store, 'ended-empty', 'cli', end_reason='complete')
        before = session_row(store, 'ended-empty')
        owner = ProfileOwnership()
        owner.reserve([tmp_path])
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                for method, path, body in [('POST', 'bulk-delete', {'ids': ['ended-empty']}),
                        ('DELETE', 'empty', None), ('POST', 'owner-backfill', {}),
                        ('POST', 'prune', {'older_than_days': 1})]:
                    response = await client.request(method, '/api/sessions/' + path, json=body)
                    assert response.status_code == 409, (path, response.text)
                    assert 'maintenance' in response.json()['detail'].lower()
                    assert session_row(store, 'ended-empty') == before
        finally:
            owner.close()


@pytest.mark.asyncio
async def test_exclusive_offline_maintenance_preserves_bulk_behavior(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    app = FastAPI()
    app.include_router(manage_router)
    with open_persistence(profile_scope(home=tmp_path)) as store:
        seed_session(store, 'parent', 'cli')
        seed_session(store, 'child', 'cli', parent_session_id='parent')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            result = await client.post('/api/sessions/bulk-delete', json={'ids': ['parent', 'absent']})
            assert result.status_code == 200, result.text
            assert result.json() == {'ok': True, 'deleted': 1, 'skipped_active': []}
            assert session_row(store, 'parent') is None
            assert session_row(store, 'child')['parent_session_id'] is None


@pytest.mark.asyncio
async def test_owner_backfill_skips_exclusive_maintenance_when_no_unowned_rows_exist(tmp_path, monkeypatch):
    """When a live gateway control socket owns the home and all sessions already have
    profile_name stamped, standalone serve must return 200 via read_only precheck instead
    of failing 409 on every Desktop boot; if an unowned NULL row exists, it must still refuse 409."""
    monkeypatch.setattr('hermes_state._default_db_path', lambda: tmp_path / 'state.db')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(
        'hermes_cli.gateway_runtime_discovery.query_identify',
        lambda home, timeout=1.5: {'state': 'starting', 'runtime_protocol': 1},
    )
    app = FastAPI()
    app.include_router(manage_router)
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('already-owned', source='cli')
        owner = ProfileOwnership()
        owner.reserve([tmp_path])
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                # Polarity 1 (0 NULL rows): read_only precheck short-circuits with 200
                response = await client.post('/api/sessions/owner-backfill', json={})
                assert response.status_code == 200, response.text
                assert response.json() == {'ok': True, 'stamped': 0, 'profile': 'default'}

                # Polarity 2 (legacy NULL row present): must not falsely report stamped=0; hits write lock -> 409
                db._execute_write(
                    lambda conn: conn.execute("UPDATE sessions SET profile_name = NULL WHERE id = 'already-owned'")
                )
                refused = await client.post('/api/sessions/owner-backfill', json={})
                assert refused.status_code == 409, refused.text
        finally:
            owner.close()
