"""Bounded room lists, tied timestamps, scoped cursors and permission filters."""
import pytest
from app.security import DomainError, Settings, iso_time
from app.store import Store


def seeded(store):
    owner = store.create_session(source='pagination-owner', nickname='鍒嗛〉鎴夸富')
    viewer = store.create_session(source='pagination-viewer', nickname='鍒嗛〉瑙備紬')
    with store._transaction(write=True) as connection:
        host = store._session(connection, owner['token'])['id']
        guest = store._session(connection, viewer['token'])['id']
        at = iso_time()
        for index in range(1107):
            rid = f'{index:032x}'
            connection.execute("INSERT INTO rooms(id,title,scenario_id,status,host_session_id,created_at,updated_at) VALUES(:id,'page','mygo_rehearsal',:status,:host,:at,:at)", {'id':rid,'status':'ended' if index % 2 else 'preparing','host':host,'at':at})
            connection.execute("INSERT INTO room_members(id,room_id,session_id,alias,display_alias,joined_at) VALUES(:id,:rid,:host,'瑙備紬 1','瑙備紬 1',:at)", {'id':'host'+rid,'rid':rid,'host':host,'at':at})
            if index % 3 == 0:
                connection.execute("INSERT INTO room_members(id,room_id,session_id,alias,display_alias,joined_at) VALUES(:id,:rid,:guest,'瑙備紬 2','瑙備紬 2',:at)", {'id':'guest'+rid,'rid':rid,'guest':guest,'at':at})
        connection.execute("INSERT INTO room_bans(room_id,session_id,removed_by_member_id,created_at) VALUES(:rid,:guest,:member,:at)", {'rid':f'{3:032x}','guest':guest,'member':'host'+f'{3:032x}','at':at})
    return owner['token'], viewer['token']


def verify_pages(store):
    owner, viewer = seeded(store)
    first = store.list_rooms(viewer,{})
    assert len(first['rooms']) == 50 and first['has_more']
    assert first['rooms'][0]['host_member_id']
    for filter, expected in [('all',[i for i in range(1107) if i != 3]), ('active',list(range(0,1107,2))), ('ended',[i for i in range(1,1107,2) if i != 3]), ('mine',[i for i in range(0,1107,3) if i != 3])]:
        cursor = None
        seen = []
        while True:
            page = store.list_rooms(viewer,{},37,cursor,filter)
            assert len(page['rooms']) <= 37
            seen.extend(int(room['id'],16) for room in page['rooms'])
            if not page['has_more']:
                assert page['next_cursor'] is None
                break
            assert page['next_cursor'] != cursor
            cursor = page['next_cursor']
        assert seen == expected
    cursor = first['next_cursor']
    for args in [(viewer,{},101), (viewer,{},0), (viewer,{},50,'garbage'), (viewer,{},50,cursor,'active'), (owner,{},50,cursor)]:
        with pytest.raises(DomainError) as error:
            store.list_rooms(*args)
        assert error.value.status_code == 422
    parts = cursor.split('|'); parts[2] = '2000-01-01T00:00:00.000000Z'
    assert store.list_rooms(viewer,{},50,'|'.join(parts)) == {'rooms':[], 'has_more':False,'next_cursor':None}
    # Existing rows retain member permissions and host identity in batched snapshots.
    owned = store.list_rooms(owner,{},1)['rooms'][0]
    # Account-era policy: room ownership must not reveal private reports.
    assert owned['me']['is_host'] and not owned['me']['can_manage_reports']


def test_sqlite_thousand_rooms(tmp_path):
    store = Store(Settings.from_values(data_dir=tmp_path,public_origin='http://localhost'))
    store.initialize()
    verify_pages(store)
    queries = []
    original_connect = store._connect
    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(queries.append)
        return connection
    store._connect = traced_connect
    # Query count stays constant for a full page, rather than two extra queries per room.
    # Reuse the seeded host token through a new session with no room membership.
    account = store.create_session(source="query-page",nickname="查询页")
    queries.clear()
    store.list_rooms(account["token"],{},100)
    # One account-token lookup was added; room options and seats stay batched.
    assert sum(sql.lstrip().upper().startswith("SELECT") for sql in queries) == 4
    with store._transaction() as connection:
        plan = connection.execute('EXPLAIN QUERY PLAN SELECT * FROM rooms ORDER BY created_at DESC,id ASC LIMIT 51').fetchall()
        assert any('rooms_created_page' in row[3] for row in plan)



def test_room_pagination_http_validation(tmp_path, monkeypatch, http_database):
    # Preserve the v0.8 API contract as an explicit compatibility path.
    monkeypatch.setenv('ONSTAGE_LEGACY_GUESTS', '1')
    from fastapi.testclient import TestClient
    from app.main import create_app
    application = create_app(data_dir=tmp_path, public_origin="http://testserver", secure_cookie=False)
    with TestClient(application, headers={"Origin":"http://testserver"}) as client:
        assert client.get("/api/rooms").status_code == 401
        assert client.post("/api/session",json={"nickname":"分页访客"}).status_code == 200
        assert client.get("/api/rooms").json() == {"rooms":[],"next_cursor":None,"has_more":False}
        for query in ["limit=0","limit=101","limit=1.5","filter=bad","cursor=", "cursor=invalid"]:
            assert client.get("/api/rooms?"+query).status_code == 422
