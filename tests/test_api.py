from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from api.main import create_app


def window(minutes=5, hours=2):
    s = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    e = s + timedelta(hours=hours)
    return dict(start_at=s.isoformat(), end_at=e.isoformat())


def request(client, method, path, actor='student-1', **kwargs):
    return client.request(method, path, headers={'X-Actor-Id':actor}, **kwargs)


def test_catalog_create_overlap_cancel_and_permissions(tmp_path):
    with TestClient(create_app(str(tmp_path/'demo1.db'))) as c:
        assert request(c,'GET','/equipment').status_code == 200
        payload = {'equipment_id':1, 'purpose':'Студенческий проект', **window()}
        first=request(c,'POST','/bookings', json=payload)
        assert first.status_code==201, first.text
        i=first.json()['id']
        assert request(c,'POST','/bookings',actor='student-2',json=payload).status_code==409
        assert request(c,'GET',f'/bookings/{i}',actor='student-2').status_code==404
        assert request(c,'POST',f'/bookings/{i}/decision',json={'decision':'approve'}).status_code==403
        assert request(c,'POST',f'/bookings/{i}/cancel').json()['status']=='cancelled'
        assert request(c,'POST','/bookings',actor='student-2',json=payload).status_code==201


def test_review_revision_issue_return_damaged(tmp_path):
    with TestClient(create_app(str(tmp_path/'demo2.db'))) as c:
        payload={'equipment_id':2, 'purpose':'Запись интервью',**window(minutes=2)}
        i=request(c,'POST','/bookings',json=payload).json()['id']
        assert request(c,'POST',f'/bookings/{i}/decision',actor='admin-1',
                       json={'decision':'needs_info','comment':'Уточните цель'}).json()['status']=='needs_info'
        assert request(c,'PATCH',f'/bookings/{i}',json={**window(minutes=2),'purpose':'Запись подкаста'}).status_code==200
        assert request(c,'POST',f'/bookings/{i}/decision',actor='admin-1',
                       json={'decision':'approve'}).json()['status']=='approved'
        assert request(c,'POST',f'/bookings/{i}/issue',actor='admin-1').json()['status']=='issued'
        assert request(c,'POST',f'/bookings/{i}/return',actor='admin-1',
                       json={'condition':'damaged','comment':'Не работает кнопка'}).json()['status']=='returned'
        eq=request(c,'GET','/equipment/2').json()
        assert eq['condition']=='maintenance'
        assert request(c,'POST','/bookings',json=payload).status_code==409
        assert len(request(c,'GET','/admin/events',actor='admin-1').json())==6


def test_overdue_blocks_until_return_and_job_idempotent(tmp_path):
    with TestClient(create_app(str(tmp_path/'demo3.db'))) as c:
        payload={'equipment_id':3,'purpose':'Свет для съемок',**window(minutes=1)}
        i=request(c,'POST','/bookings',json=payload).json()['id']
        request(c,'POST',f'/bookings/{i}/decision',actor='admin-1',json={'decision':'approve'})
        request(c,'POST',f'/bookings/{i}/issue',actor='admin-1')
        # Force elapsed end timestamp in the isolated test database.
        import sqlite3
        with sqlite3.connect(str(tmp_path/'demo3.db')) as db:
            db.execute('UPDATE bookings SET start_at=?, end_at=? WHERE id=?',
                       ((datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(timespec='microseconds'),
                        (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(timespec='microseconds'),i))
        other={'equipment_id':1,'purpose':'Новая съемка',**window(minutes=5)}
        assert request(c,'POST','/bookings',json=other).status_code==403
        assert request(c,'POST','/admin/jobs/overdue-check',actor='admin-1').json()['newly_overdue']==1
        assert request(c,'POST','/admin/jobs/overdue-check',actor='admin-1').json()['newly_overdue']==0
        assert request(c,'POST',f'/bookings/{i}/return',actor='admin-1',json={'condition':'good'}).status_code==200
        assert request(c,'POST','/bookings',json=other).status_code==201
