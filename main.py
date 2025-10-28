import os
import asyncio
import logging
import random
import csv
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from calendar import monthrange

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, KeyboardButton, ReplyKeyboardMarkup,
    CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# -------------------- базовая настройка --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TZ = os.getenv("TZ", "Europe/Moscow")
TZINFO = ZoneInfo(TZ)

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN в .env")

logging.basicConfig(level=logging.INFO)
bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
r = Router()
dp.include_router(r)

DATA_DIR = Path(".")
PARTICIPANTS_CSV = DATA_DIR / "participants.csv"
SETTINGS_CSV = DATA_DIR / "settings.csv"

scheduler = AsyncIOScheduler(timezone=TZINFO)
file_lock = asyncio.Lock()  # защищаем одновременную запись из одного процесса

# -------------------- CSV-хранилище --------------------
PART_FIELDS = ["user_id", "full_name", "postal_code", "address", "created_at"]
SET_FIELDS = ["key", "value"]

PAIRS_CSV = DATA_DIR / "pairs.csv"
PAIR_FIELDS = ["round_id", "giver_id", "giver_name", "receiver_id", "receiver_name", "confirmed", "confirmed_at"]


def _ensure_files_sync():
    if not PARTICIPANTS_CSV.exists():
        with PARTICIPANTS_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=PART_FIELDS).writeheader()
    if not SETTINGS_CSV.exists():
        with SETTINGS_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=SET_FIELDS).writeheader()
    if not PAIRS_CSV.exists():
        with PAIRS_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=["round_id", "giver_id", "giver_name", "receiver_id",
                                          "receiver_name"]).writeheader()


async def ensure_files():
    await asyncio.to_thread(_ensure_files_sync)


def _read_all_participants_sync():
    rows = []
    if not PARTICIPANTS_CSV.exists():
        return rows
    with PARTICIPANTS_CSV.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["user_id"] = int(row["user_id"])
            rows.append(row)
    return rows


async def read_all_participants():
    return await asyncio.to_thread(_read_all_participants_sync)


def _write_all_participants_sync(rows):
    with PARTICIPANTS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PART_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "user_id": int(r["user_id"]),
                "full_name": r["full_name"],
                "postal_code": r["postal_code"],
                "address": r["address"],
                "created_at": r["created_at"],
            })


async def write_all_participants(rows):
    await asyncio.to_thread(_write_all_participants_sync, rows)


async def find_participant(user_id: int):
    rows = await read_all_participants()
    for r in rows:
        if r["user_id"] == user_id:
            return r
    return None


async def add_participant(user_id: int, full_name: str, postal_code: str, address: str, created_at: str):
    async with file_lock:
        rows = await read_all_participants()
        if any(r["user_id"] == user_id for r in rows):
            return False
        rows.append({
            "user_id": user_id,
            "full_name": full_name,
            "postal_code": postal_code,
            "address": address,
            "created_at": created_at,
        })
        await write_all_participants(rows)
    return True


async def delete_participant(user_id: int):
    async with file_lock:
        rows = await read_all_participants()
        new_rows = [r for r in rows if r["user_id"] != user_id]
        await write_all_participants(new_rows)


async def count_participants() -> int:
    rows = await read_all_participants()
    return len(rows)


def _read_settings_sync():
    data = {}
    if not SETTINGS_CSV.exists():
        return data
    with SETTINGS_CSV.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            data[row["key"]] = row["value"]
    return data


async def read_settings():
    return await asyncio.to_thread(_read_settings_sync)


def _write_settings_sync(data: dict):
    with SETTINGS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SET_FIELDS)
        writer.writeheader()
        for k, v in data.items():
            writer.writerow({"key": k, "value": v})


async def write_settings(data: dict):
    await asyncio.to_thread(_write_settings_sync, data)


async def get_setting(key: str) -> str | None:
    data = await read_settings()
    return data.get(key)


async def set_setting(key: str, value: str):
    async with file_lock:
        data = await read_settings()
        data[key] = value
        await write_settings(data)


# -------------------- FSM анкеты --------------------
class Form(StatesGroup):
    full_name = State()
    postal_code = State()
    address = State()


main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Хочешь поучаствовать? Урааа 😳💞")],
        [KeyboardButton(text="Мой статус"), KeyboardButton(text="Удалить мои данные")],
        [KeyboardButton(text="Помощь")],
    ],
    resize_keyboard=True
)


# -------------------- утилиты --------------------
def is_admin(m: Message) -> bool:
    return bool(ADMIN_ID) and m.from_user and m.from_user.id == ADMIN_ID


# -------------------- обработчики пользователя --------------------
@r.message(CommandStart())
async def cmd_start(m: Message):
    draw_at = await get_setting("draw_at")
    text = (
        "Привееет 💌✨\n\n"
        "Это пикми-почта — ну такая скромная, но очень милая штучка, где люди отправляют друг другу добрые письма. "
        "Жмякни «Заполнить анкету» и расскажи о себе чуточку — совсем-совсем немного: ФИО, индекс и адрес.\n\n"
        "Когда придёт время, я так тихонечко выберу, кому ты отправишь послание, и пришлю тебе адрес этого человечка... ну если тебе не сложно 📮💖"
        f"Текущая дата жеребьёвки: <b>{draw_at or 'не назначена'}</b> ({TZ})."
    )
    await m.answer(text, reply_markup=main_kb)


@r.message(F.text == "Помощь")
@r.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        'Хочешь подсказки? '
        'Лови, сладенький(ая) 🍬\n\n'

        '• /start — я красиво представлюсь 💌  \n'
        '• «Хочешь поучаствовать? Урааа 😳💞» — вписать себя в пикми-почту \n'
        '• «Мой статус» — посмотреть, что я храняю о тебе (аккуратно и с любовью) \n'
        '• «Удалить мои данные» — ну… если ты вдруг захотел(а) уйти. Я переживу 😿\n\n'

        "Админ-команды:\n"
        "• /set_draw — выбрать дату, когда начнётся обменчик\n"
        "• /draw_now — запустить всё прямо сейчас (как будто я не готовилась, ну ладно 🥺)\n"
        "• /count — узнать, сколько нас тут таких мягеньких 💞"
    )


@r.message(F.text == "Хочешь поучаствовать? Урааа 😳💞")
async def form_start(m: Message, state: FSMContext):
    if await find_participant(m.from_user.id):
        await m.answer("Эмм… кажется, ты уже тут есть 😳 \n"
                       "Можешь глянуть «Мой статус»… или стереть всё, если я вдруг тебе наскучила 🥺💘")
        return
    await state.set_state(Form.full_name)
    await m.answer(
        "Тогда напиши, пожалуйста, свои ФИО полностью (ну, чтобы письмо точно дошло, вдруг почтальон придирчивый 😌)")


@r.message(Form.full_name)
async def form_full_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if len(name.split()) < 2:
        await m.answer("Ой, кажется, где-то пропущено имя или фамилия 😿.\n"
                       "Напиши полностью, пожалуйста, чтобы всё было аккуратно и правильно 💗")
        return
    await state.update_data(full_name=name)
    await state.set_state(Form.postal_code)
    await m.answer("Теперь индекс!\n"
                   "Циферки, пожалуйста, чтобы твоя пикми-почта не потерялась в этом огромном мире 📮✨")


@r.message(Form.postal_code)
async def form_postal(m: Message, state: FSMContext):
    code = m.text.strip().replace(" ", "")
    if not code.isdigit() or not (4 <= len(code) <= 10):
        await m.answer("Кажется, индекс не похож на настоящий… \n"
                       "А можно ещё раз? Я знаю, ты умничка, у тебя всё выйдет 💕")
        return
    await state.update_data(postal_code=code)
    await state.set_state(Form.address)
    await m.answer("И остался самый-самый финальный штришок — напиши полный адрес! "
                   "\nУлица, дом, квартира, город… ну всё как у больших почт 🏡📬"
                   "\n(я правда очень постараюсь ничего не перепутать 😳)")


@r.message(Form.address)
async def form_address(m: Message, state: FSMContext):
    addr = m.text.strip()
    if len(addr) < 10:
        await m.answer("Ой, адрес такой маленький… как будто ты ничего не хочешь мне рассказывать 😿\n"
                       "Можно чуть-чуть подробнее? Я же хочу, чтобы письмо точно дошло 💌")
        return

    data = await state.get_data()
    ok = await add_participant(
        user_id=m.from_user.id,
        full_name=data["full_name"],
        postal_code=data["postal_code"],
        address=addr,
        created_at=datetime.now(TZINFO).isoformat(),
    )
    await state.clear()
    if ok:
        await m.answer("Ого, ты заполнил(а) всё так аккуратно 😳💘 \n"
                       "Ты в списке пикми-почты! "
                       "\nМожешь нажать «Мой статус», чтобы посмотреть, как всё миленько сохранилось ✨")
    else:
        await m.answer("Ооо, ты уже был(а) здесь… \n"
                       "Ну я не обижаюсь 😳\n"
                       "Посмотри «Мой статус», там всё аккуратно лежит 🌸")


@r.message(F.text == "Мой статус")
async def my_status(m: Message):
    row = await find_participant(m.from_user.id)
    if not row:
        await m.answer("Ой… тебя ещё нет в списке 😿\n"
                       "Но это можно исправить — просто нажми «Заполнить анкету» 💖")
        return
    await m.answer(
        "<b>Глянь, что я бережно записала о тебе 💌</b>\n"
        f"ФИО: {row['full_name']}\n"
        f"Индекс: {row['postal_code']}\n"
        f"Адрес: {row['address']}\n"
        f"Когда: {row['created_at']}\n"
        f"Всё такое официальное, даже страшно… а вдруг ты подумаешь, что я слишком старалась 🥺"
    )


@r.message(F.text == "Удалить мои данные")
async def delete_me(m: Message):
    await delete_participant(m.from_user.id)
    await m.answer("Ты… хочешь удалить свои данные?\n"
                   "Ну, ладно 😿 я не обижаюсь…\n"
                   "Если вдруг передумаешь — я буду здесь и тихонечко ждать 💌")


# -------------------- жеребьёвка --------------------
def make_derangement(ids: list[int]) -> list[int]:
    shuffled = ids[:]
    random.shuffle(shuffled)
    return shuffled[1:] + shuffled[:1]


async def run_draw_and_notify():
    rows = await read_all_participants()
    if len(rows) < 2:
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, "Жеребьёвка не выполнена: участников меньше 2.")
        return

    idxs = list(range(len(rows)))
    mapping = make_derangement(idxs)

    # --- сохраняем пары этого раунда
    round_id = datetime.now(TZINFO).isoformat()
    pairs_to_save = []
    for i, giver_idx in enumerate(idxs):
        receiver_idx = mapping[i]
        giver = rows[giver_idx]
        recv = rows[receiver_idx]
        pairs_to_save.append({
            "round_id": round_id,
            "giver_id": int(giver["user_id"]),
            "giver_name": giver["full_name"],
            "receiver_id": int(recv["user_id"]),
            "receiver_name": recv["full_name"],
            "confirmed": "0",
            "confirmed_at": "",
        })
    async with file_lock:
        await append_pairs(pairs_to_save)

    # --- отправляем дарителям данные получателей + кнопку подтверждения
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    for i, giver_idx in enumerate(idxs):
        receiver_idx = mapping[i]
        giver = rows[giver_idx]
        recv = rows[receiver_idx]

        kb = InlineKeyboardBuilder()
        # в callback кладём round_id и giver_id — по ним найдём receiver
        kb.button(text="✅ Я отправил(а) сообщение", callback_data=f"sent:{round_id}:{int(giver['user_id'])}")
        kb.adjust(1)

        text = (
            "🎁 <b>💌 Твоя пикми-почта готова!\n"
            "Вот кому ты пишешь… только тсс, это тихая магия ✨</b>\n\n"
            f"<b>ФИО:</b> {recv['full_name']}\n"
            f"<b>Индекс:</b> {recv['postal_code']}\n"
            f"<b>Адрес:</b> {recv['address']}\n\n"
            "Как только напишешь получателю, жмякни кнопочку снизу 😳 Я тихонечко шепну, что письмо уже в пути 💌✨"
        )
        try:
            await bot.send_message(int(giver["user_id"]), text, reply_markup=kb.as_markup())
        except Exception as e:
            logging.exception(f"Ой-ой… сообщение не ушло 😿  {giver['user_id']}: {e}\n"
                              "Наверное, интернет обиделся… попробуем позже? 🙈 ")

    if ADMIN_ID:
        await bot.send_message(ADMIN_ID, "Жеребьёвка завершена и участники уведомлены ✅")


# -------------------- админ-команды --------------------
@r.message(Command("count"))
async def cmd_count(m: Message):
    if not is_admin(m):
        return
    n = await count_participants()
    await m.answer(f"Участников сейчас: <b>{n}</b>")


# --- календарь/время ---
class DrawFSM(StatesGroup):
    date = State()  # YYYY-MM-DD
    hour = State()  # 0..23
    minute = State()  # 0,15,30,45


CAL_PREFIX = "cal"


def _build_calendar_kb(year: int, month: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    kb.row(
        InlineKeyboardButton(text="«", callback_data=f"{CAL_PREFIX}:nav:{prev_y}:{prev_m}"),
        InlineKeyboardButton(text=f"{year}-{month:02d}", callback_data=f"{CAL_PREFIX}:noop"),
        InlineKeyboardButton(text="»", callback_data=f"{CAL_PREFIX}:nav:{next_y}:{next_m}"),
    )
    kb.row(*(InlineKeyboardButton(text=t, callback_data=f"{CAL_PREFIX}:noop") for t in
             ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]))

    first_weekday, days_in_month = monthrange(year, month)  # Mon=0..Sun=6
    pad = (first_weekday - 0) % 7
    cells = ["" for _ in range(pad)] + [f"{d}" for d in range(1, days_in_month + 1)]
    while len(cells) % 7 != 0:
        cells.append("")

    for i in range(0, len(cells), 7):
        row = []
        for c in cells[i:i + 7]:
            if c:
                row.append(InlineKeyboardButton(
                    text=f"{int(c):02d}",
                    callback_data=f"{CAL_PREFIX}:day:{year}-{month:02d}-{int(c):02d}"
                ))
            else:
                row.append(InlineKeyboardButton(text=" ", callback_data=f"{CAL_PREFIX}:noop"))
        kb.row(*row)

    return kb.as_markup()


def _build_hours_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for h in range(0, 24):
        kb.add(InlineKeyboardButton(text=f"{h:02d}", callback_data=f"{CAL_PREFIX}:hour:{h}"))
    kb.adjust(6, 6, 6, 6)
    return kb.as_markup()


def _build_minutes_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for m in (0, 15, 30, 45):
        kb.add(InlineKeyboardButton(text=f"{m:02d}", callback_data=f"{CAL_PREFIX}:minute:{m}"))
    kb.row(InlineKeyboardButton(text="↩ Назад (часы)", callback_data=f"{CAL_PREFIX}:back_hours"))
    return kb.as_markup()


def _build_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"{CAL_PREFIX}:confirm"),
        InlineKeyboardButton(text="↩ Изменить минуты", callback_data=f"{CAL_PREFIX}:back_minutes"),
    )
    return kb.as_markup()


@r.callback_query(F.data.startswith("cal:"))
async def on_calendar_callbacks(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("Недоступно", show_alert=True)
        return

    parts = (cq.data or "").split(":")
    if len(parts) < 2:
        await cq.answer()
        return

    if parts[1] == "noop":
        await cq.answer()
        return

    if parts[1] == "nav":
        year, month = int(parts[2]), int(parts[3])
        await cq.message.edit_reply_markup(reply_markup=_build_calendar_kb(year, month))
        await cq.answer()
        return

    if parts[1] == "day":
        date_str = parts[2]
        await state.update_data(date=date_str)
        await state.set_state(DrawFSM.hour)
        await cq.message.edit_text(f"Дата: <b>{date_str}</b>\nВыбери час (0–23):", reply_markup=_build_hours_kb())
        await cq.answer()
        return

    if parts[1] == "hour":
        hour = int(parts[2])
        data = await state.get_data()
        if "date" not in data:
            await cq.answer("Сначала выбери дату", show_alert=True)
            return
        await state.update_data(hour=hour)
        await state.set_state(DrawFSM.minute)
        await cq.message.edit_text(
            f"Дата: <b>{data['date']}</b>\nЧас: <b>{hour:02d}</b>\nВыбери минуты:",
            reply_markup=_build_minutes_kb()
        )
        await cq.answer()
        return

    if parts[1] == "minute":
        minute = int(parts[2])
        data = await state.get_data()
        if "date" not in data or "hour" not in data:
            await cq.answer("Сначала выбери дату и час", show_alert=True)
            return
        await state.update_data(minute=minute)
        await state.set_state(DrawFSM.minute)
        await cq.message.edit_text(
            f"Дата: <b>{data['date']}</b>\nЧас: <b>{data['hour']:02d}</b>\nМинуты: <b>{minute:02d}</b>\nПодтвердить?",
            reply_markup=_build_confirm_kb()
        )
        await cq.answer()
        return

    if parts[1] == "back_hours":
        data = await state.get_data()
        await state.set_state(DrawFSM.hour)
        await cq.message.edit_text(
            f"Дата: <b>{data.get('date', '—')}</b>\nВыбери час (0–23):",
            reply_markup=_build_hours_kb()
        )
        await cq.answer()
        return

    if parts[1] == "back_minutes":
        data = await state.get_data()
        await state.set_state(DrawFSM.minute)
        await cq.message.edit_text(
            f"Дата: <b>{data.get('date', '—')}</b>\nЧас: <b>{int(data.get('hour', 0)):02d}</b>\nВыбери минуты:",
            reply_markup=_build_minutes_kb()
        )
        await cq.answer()
        return

    if parts[1] == "confirm":
        data = await state.get_data()
        try:
            y, m, d = map(int, data["date"].split("-"))
            h = int(data["hour"])
            mm = int(data["minute"])
        except Exception:
            await cq.answer("Не хватает данных: выбери дату/время заново", show_alert=True)
            return

        when_local = datetime(y, m, d, h, mm, tzinfo=TZINFO)
        await set_setting("draw_at", when_local.isoformat())

        for job in scheduler.get_jobs():
            job.remove()
        scheduler.add_job(run_draw_and_notify, trigger=DateTrigger(run_date=when_local))

        await state.clear()
        await cq.message.edit_text(f"Дата жеребьёвки установлена: <b>{when_local}</b>")
        await cq.answer("Сохранено")
        return


# -------------------- ручной запуск жеребьёвки --------------------
@r.message(Command("draw_now"))
async def cmd_draw_now(m: Message):
    if not is_admin(m):
        return
    await m.answer("Стартую жеребьёвку прямо сейчас…")
    await run_draw_and_notify()


# -------------------- эхо --------------------
@r.message()
async def echo(m: Message):
    await m.answer("Эмм… я немножко запуталась 😳 \n"
                   "Можешь нажать кнопочки или /help, если хочешь, чтобы я выглядела умненькой 💕")


# -------------------- пары ----------------------
def _read_all_pairs_sync():
    rows = []
    if not PAIRS_CSV.exists():
        return rows
    with PAIRS_CSV.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["giver_id"] = int(row["giver_id"])
            row["receiver_id"] = int(row["receiver_id"])
            row["confirmed"] = row["confirmed"] == "1"
            rows.append(row)
    return rows


async def read_all_pairs():
    return await asyncio.to_thread(_read_all_pairs_sync)


def _append_pairs_sync(items: list[dict]):
    exists = PAIRS_CSV.exists()
    with PAIRS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PAIR_FIELDS)
        if not exists:
            writer.writeheader()
        for it in items:
            writer.writerow(it)


async def append_pairs(items: list[dict]):
    await asyncio.to_thread(_append_pairs_sync, items)


def _write_all_pairs_sync(rows: list[dict]):
    with PAIRS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PAIR_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "round_id": r["round_id"],
                "giver_id": int(r["giver_id"]),
                "giver_name": r["giver_name"],
                "receiver_id": int(r["receiver_id"]),
                "receiver_name": r["receiver_name"],
                "confirmed": "1" if r.get("confirmed") else "0",
                "confirmed_at": r.get("confirmed_at", ""),
            })


@r.callback_query(F.data.startswith("sent:"))
async def on_sent_confirm(cq: CallbackQuery):
    # callback: sent:<round_id>:<giver_id>
    try:
        payload = (cq.data or "")[len("sent:"):]  # убрали префикс "sent:"
        round_id, giver_id_str = payload.rsplit(":", 1)  # <-- ключевая правка
        giver_id = int(giver_id_str)
    except Exception:
        await cq.answer("Некорректные данные", show_alert=True)
        return

    # защита: кнопка должна работать только у самого дарителя
    if cq.from_user.id != giver_id:
        await cq.answer("Ой… эта кнопочка не твоя 😬\n"
                        "Не сердись, ладно? Я просто должна быть справедливой 🥺", show_alert=True)
        return

    pairs = await read_all_pairs()
    updated = False
    receiver_id = None

    # найдём запись и отметим подтверждение, если ещё не было
    for p in pairs:
        if p["round_id"] == round_id and p["giver_id"] == giver_id:
            if p.get("confirmed"):
                await cq.answer("Ты уже нажимал(а) 😳\n"
                                "Я всё помню, честно-честно 💘")
                return
            p["confirmed"] = True
            p["confirmed_at"] = datetime.now(TZINFO).isoformat()
            receiver_id = int(p["receiver_id"])
            updated = True
            break

    if not updated or receiver_id is None:
        await cq.answer("Пара не найдена", show_alert=True)
        return

    # перезапишем pairs.csv
    async with file_lock:
        # простая перезапись всего файла
        await asyncio.to_thread(lambda rows=pairs: _write_all_pairs_sync(rows))

    await cq.answer("Урааа 😭💗 \n"
                    "Ты подтвердил(а), я уже сказала получателю, что письмо в пути!\n"
                    "Горжусь тобой немножко… но не показываю 🫣")

    # уведомим получателя (анонимно)
    try:
        await bot.send_message(
            receiver_id,
            "📬 Кажется, твоя пикми-почта уже в пути!\n"
            "Та-дам: отправитель подтвердил, что написал тебе письмо 💕✨\n"
            "Смотри в ящик, вдруг придёт что-то очень милое 😊"
        )
    except Exception as e:
        logging.exception(f"Эх… не получилось сказать получателю 😿\n"
                          "Я правда старалась… можно позже ещё раз? 💌")


# -------------------- запуск --------------------
async def on_startup():
    await ensure_files()
    draw_at = await get_setting("draw_at")
    if draw_at:
        try:
            when = datetime.fromisoformat(draw_at)
            if when > datetime.now(TZINFO):
                scheduler.add_job(run_draw_and_notify, trigger=DateTrigger(run_date=when))
        except Exception:
            logging.exception("Не удалось восстановить расписание жеребьёвки")


async def main():
    await on_startup()
    if not scheduler.running:
        scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
