import os
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv('DATA_DIR', '/app/data'))
if not DATA_DIR.exists():
    DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv('DB_PATH', str(DATA_DIR / 'bot.db')))
KB_PATH = Path(os.getenv('KB_PATH', str(BASE_DIR / 'knowledge_base.json')))
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
KIE_API_KEY = os.getenv('KIE_API_KEY', '').strip()
KIE_API_URL = os.getenv('KIE_API_URL', 'https://api.kie.ai/gpt-5-2/v1/chat/completions').strip()
ADMIN_ID = int(os.getenv('ADMIN_ID', '0') or '0')
TEACHER_USERNAME = os.getenv('TEACHER_USERNAME', '').strip().lstrip('@')
MEMORY_LIMIT = int(os.getenv('MEMORY_LIMIT', '15'))
if not BOT_TOKEN:
    raise RuntimeError('BOT_TOKEN is not set')
with open(KB_PATH, 'r', encoding='utf-8') as f:
    KB = json.load(f)
router = Router()
bot = Bot(BOT_TOKEN)
MAIN_KB = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text='📅 Записаться на урок'), KeyboardButton(text='🕒 Свободные окна')],
    [KeyboardButton(text='📚 Услуги и стоимость'), KeyboardButton(text='⭐ Отзывы')],
    [KeyboardButton(text='💬 Задать вопрос'), KeyboardButton(text='👤 Связаться с преподавателем')],
], resize_keyboard=True, input_field_placeholder='Выберите действие или напишите вопрос')

# Повторяющееся расписание на 4 недели с 1 сентября 2026 года.
SCHEDULE_START = datetime(2026, 9, 1, 0, 0)
SCHEDULE_DAYS = 28
SCHEDULE_END = SCHEDULE_START + timedelta(days=SCHEDULE_DAYS)
WEEKDAY_NAMES = {0: 'Понедельник', 1: 'Вторник', 2: 'Среда', 3: 'Четверг', 4: 'Пятница', 5: 'Суббота'}
WEEKDAY_SHORT = {0: 'Пн', 1: 'Вт', 2: 'Ср', 3: 'Чт', 4: 'Пт', 5: 'Сб'}
RECURRING_HOURS = {
    0: ['17:00', '18:00'],
    1: ['16:00', '17:00', '18:00', '19:00', '20:30', '21:30'],
    2: ['17:00', '18:00', '20:00'],
    3: ['18:00', '20:00'],
    4: ['18:00', '19:00'],
    5: ['09:00', '14:00', '15:00', '16:00', '17:00', '18:00', '19:00', '20:00', '21:00'],
}

BLOCKED_RECURRING_SLOTS = {(3, '16:00'), (5, '10:30')}

def parse_recurring_time(value):
    raw = str(value).strip()
    if ':' in raw:
        hour_raw, minute_raw = raw.split(':', 1)
    elif '-' in raw:
        hour_raw, minute_raw = raw.split('-', 1)
    else:
        hour_raw, minute_raw = raw, '0'
    return int(hour_raw), int(minute_raw)

def recurring_time_label(value):
    hour, minute = parse_recurring_time(value)
    return f'{hour:02d}:{minute:02d}'

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            starts_at TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'free',
            booked_by INTEGER,
            booked_name TEXT,
            service_code TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS takeover (
            user_id INTEGER PRIMARY KEY,
            until_ts TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS student_profiles (
            user_id INTEGER PRIMARY KEY,
            student_name TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS booking_drafts (
            user_id INTEGER PRIMARY KEY,
            service_code TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        ''')
    sync_recurring_schedule()

def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID

def services():
    return KB['services']

def service_by_code(code):
    return next((s for s in services() if s['code'] == code), None)

def get_student_name(user_id):
    with db() as con:
        row = con.execute(
            'SELECT student_name FROM student_profiles WHERE user_id=?',
            (user_id,)
        ).fetchone()
    return row['student_name'] if row else None

def save_student_name(user_id, student_name):
    with db() as con:
        con.execute(
            'INSERT INTO student_profiles(user_id,student_name,updated_at) VALUES(?,?,?) '
            'ON CONFLICT(user_id) DO UPDATE SET student_name=excluded.student_name, updated_at=excluded.updated_at',
            (user_id, student_name, datetime.now().isoformat(timespec='seconds'))
        )

def set_booking_draft(user_id, service_code):
    with db() as con:
        con.execute(
            'INSERT INTO booking_drafts(user_id,service_code,created_at) VALUES(?,?,?) '
            'ON CONFLICT(user_id) DO UPDATE SET service_code=excluded.service_code, created_at=excluded.created_at',
            (user_id, service_code, datetime.now().isoformat(timespec='seconds'))
        )

def get_booking_draft(user_id):
    with db() as con:
        return con.execute(
            'SELECT service_code FROM booking_drafts WHERE user_id=?',
            (user_id,)
        ).fetchone()

def clear_booking_draft(user_id):
    with db() as con:
        con.execute('DELETE FROM booking_drafts WHERE user_id=?', (user_id,))

def normalize_student_name(text):
    value = ' '.join((text or '').strip().split())
    if not 3 <= len(value) <= 80:
        return None
    words = value.split()
    if not 2 <= len(words) <= 4:
        return None
    if not all(re.fullmatch(r"[A-Za-zА-Яа-яЁё'-]+", word) for word in words):
        return None
    return value

def service_keyboard(prefix='bookservice'):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=s['title'], callback_data=f"{prefix}:{s['code']}")]
        for s in services()
    ])

def format_slot(text):
    dt = datetime.fromisoformat(text)
    wd = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс']
    return f"{wd[dt.weekday()]}, {dt:%d.%m.%Y} · {dt:%H:%M}"

def schedule_datetimes():
    result = []
    for offset in range(SCHEDULE_DAYS):
        day = SCHEDULE_START + timedelta(days=offset)
        for time_value in RECURRING_HOURS.get(day.weekday(), []):
            hour, minute = parse_recurring_time(time_value)
            result.append(day.replace(hour=hour, minute=minute, second=0, microsecond=0))
    return result

def sync_recurring_schedule():
    # Синхронизируем только свободные окна в заданном 4-недельном периоде.
    # Уже занятые записи не затрагиваются.
    expected = [dt.strftime('%Y-%m-%dT%H:%M') for dt in schedule_datetimes()]
    with db() as con:
        con.execute("DELETE FROM slots WHERE status='free' AND strftime('%w', starts_at)='4' AND substr(starts_at, 12, 5)='16:00'")
        con.execute("DELETE FROM slots WHERE status='free' AND strftime('%w', starts_at)='6' AND substr(starts_at, 12, 5)='10:30'")
        con.execute(
            "DELETE FROM slots WHERE status='free' AND starts_at >= ? AND starts_at < ?",
            (SCHEDULE_START.strftime('%Y-%m-%dT%H:%M'), SCHEDULE_END.strftime('%Y-%m-%dT%H:%M'))
        )
        now = datetime.now().isoformat(timespec='seconds')
        con.executemany(
            "INSERT OR IGNORE INTO slots(starts_at,status,created_at) VALUES(?,?,?)",
            [(starts_at, 'free', now) for starts_at in expected]
        )

def series_datetimes(weekday, time_value, future_only=True):
    hour, minute = parse_recurring_time(time_value)
    items = [
        dt for dt in schedule_datetimes()
        if dt.weekday() == weekday and dt.hour == hour and dt.minute == minute
    ]
    if future_only:
        now = datetime.now()
        items = [dt for dt in items if dt >= now]
    return items

def series_rows(weekday, time_value):
    dates = series_datetimes(weekday, time_value, future_only=True)
    if not dates:
        return []
    keys = [dt.strftime('%Y-%m-%dT%H:%M') for dt in dates]
    ph = ','.join('?' for _ in keys)
    with db() as con:
        rows = con.execute(
            f"SELECT * FROM slots WHERE starts_at IN ({ph}) ORDER BY starts_at",
            keys
        ).fetchall()
    return rows

def series_available(weekday, time_value):
    if (weekday, time_value) in BLOCKED_RECURRING_SLOTS:
        return False
    dates = series_datetimes(weekday, time_value, future_only=True)
    rows = series_rows(weekday, time_value)
    return bool(dates) and len(rows) == len(dates) and all(r['status'] == 'free' for r in rows)

def available_times_for_day(weekday):
    return [time_value for time_value in RECURRING_HOURS.get(weekday, []) if series_available(weekday, time_value)]

def series_dates_text(weekday, time_value):
    return ', '.join(dt.strftime('%d.%m') for dt in series_datetimes(weekday, time_value, future_only=True))

def get_free_slots(limit=12):
    with db() as con:
        return con.execute(
            "SELECT * FROM slots WHERE status='free' AND starts_at >= ? ORDER BY starts_at LIMIT ?",
            (datetime.now().strftime('%Y-%m-%dT%H:%M'), limit)
        ).fetchall()

def save_message(uid, role, content):
    with db() as con:
        con.execute(
            'INSERT INTO messages(user_id,role,content,created_at) VALUES(?,?,?,?)',
            (uid, role, content, datetime.now().isoformat(timespec='seconds'))
        )
        rows = con.execute(
            'SELECT id FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?',
            (uid, MEMORY_LIMIT)
        ).fetchall()
        keep = [r['id'] for r in rows]
        if keep:
            ph = ','.join('?' for _ in keep)
            con.execute(f'DELETE FROM messages WHERE user_id=? AND id NOT IN ({ph})', (uid, *keep))

def get_history(uid):
    with db() as con:
        rows = con.execute('SELECT role,content FROM messages WHERE user_id=? ORDER BY id', (uid,)).fetchall()
    return [{'role': r['role'], 'content': r['content']} for r in rows]

def takeover_active(uid):
    with db() as con:
        row = con.execute('SELECT until_ts FROM takeover WHERE user_id=?', (uid,)).fetchone()
        if not row:
            return False
        until = datetime.fromisoformat(row['until_ts'])
        if until <= datetime.now():
            con.execute('DELETE FROM takeover WHERE user_id=?', (uid,))
            return False
        return True

def set_takeover(uid, hours=24):
    until = (datetime.now() + timedelta(hours=hours)).isoformat(timespec='seconds')
    with db() as con:
        con.execute(
            'INSERT INTO takeover(user_id,until_ts) VALUES(?,?) '
            'ON CONFLICT(user_id) DO UPDATE SET until_ts=excluded.until_ts',
            (uid, until)
        )

def clear_takeover(uid):
    with db() as con:
        con.execute('DELETE FROM takeover WHERE user_id=?', (uid,))

def system_prompt():
    service_text = '\n'.join(
        f"- {s['title']}: {s['description']} Длительность: {s['duration']}. Стоимость: {s['price']}."
        for s in services()
    )
    return f'''Ты — вежливый Telegram-администратор преподавателя английского языка проекта Great Opportunities.
Отвечай только на основе базы знаний. Не придумывай цены, скидки, гарантии, расписание и правила.
Если данных нет — предложи уточнить у преподавателя. Отвечай кратко и естественно, без навязчивых продаж.

ФАКТЫ:
{json.dumps(KB['facts'], ensure_ascii=False, indent=2)}

УСЛУГИ:
{service_text}

Если человек хочет записаться — предложи кнопку «📅 Записаться на урок».
Если спрашивает о времени — «🕒 Свободные окна».
Если нужно решение преподавателя — «👤 Связаться с преподавателем».'''

async def ai_answer(uid, text):
    if not KIE_API_KEY:
        return 'Сейчас ИИ-консультант ещё не подключён. Можно выбрать услугу, посмотреть свободные окна или связаться с преподавателем.'
    save_message(uid, 'user', text)
    payload = {
        'messages': [{'role': 'system', 'content': system_prompt()}] + get_history(uid),
        'reasoning_effort': 'low'
    }
    headers = {'Authorization': f'Bearer {KIE_API_KEY}', 'Content-Type': 'application/json'}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(KIE_API_URL, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
        answer = data['choices'][0]['message']['content'].strip()
        save_message(uid, 'assistant', answer)
        return answer
    except Exception:
        return 'Не удалось получить ответ ИИ прямо сейчас. Попробуйте позже или нажмите «👤 Связаться с преподавателем».'

async def notify_admin(text):
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, text)
        except Exception:
            pass

@router.message(CommandStart())
async def start(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    clear_booking_draft(message.from_user.id)
    text = ('Здравствуйте! 👋\n\nЯ персональный ассистент преподавателя английского языка Great Opportunities. '
            'Помогу узнать об уроках, посмотреть свободное время, записаться на занятие или задать вопрос.')
    img = BASE_DIR / 'assets' / 'welcome.jpg'
    if img.exists():
        await message.answer_photo(FSInputFile(img), caption=text, reply_markup=MAIN_KB)
    else:
        await message.answer(text, reply_markup=MAIN_KB)

@router.message(Command('myid'))
async def myid(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(f'Ваш Telegram ID: {message.from_user.id}')

@router.message(Command('clearhistory'))
async def clearhistory(message: Message):
    with db() as con:
        con.execute('DELETE FROM messages WHERE user_id=?', (message.from_user.id,))
    await message.answer('История диалога с ИИ очищена.')

@router.message(Command('addslot'))
async def addslot(message: Message):
    if not is_admin(message.from_user.id):
        return
    raw = message.text.replace('/addslot', '', 1).strip()
    try:
        dt = datetime.strptime(raw, '%d.%m.%Y %H:%M')
    except ValueError:
        await message.answer('Формат: /addslot 25.08.2026 17:00')
        return
    try:
        with db() as con:
            con.execute('INSERT INTO slots(starts_at,status,created_at) VALUES(?,?,?)',
                        (dt.strftime('%Y-%m-%dT%H:%M'), 'free', datetime.now().isoformat(timespec='seconds')))
        await message.answer(f"✅ Добавлено: {format_slot(dt.strftime('%Y-%m-%dT%H:%M'))}")
    except sqlite3.IntegrityError:
        await message.answer('Такой слот уже есть.')

@router.message(Command('delslot'))
async def delslot(message: Message):
    if not is_admin(message.from_user.id):
        return
    raw = message.text.replace('/delslot', '', 1).strip()
    if not raw.isdigit():
        await message.answer('Формат: /delslot 12')
        return
    with db() as con:
        row = con.execute('SELECT * FROM slots WHERE id=?', (int(raw),)).fetchone()
        if not row:
            await message.answer('Слот не найден.')
            return
        con.execute('DELETE FROM slots WHERE id=?', (int(raw),))
    await message.answer(f"🗑 Удалено: {format_slot(row['starts_at'])}")

@router.message(Command('slots'))
async def slots_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    with db() as con:
        rows = con.execute('SELECT * FROM slots ORDER BY starts_at LIMIT 50').fetchall()
    if not rows:
        await message.answer('Слотов пока нет.')
        return
    await message.answer('\n'.join(['Слоты:'] + [f"#{r['id']} · {format_slot(r['starts_at'])} · {r['status']}" for r in rows]))

@router.message(Command('bookings'))
async def bookings(message: Message):
    if not is_admin(message.from_user.id):
        return
    with db() as con:
        rows = con.execute("SELECT * FROM slots WHERE status='booked' ORDER BY starts_at").fetchall()
    if not rows:
        await message.answer('Записей пока нет.')
        return
    out = ['Текущие записи:']
    for r in rows:
        s = service_by_code(r['service_code']) or {'title': r['service_code']}
        out.append(f"#{r['id']} · {format_slot(r['starts_at'])}\n{s['title']} · {r['booked_name']} · TG ID {r['booked_by']}")
    await message.answer('\n\n'.join(out))

@router.message(Command('pause'))
async def pause_user(message: Message):
    if not is_admin(message.from_user.id):
        return
    p = message.text.split()
    if len(p) < 2 or not p[1].isdigit():
        await message.answer('Формат: /pause USER_ID [часы]')
        return
    uid = int(p[1])
    hours = int(p[2]) if len(p) > 2 and p[2].isdigit() else 24
    set_takeover(uid, hours)
    await message.answer(f'ИИ выключен для {uid} на {hours} ч.')

@router.message(Command('resume'))
async def resume_user(message: Message):
    if not is_admin(message.from_user.id):
        return
    p = message.text.split()
    if len(p) < 2 or not p[1].isdigit():
        await message.answer('Формат: /resume USER_ID')
        return
    clear_takeover(int(p[1]))
    await message.answer('ИИ снова активен для клиента.')

@router.message(Command('reply'))
async def reply_user(message: Message):
    if not is_admin(message.from_user.id):
        return
    p = message.text.split(maxsplit=2)
    if len(p) < 3 or not p[1].isdigit():
        await message.answer('Формат: /reply USER_ID текст')
        return
    uid = int(p[1])
    try:
        await bot.send_message(uid, f'Сообщение от преподавателя:\n\n{p[2]}')
        set_takeover(uid, 24)
        await message.answer('✅ Отправлено. ИИ для клиента на паузе 24 часа.')
    except Exception as e:
        await message.answer(f'Не удалось отправить: {e}')

@router.message(F.text == '📚 Услуги и стоимость')
async def show_services(message: Message):
    lines = ['📚 Услуги:']
    for s in services():
        lines.append(f"\n<b>{s['title']}</b>\n{s['description']}\n⏱ {s['duration']}\n💳 {s['price']}")
    await message.answer('\n'.join(lines), parse_mode='HTML', reply_markup=service_keyboard())

async def send_day_picker(message: Message, code):
    s = service_by_code(code)
    if not s:
        await message.answer('Услуга не найдена.')
        return
    days = [wd for wd in RECURRING_HOURS if available_times_for_day(wd)]
    if not days:
        await message.answer('Сейчас свободных регулярных окон нет. Нажмите «👤 Связаться с преподавателем», и мы уточним возможность записи.')
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=WEEKDAY_NAMES[wd], callback_data=f"pickday:{code}:{wd}")]
        for wd in days
    ])
    await message.answer(
        f"Вы выбрали: <b>{s['title']}</b>\n\n"
        "Выберите постоянный день недели. Выбранное время закрепится на 4 недели:",
        parse_mode='HTML',
        reply_markup=kb
    )

@router.message(F.text == '📅 Записаться на урок')
async def book_start(message: Message):
    clear_booking_draft(message.from_user.id)
    await message.answer('Выберите занятие:', reply_markup=service_keyboard())

@router.callback_query(F.data.startswith('bookservice:'))
async def choose_service(callback: CallbackQuery):
    code = callback.data.split(':', 1)[1]
    s = service_by_code(code)
    if not s:
        await callback.answer('Услуга не найдена', show_alert=True)
        return

    student_name = get_student_name(callback.from_user.id)
    if not student_name:
        set_booking_draft(callback.from_user.id, code)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='↩️ Отмена', callback_data='cancelname')
        ]])
        await callback.message.answer(
            "Перед первой записью напиши <b>имя и фамилию ученика</b> одним сообщением.\n\n"
            "Например: <b>Анна Иванова</b>.\n"
            "Ник, эмодзи или только одно имя не подойдут.",
            parse_mode='HTML',
            reply_markup=kb
        )
        await callback.answer()
        return

    await send_day_picker(callback.message, code)
    await callback.answer()

@router.callback_query(F.data.startswith('pickday:'))
async def pick_day(callback: CallbackQuery):
    _, code, weekday_raw = callback.data.split(':', 2)
    weekday = int(weekday_raw)
    s = service_by_code(code)
    if not s:
        await callback.answer('Услуга не найдена', show_alert=True)
        return
    hours = available_times_for_day(weekday)
    if not hours:
        await callback.answer('На этот день свободных регулярных окон уже нет.', show_alert=True)
        return

    buttons = []
    row = []
    for time_value in hours:
        label = recurring_time_label(time_value)
        row.append(InlineKeyboardButton(text=label, callback_data=f'pickseries:{code}:{weekday}:{time_value}'))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text='↩️ Назад к дням', callback_data=f'bookservice:{code}')])
    await callback.message.answer(
        f"<b>{WEEKDAY_NAMES[weekday]}</b>\nВыберите постоянное время:",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()

@router.callback_query(F.data.startswith('pickseries:'))
async def pick_series(callback: CallbackQuery):
    _, code, weekday_raw, time_raw = callback.data.split(':', 3)
    weekday = int(weekday_raw)
    time_value = time_raw
    label = recurring_time_label(time_value)
    s = service_by_code(code)
    if not s or not series_available(weekday, time_value):
        await callback.answer('Это регулярное время уже недоступно.', show_alert=True)
        return

    dates = series_dates_text(weekday, time_value)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='✅ Подтвердить', callback_data=f'confirmseries:{code}:{weekday}:{time_value}'),
        InlineKeyboardButton(text='↩️ Отмена', callback_data='cancel')
    ]])
    await callback.message.answer(
        f"Подтвердить регулярную запись?\n\n"
        f"📘 {s['title']}\n"
        f"🗓 {WEEKDAY_NAMES[weekday]} · {label}\n"
        f"📅 Даты: {dates}\n\n"
        "Это время будет закреплено за учеником на 4 недели.",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data.startswith('confirmseries:'))
async def confirm_series(callback: CallbackQuery):
    _, code, weekday_raw, time_raw = callback.data.split(':', 3)
    weekday = int(weekday_raw)
    time_value = time_raw
    label = recurring_time_label(time_value)
    s = service_by_code(code)
    if not s:
        await callback.answer('Услуга не найдена', show_alert=True)
        return

    dates = series_datetimes(weekday, time_value, future_only=True)
    if not dates:
        await callback.answer('Это время уже недоступно.', show_alert=True)
        return
    keys = [dt.strftime('%Y-%m-%dT%H:%M') for dt in dates]
    ph = ','.join('?' for _ in keys)
    user = callback.from_user
    name = get_student_name(user.id) or (user.full_name or user.username or str(user.id)).strip()

    with db() as con:
        rows = con.execute(
            f"SELECT * FROM slots WHERE starts_at IN ({ph}) ORDER BY starts_at",
            keys
        ).fetchall()
        if len(rows) != len(keys) or any(r['status'] != 'free' for r in rows):
            await callback.answer('К сожалению, это регулярное время только что стало недоступно.', show_alert=True)
            return
        con.execute(
            f"UPDATE slots SET status='booked',booked_by=?,booked_name=?,service_code=? "
            f"WHERE starts_at IN ({ph}) AND status='free'",
            (user.id, name, code, *keys)
        )

    dates_text = ', '.join(dt.strftime('%d.%m') for dt in dates)
    uname = f'@{user.username}' if user.username else 'username не указан'
    await callback.message.answer(
        f"✅ Запись успешно оформлена!\n\n"
        f"📘 {s['title']}\n"
        f"🗓 {WEEKDAY_NAMES[weekday]} · {label}\n"
        f"📅 {dates_text}\n\n"
        "Время закреплено за учеником на 4 недели. Дополнительного подтверждения от преподавателя не требуется.\n\n"
        "Если нужно второе занятие в неделю, нажмите «📅 Записаться на урок» ещё раз и выберите второе постоянное время."
    )
    await notify_admin(
        f"🔔 Новая регулярная запись\n\n"
        f"Имя: {name}\nTelegram: {uname}\nTG ID: {user.id}\n"
        f"Услуга: {s['title']}\n"
        f"Время: {WEEKDAY_NAMES[weekday]} · {label}\n"
        f"Даты: {dates_text}\n\n"
        "✅ Клиенту автоматически отправлено подтверждение записи.\n\n"
        f"Ответить: /reply {user.id} Ваш текст"
    )
    await callback.answer('Запись подтверждена')

# Старые callback-обработчики оставлены для совместимости с ранее отправленными сообщениями.
@router.callback_query(F.data.startswith('pickslot:'))
async def pick_slot(callback: CallbackQuery):
    _, sid, code = callback.data.split(':', 2)
    with db() as con:
        row = con.execute("SELECT * FROM slots WHERE id=? AND status='free'", (int(sid),)).fetchone()
    if not row:
        await callback.answer('Этот слот уже недоступен.', show_alert=True)
        return
    s = service_by_code(code)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Подтвердить', callback_data=f'confirm:{sid}:{code}'), InlineKeyboardButton(text='↩️ Отмена', callback_data='cancel')]])
    await callback.message.answer(f"Подтвердить запись?\n\n📘 {s['title']}\n🕒 {format_slot(row['starts_at'])}", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith('confirm:'))
async def confirm_booking(callback: CallbackQuery):
    _, sid, code = callback.data.split(':', 2)
    user = callback.from_user
    name = get_student_name(user.id) or (user.full_name or user.username or str(user.id)).strip()
    with db() as con:
        row = con.execute("SELECT * FROM slots WHERE id=? AND status='free'", (int(sid),)).fetchone()
        if not row:
            await callback.answer('Этот слот уже занят.', show_alert=True)
            return
        con.execute("UPDATE slots SET status='booked',booked_by=?,booked_name=?,service_code=? WHERE id=? AND status='free'",
                    (user.id, name, code, int(sid)))
    s = service_by_code(code)
    uname = f'@{user.username}' if user.username else 'username не указан'
    await callback.message.answer(f"✅ Заявка принята.\n\n📘 {s['title']}\n🕒 {format_slot(row['starts_at'])}\n\nПреподаватель получил уведомление.")
    await notify_admin(f"🔔 Новая запись\n\nИмя: {name}\nTelegram: {uname}\nTG ID: {user.id}\nУслуга: {s['title']}\nВремя: {format_slot(row['starts_at'])}\n\nОтветить: /reply {user.id} Ваш текст")
    await callback.answer('Запись принята')

@router.callback_query(F.data == 'cancelname')
async def cancel_name(callback: CallbackQuery):
    clear_booking_draft(callback.from_user.id)
    await callback.message.answer('Запись отменена.')
    await callback.answer()

@router.callback_query(F.data == 'cancel')
async def cancel(callback: CallbackQuery):
    clear_booking_draft(callback.from_user.id)
    await callback.message.answer('Запись отменена.')
    await callback.answer()

@router.message(F.text == '🕒 Свободные окна')
async def show_slots(message: Message):
    lines = ['🕒 Свободные регулярные окна на 4 недели с 1 сентября:']
    found = False
    for weekday in RECURRING_HOURS:
        hours = available_times_for_day(weekday)
        if hours:
            found = True
            lines.append(f"\n<b>{WEEKDAY_NAMES[weekday]}</b>: " + ', '.join(recurring_time_label(t) for t in hours))
    if not found:
        await message.answer('Сейчас свободных регулярных окон не опубликовано.')
        return
    lines.append('\nЧтобы закрепить время на 4 недели, нажмите «📅 Записаться на урок».')
    await message.answer('\n'.join(lines), parse_mode='HTML')

@router.message(F.text == '⭐ Отзывы')
async def reviews(message: Message):
    folder = BASE_DIR / 'assets' / 'reviews'
    imgs = sorted([p for p in folder.glob('*') if p.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}])
    if not imgs:
        await message.answer('Отзывы скоро появятся здесь.')
        return
    await message.answer('⭐ Несколько отзывов учеников и родителей:')
    captions = {
        '00 Admission 2026.png': 'Не каждый день три года твоей работы возвращаются к тебе одной фразой: «Я поступила туда, куда мечтала».',
        '00B Admission Plekhanov 2026.png': '«Пришел приказ о зачислении! Спасибо Вам за труд и силы! Это было феерично!»',
    }
    for p in imgs:
        await message.answer_photo(FSInputFile(p), caption=captions.get(p.name))

@router.message(F.text == '👤 Связаться с преподавателем')
async def contact(message: Message):
    u = message.from_user
    uname = f'@{u.username}' if u.username else 'username не указан'
    await notify_admin(f"👤 Клиент просит связаться\n\nИмя: {u.full_name}\nTelegram: {uname}\nTG ID: {u.id}\n\nОтветить: /reply {u.id} Ваш текст")
    if TEACHER_USERNAME:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Открыть профиль преподавателя', url=f'https://t.me/{TEACHER_USERNAME}')]])
        await message.answer('Я уведомил преподавателя. Также можно написать напрямую:', reply_markup=kb)
    else:
        await message.answer('Я передал преподавателю, что вы хотите связаться.')

@router.message(F.text == '💬 Задать вопрос')
async def ask_prompt(message: Message):
    await message.answer('Напишите вопрос обычным сообщением. Я отвечу на основе информации о занятиях и услугах.')

@router.message(F.text)
async def free_text(message: Message):
    if message.chat.type != ChatType.PRIVATE or message.text.startswith('/'):
        return
    uid = message.from_user.id

    draft = get_booking_draft(uid)
    if draft:
        student_name = normalize_student_name(message.text)
        if not student_name:
            await message.answer(
                'Пожалуйста, напиши имя и фамилию ученика словами, например: Анна Иванова.\n'
                'Нужно минимум два слова, без ников, цифр и эмодзи.'
            )
            return
        code = draft['service_code']
        save_student_name(uid, student_name)
        clear_booking_draft(uid)
        await message.answer(f'✅ Спасибо! Запись оформляем на: <b>{student_name}</b>.', parse_mode='HTML')
        await send_day_picker(message, code)
        return

    if takeover_active(uid):
        await notify_admin(f"💬 Сообщение клиента (ИИ на паузе)\n\n{message.from_user.full_name} · TG ID {uid}\n{message.text}\n\nОтветить: /reply {uid} Ваш текст")
        return
    await message.answer(await ai_answer(uid, message.text))

async def main():
    init_db()
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
