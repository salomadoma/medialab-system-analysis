"""MediaLAB demo REST API. X-Actor-Id is DEMO identity, NOT authentication."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

ACTIVE = ('pending', 'needs_info', 'approved', 'issued', 'overdue')
MAX_ACTIVE = 3


def now():
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def require_aware(value: datetime):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Дата и время должны содержать часовой пояс (например, +03:00 или Z)')
    return value


class BookingCreate(BaseModel):
    equipment_id: int = Field(ge=1)
    start_at: datetime
    end_at: datetime
    purpose: str = Field(min_length=5, max_length=300)

    @model_validator(mode='after')
    def validate_period(self):
        require_aware(self.start_at)
        require_aware(self.end_at)
        if self.end_at <= self.start_at:
            raise ValueError('end_at должно быть позже start_at')
        if self.start_at < now():
            raise ValueError('Нельзя забронировать прошедший период')
        return self


class BookingUpdate(BaseModel):
    start_at: datetime
    end_at: datetime
    purpose: str = Field(min_length=5, max_length=300)

    @model_validator(mode='after')
    def valid(self):
        require_aware(self.start_at)
        require_aware(self.end_at)
        if self.end_at <= self.start_at or self.start_at < now():
            raise ValueError('Некорректный период')
        return self


class DecisionRequest(BaseModel):
    decision: Literal['approve', 'reject', 'needs_info']
    comment: str | None = Field(default=None, max_length=500)


class ReturnRequest(BaseModel):
    condition: Literal['good', 'damaged']
    comment: str | None = Field(default=None, max_length=500)


class EquipmentOut(BaseModel):
    id: int
    name: str
    category: str
    description: str
    condition: str
    available_now: bool
    available_for_period: bool | None = None


class BookingOut(BaseModel):
    id: int
    student_id: str
    equipment_id: int
    start_at: str
    end_at: str
    purpose: str
    status: str
    review_comment: str | None = None
    return_condition: str | None = None
    created_at: str
    issued_at: str | None = None
    returned_at: str | None = None


class EventOut(BaseModel):
    id: int
    booking_id: int
    actor_id: str
    action: str
    at: str
    note: str | None = None


class JobOut(BaseModel):
    checked: int
    newly_overdue: int


def create_app(db_path: str | None = None) -> FastAPI:
    dbfile = db_path or os.getenv('MEDIALAB_DB', str(Path(__file__).with_name('medialab.db')))
    app = FastAPI(
        title='MediaLAB Equipment Booking API', version='1.0.0',
        description=(
            'Учебный прототип системы бронирования техники. Тестовые акторы: '
            '`student-1`, `student-2`, `admin-1` через заголовок `X-Actor-Id`. '
            '**Важно:** заголовок не является безопасной аутентификацией. '
            'Telegram-бот и веб-панель описаны в концепции, но не входят в этот API-прототип.'
        ),
        openapi_tags=[
            {'name': 'Equipment', 'description': 'Каталог и доступность на период'},
            {'name': 'Bookings', 'description': 'Заявки студентов'},
            {'name': 'Administration', 'description': 'Решения и физический учет'},
        ],
    )

    @contextmanager
    def db(write: bool = False):
        connection = sqlite3.connect(dbfile, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    with db(write=True) as cx:
        cx.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL CHECK(role IN ('student','admin'))
        );
        CREATE TABLE IF NOT EXISTS equipment (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            condition TEXT NOT NULL DEFAULT 'ready' CHECK(condition IN ('ready','maintenance'))
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL REFERENCES users(id),
            equipment_id INTEGER NOT NULL REFERENCES equipment(id),
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            purpose TEXT NOT NULL,
            status TEXT NOT NULL,
            review_comment TEXT,
            return_condition TEXT,
            created_at TEXT NOT NULL,
            issued_at TEXT,
            returned_at TEXT,
            CHECK(end_at > start_at)
        );
        CREATE INDEX IF NOT EXISTS idx_bookings_equipment_period
            ON bookings(equipment_id, start_at, end_at, status);
        CREATE INDEX IF NOT EXISTS idx_bookings_student_status
            ON bookings(student_id, status);
        CREATE TABLE IF NOT EXISTS booking_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL REFERENCES bookings(id),
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            at TEXT NOT NULL,
            note TEXT
        );
        ''')
        cx.executemany('INSERT OR IGNORE INTO users(id, role) VALUES (?,?)', [
            ('student-1','student'), ('student-2','student'), ('admin-1','admin')])
        cx.executemany('''INSERT OR IGNORE INTO equipment(id,name,category,description,condition)
            VALUES (?,?,?,?,?)''', [
            (1, 'Canon EOS R6', 'photo', 'Беззеркальная камера', 'ready'),
            (2, 'Zoom H6', 'audio', 'Портативный аудиорекордер', 'ready'),
            (3, 'Aputure 120d', 'light', 'LED-осветитель', 'ready'),
        ])

    def actor(cx, actor_id: str, role: str | None = None):
        r = cx.execute('SELECT * FROM users WHERE id=?', (actor_id,)).fetchone()
        if r is None:
            raise HTTPException(401, 'Неизвестный демо-пользователь')
        if role is not None and r['role'] != role:
            raise HTTPException(403, 'Недостаточно прав')
        return r

    def get_booking(cx, booking_id: int, actor_id: str):
        a = actor(cx, actor_id)
        r = cx.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
        if r is None:
            raise HTTPException(404, 'Заявка не найдена')
        if a['role'] == 'student' and r['student_id'] != actor_id:
            raise HTTPException(404, 'Заявка не найдена')
        return r

    def equipment_exists(cx, equipment_id: int):
        r = cx.execute('SELECT * FROM equipment WHERE id=?', (equipment_id,)).fetchone()
        if r is None:
            raise HTTPException(404, 'Оборудование не найдено')
        if r['condition'] != 'ready':
            raise HTTPException(409, 'Оборудование недоступно: требуется обслуживание')
        return r

    def has_overlap(cx, equipment_id: int, start_at: str, end_at: str, excluding: int = -1):
        placeholders = ','.join('?' for _ in ACTIVE)
        r = cx.execute(f'''SELECT id FROM bookings
            WHERE equipment_id=? AND id!=? AND status IN ({placeholders})
            AND start_at<? AND end_at>? LIMIT 1''',
            (equipment_id, excluding, *ACTIVE, end_at, start_at)).fetchone()
        return r is not None

    def check_student_limits(cx, student_id: str, excluding: int = -1):
        pending = cx.execute('''SELECT COUNT(*) AS n FROM bookings
            WHERE student_id=? AND id!=? AND status IN ('pending','needs_info','approved','issued','overdue')''',
            (student_id, excluding)).fetchone()['n']
        if pending >= MAX_ACTIVE:
            raise HTTPException(403, f'Лимит: не более {MAX_ACTIVE} активных заявок')
        late = cx.execute('''SELECT id FROM bookings WHERE student_id=?
            AND status IN ('issued','overdue') AND end_at<=? LIMIT 1''',
            (student_id, iso(now()))).fetchone()
        if late:
            raise HTTPException(403, 'Новые заявки запрещены до возврата просроченной техники')

    def event(cx, booking_id: int, who: str, action: str, note: str | None = None):
        cx.execute('INSERT INTO booking_events(booking_id,actor_id,action,at,note) VALUES (?,?,?,?,?)',
                   (booking_id, who, action, iso(now()), note))

    def as_booking(row):
        return dict(row)

    @app.get('/equipment', tags=['Equipment'], response_model=list[EquipmentOut],
             summary='Получить каталог техники')
    def list_equipment(x_actor_id: str = Header(..., alias='X-Actor-Id'),
                       start_at: datetime | None = Query(None), end_at: datetime | None = Query(None)):
        if (start_at is None) != (end_at is None):
            raise HTTPException(422, 'Укажите оба параметра периода')
        if start_at is not None:
            if start_at.tzinfo is None or end_at.tzinfo is None or start_at.utcoffset() is None or end_at.utcoffset() is None:
                raise HTTPException(422, 'Укажите часовой пояс')
            if end_at <= start_at:
                raise HTTPException(422, 'Некорректный период')
        with db() as cx:
            actor(cx, x_actor_id)
            rows = cx.execute('SELECT * FROM equipment ORDER BY id').fetchall()
            result = []
            current = iso(now())
            for r in rows:
                active = cx.execute("""SELECT 1 FROM bookings WHERE equipment_id=?
                    AND status IN ('issued','overdue') AND issued_at IS NOT NULL AND returned_at IS NULL LIMIT 1""",
                    (r['id'],)).fetchone()
                free = None
                if start_at is not None:
                    free = r['condition']=='ready' and not has_overlap(cx, r['id'], iso(start_at), iso(end_at))
                result.append({**dict(r), 'available_now': r['condition']=='ready' and active is None,
                               'available_for_period': free})
            return result

    @app.get('/equipment/{equipment_id}', tags=['Equipment'], response_model=EquipmentOut,
             summary='Получить информацию об оборудовании')
    def equipment_detail(equipment_id: int, x_actor_id: str = Header(..., alias='X-Actor-Id')):
        with db() as cx:
            actor(cx, x_actor_id)
            r = cx.execute('SELECT * FROM equipment WHERE id=?', (equipment_id,)).fetchone()
            if not r:
                raise HTTPException(404, 'Оборудование не найдено')
            occupied = cx.execute('''SELECT 1 FROM bookings WHERE equipment_id=? AND status IN ('issued','overdue')
                AND returned_at IS NULL LIMIT 1''', (equipment_id,)).fetchone()
            return {**dict(r), 'available_now': r['condition']=='ready' and occupied is None}

    @app.post('/bookings', tags=['Bookings'], response_model=BookingOut, status_code=201,
              summary='Создать заявку на бронирование',
              description='Проверяет лимит, просрочки и пересечение периодов в одной транзакции.')
    def create_booking(body: BookingCreate, x_actor_id: str = Header(..., alias='X-Actor-Id')):
        with db(write=True) as cx:
            actor(cx, x_actor_id, 'student')
            equipment_exists(cx, body.equipment_id)
            check_student_limits(cx, x_actor_id)
            if has_overlap(cx, body.equipment_id, iso(body.start_at), iso(body.end_at)):
                raise HTTPException(409, 'Техника уже забронирована на выбранный период')
            cur = cx.execute('''INSERT INTO bookings
                (student_id,equipment_id,start_at,end_at,purpose,status,created_at)
                VALUES (?,?,?,?,?,?,?)''', (x_actor_id, body.equipment_id, iso(body.start_at),
                iso(body.end_at), body.purpose, 'pending', iso(now())))
            event(cx, cur.lastrowid, x_actor_id, 'created')
            return as_booking(cx.execute('SELECT * FROM bookings WHERE id=?', (cur.lastrowid,)).fetchone())

    @app.get('/bookings', tags=['Bookings'], response_model=list[BookingOut],
             summary='Получить список заявок (свои или все для администратора)')
    def list_bookings(x_actor_id: str = Header(..., alias='X-Actor-Id')):
        with db() as cx:
            a = actor(cx, x_actor_id)
            if a['role']=='admin':
                rows = cx.execute('SELECT * FROM bookings ORDER BY id DESC').fetchall()
            else:
                rows = cx.execute('SELECT * FROM bookings WHERE student_id=? ORDER BY id DESC', (x_actor_id,)).fetchall()
            return [as_booking(r) for r in rows]

    @app.get('/bookings/{booking_id}', tags=['Bookings'], response_model=BookingOut,
             summary='Просмотреть заявку')
    def booking_detail(booking_id: int, x_actor_id: str = Header(..., alias='X-Actor-Id')):
        with db() as cx:
            return as_booking(get_booking(cx, booking_id, x_actor_id))

    @app.patch('/bookings/{booking_id}', tags=['Bookings'], response_model=BookingOut,
               summary='Исправить ожидающую уточнения заявку')
    def update_booking(booking_id: int, body: BookingUpdate,
                       x_actor_id: str = Header(..., alias='X-Actor-Id')):
        with db(write=True) as cx:
            actor(cx, x_actor_id, 'student')
            r = get_booking(cx, booking_id, x_actor_id)
            if r['status'] not in ('pending','needs_info'):
                raise HTTPException(409, 'Изменение возможно только до решения администратора')
            equipment_exists(cx, r['equipment_id'])
            if has_overlap(cx, r['equipment_id'], iso(body.start_at), iso(body.end_at), booking_id):
                raise HTTPException(409, 'Техника уже забронирована на выбранный период')
            cx.execute('''UPDATE bookings SET start_at=?,end_at=?,purpose=?,status='pending',review_comment=NULL
                WHERE id=?''', (iso(body.start_at), iso(body.end_at), body.purpose, booking_id))
            event(cx, booking_id, x_actor_id, 'updated')
            return as_booking(cx.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone())

    @app.post('/bookings/{booking_id}/cancel', tags=['Bookings'], response_model=BookingOut,
              summary='Отменить заявку до выдачи')
    def cancel_booking(booking_id: int, x_actor_id: str = Header(..., alias='X-Actor-Id')):
        with db(write=True) as cx:
            actor(cx, x_actor_id, 'student')
            r = get_booking(cx, booking_id, x_actor_id)
            if r['status'] not in ('pending', 'needs_info', 'approved'):
                raise HTTPException(409, 'Отмена возможна только до выдачи')
            cx.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
            event(cx, booking_id, x_actor_id, 'cancelled')
            return as_booking(cx.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone())

    @app.post('/bookings/{booking_id}/decision', tags=['Administration'], response_model=BookingOut,
              summary='Рассмотреть заявку')
    def decide(booking_id: int, body: DecisionRequest,
               x_actor_id: str = Header(..., alias='X-Actor-Id')):
        with db(write=True) as cx:
            actor(cx, x_actor_id, 'admin')
            r = get_booking(cx, booking_id, x_actor_id)
            if r['status'] not in ('pending','needs_info'):
                raise HTTPException(409, 'Заявка уже обработана')
            if body.decision=='needs_info' and not body.comment:
                raise HTTPException(422, 'Нужно указать, что уточнить')
            if body.decision=='approve':
                if r['status']!='pending':
                    raise HTTPException(409, 'Сначала дождитесь исправления заявки студентом')
                equipment_exists(cx, r['equipment_id'])
                if has_overlap(cx, r['equipment_id'], r['start_at'], r['end_at'], booking_id):
                    raise HTTPException(409, 'Обнаружен конфликт бронирований')
            new_status = {'approve':'approved','reject':'rejected', 'needs_info':'needs_info'}[body.decision]
            cx.execute('UPDATE bookings SET status=?,review_comment=? WHERE id=?',
                       (new_status, body.comment, booking_id))
            event(cx, booking_id, x_actor_id, body.decision, body.comment)
            return as_booking(cx.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone())

    @app.post('/bookings/{booking_id}/issue', tags=['Administration'], response_model=BookingOut,
              summary='Зафиксировать выдачу техники')
    def issue(booking_id: int, x_actor_id: str = Header(..., alias='X-Actor-Id')):
        with db(write=True) as cx:
            actor(cx, x_actor_id, 'admin')
            r = get_booking(cx, booking_id, x_actor_id)
            if r['status']!='approved':
                raise HTTPException(409, 'Выдать можно только подтвержденную заявку')
            if datetime.fromisoformat(r['start_at']) > now()+timedelta(minutes=15):
                raise HTTPException(409, 'Выдача доступна за 15 минут до начала бронирования')
            equipment_exists(cx, r['equipment_id'])
            cx.execute("UPDATE bookings SET status='issued',issued_at=? WHERE id=?", (iso(now()), booking_id))
            event(cx, booking_id, x_actor_id, 'issued')
            return as_booking(cx.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone())

    @app.post('/bookings/{booking_id}/return', tags=['Administration'], response_model=BookingOut,
              summary='Зафиксировать возврат и состояние техники')
    def return_equipment(booking_id: int, body: ReturnRequest,
                         x_actor_id: str = Header(..., alias='X-Actor-Id')):
        with db(write=True) as cx:
            actor(cx, x_actor_id, 'admin')
            r = get_booking(cx, booking_id, x_actor_id)
            if r['status'] not in ('issued', 'overdue'):
                raise HTTPException(409, 'Нельзя вернуть невыданную или уже возвращенную технику')
            cx.execute("""UPDATE bookings SET status='returned',return_condition=?,returned_at=? WHERE id=?""",
                       (body.condition, iso(now()), booking_id))
            if body.condition=='damaged':
                cx.execute("UPDATE equipment SET condition='maintenance' WHERE id=?", (r['equipment_id'],))
            event(cx, booking_id, x_actor_id, 'returned',
                  f'{body.condition}; {body.comment or ""}'.strip())
            return as_booking(cx.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone())

    @app.post('/admin/jobs/overdue-check', tags=['Administration'], response_model=JobOut,
              summary='Запустить проверку просрочек вручную (имитация планировщика)')
    def overdue_check(x_actor_id: str = Header(..., alias='X-Actor-Id')):
        with db(write=True) as cx:
            actor(cx, x_actor_id, 'admin')
            rows = cx.execute('''SELECT id FROM bookings WHERE status='issued' AND end_at<=?''',
                              (iso(now()),)).fetchall()
            for r in rows:
                cx.execute("UPDATE bookings SET status='overdue' WHERE id=?", (r['id'],))
                event(cx, r['id'], 'scheduler', 'overdue', 'Просрочен срок возврата')
            checked = cx.execute("SELECT COUNT(*) AS n FROM bookings WHERE status IN ('issued','overdue')").fetchone()['n']
            return {'checked':checked, 'newly_overdue': len(rows)}

    @app.get('/admin/events', tags=['Administration'], response_model=list[EventOut],
             summary='Получить журнал операций')
    def events(x_actor_id: str = Header(..., alias='X-Actor-Id')):
        with db() as cx:
            actor(cx, x_actor_id, 'admin')
            return [dict(r) for r in cx.execute('SELECT * FROM booking_events ORDER BY id DESC').fetchall()]

    # Explicit common errors in generated OpenAPI and in Swagger UI.
    from fastapi.routing import APIRoute
    for route in app.routes:
        if isinstance(route, APIRoute):
            route.responses.update({
                401: {'description': 'Неизвестный пользователь (демо-идентификация)'},
                403: {'description': 'Недостаточно прав или бизнес-ограничение'},
                404: {'description': 'Ресурс не найден или недоступен текущему пользователю'},
                409: {'description': 'Конфликт состояния или интервалов бронирования'},
            })
    return app


app = create_app()
