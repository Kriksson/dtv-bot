from __future__ import annotations

import asyncio
import json
import logging
import hashlib
import hmac
from math import ceil
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatMemberStatus, ChatType, ContentType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.types import WebAppInfo
from aiohttp import web

from .config import AdminRegistry, Settings, load_settings
from .database import Database

router = Router()
settings: Settings
db: Database
bot_id: int
admins: AdminRegistry
fast_tasks: dict[int, asyncio.Task] = {}
awaiting_channel_add: set[int] = set()


class Draft(StatesGroup):
    text = State()
    media_choice = State()
    media = State()
    channel = State()
    button = State()
    template_name = State()
    channel_search = State()


MENU = ReplyKeyboardMarkup(keyboard=[])


def make_menu() -> ReplyKeyboardMarkup:
    rows = [
    [KeyboardButton(text="Создать пост"), KeyboardButton(text="Шаблоны")],
    [KeyboardButton(text="Добавить канал"), KeyboardButton(text="Список каналов")],
    [KeyboardButton(text="Быстро добавить каналы"), KeyboardButton(text="Проверить права")],
    [KeyboardButton(text="Статистика")],
    ]
    if settings.webapp_url:
        rows.append([KeyboardButton(text="⚡ Быстрая панель", web_app=WebAppInfo(url=settings.webapp_url))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def is_private(message: Message | CallbackQuery) -> bool:
    chat = message.chat if isinstance(message, Message) else message.message.chat
    return chat.type == ChatType.PRIVATE


async def allowed(user_id: int) -> bool:
    return admins.contains(user_id)


async def guard_message(message: Message) -> bool:
    if not is_private(message):
        return False
    if await allowed(message.from_user.id):
        return True
    await message.answer("У вас нет доступа к панели.")
    return False


async def guard_callback(call: CallbackQuery) -> bool:
    if not is_private(call) or not await allowed(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return False
    return True


def channels_kb(rows, page: int, total: int, prefix: str) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=row["title"][:45], callback_data=f"{prefix}:pick:{row['chat_id']}")] for row in rows]
    pages = max(1, ceil(total / 8))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"{prefix}:page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"{prefix}:page:{page + 1}"))
    buttons.append(nav)
    if prefix == "post":
        buttons.append([InlineKeyboardButton(text="🔎 Найти канал", callback_data="post:search:0")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_channel_page(chat_id: int, page: int, prefix: str, message: Message | None = None):
    rows, total = await db.channel_page(max(0, page))
    if not total:
        text = "Нет сохранённых каналов с подтверждёнными правами бота."
        if message:
            await message.edit_text(text)
        else:
            await bot.send_message(chat_id, text, reply_markup=MENU)
        return
    text = "Выберите канал:" if prefix == "post" else "Список каналов:"
    markup = channels_kb(rows, max(0, page), total, prefix)
    if message:
        await message.edit_text(text, reply_markup=markup)
    else:
        await bot.send_message(chat_id, text, reply_markup=markup)


def origin_channel(message: Message):
    origin = getattr(message, "forward_origin", None)
    return getattr(origin, "chat", None) if origin and origin.type == "channel" else None


async def verify_bot_admin(channel_id: int) -> tuple[bool, str]:
    chat = await bot.get_chat(channel_id)
    member = await bot.get_chat_member(channel_id, bot_id)
    ok = member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
    return ok, chat.title or str(channel_id)


@router.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    if not await guard_message(message):
        return
    await message.answer("Панель управления каналами.", reply_markup=MENU)
    if settings.webapp_url:
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚡ Открыть быструю панель", web_app=WebAppInfo(url=settings.webapp_url))]])
        await message.answer("Быстрый поиск и выбор канала:", reply_markup=markup)


@router.message(StateFilter(None), F.text)
async def password_or_menu(message: Message, state: FSMContext):
    if not is_private(message):
        return
    if not await allowed(message.from_user.id):
        await message.answer("У вас нет доступа.")
        return
    if message.from_user.id in awaiting_channel_add and origin_channel(message):
        awaiting_channel_add.discard(message.from_user.id)
        await state.set_state(Draft.channel)
        await add_channel_or_fast(message, state)
        return
    data = await state.get_data()
    if data.get("fast_add") and origin_channel(message):
        await register_fast_channel(message, state)
        return
    if message.text == "Добавить канал":
        await message.answer("Перешлите любое сообщение из канала, где бот является администратором.")
        await state.set_state(Draft.channel)
    elif message.text == "Быстро добавить каналы":
        await state.update_data(fast_add=True, fast_added=[])
        await message.answer(f"Режим включён на {settings.fast_add_timeout} секунд. Пересылайте сообщения из каналов.")
        task = fast_tasks.pop(message.chat.id, None)
        if task: task.cancel()
        fast_tasks[message.chat.id] = asyncio.create_task(fast_timeout(message.chat.id, state))
    elif message.text == "Список каналов":
        await render_channel_page(message.chat.id, 0, "list")
    elif message.text == "Проверить права":
        await message.answer("Проверяю права в фоне. Итог пришлю отдельным сообщением.")
        asyncio.create_task(check_all_channels(message.chat.id))
    elif message.text == "Статистика":
        await message.answer(f"Всего успешных проверок подписки: {await db.click_count()}")
    elif message.text == "Создать пост":
        await state.clear()
        await state.set_state(Draft.text)
        await message.answer("Введите текст поста:", reply_markup=ReplyKeyboardRemove())
    elif message.text == "Шаблоны":
        await show_templates(message)


@router.message(Command("cancel"))
async def cancel_current_flow(message: Message, state: FSMContext):
    """Always let an administrator escape an unfinished FSM step."""
    if not await guard_message(message):
        return
    await state.clear()
    await message.answer("Текущий сценарий отменён.", reply_markup=MENU)


@router.message(F.text.in_({"Создать пост", "Шаблоны", "Добавить канал", "Список каналов", "Быстро добавить каналы", "Проверить права", "Статистика"}))
async def menu_while_in_flow(message: Message, state: FSMContext):
    """A main-menu action must never be interpreted as draft data."""
    if not await guard_message(message):
        return
    await state.clear()
    if message.text == "Список каналов":
        await render_channel_page(message.chat.id, 0, "list")
    elif message.text == "Проверить права":
        await message.answer("Проверяю права в фоне. Итог пришлю отдельным сообщением.")
        asyncio.create_task(check_all_channels(message.chat.id))
    elif message.text == "Статистика":
        await message.answer(f"Всего успешных проверок подписки: {await db.click_count()}")
    else:
        await message.answer("Текущий сценарий отменён. Нажмите выбранный пункт ещё раз.", reply_markup=MENU)


async def fast_timeout(chat_id: int, state: FSMContext):
    try:
        await asyncio.sleep(settings.fast_add_timeout)
        data = await state.get_data()
        if data.get("fast_add"):
            await state.update_data(fast_add=False)
            added = data.get("fast_added", [])
            await bot.send_message(chat_id, "Режим быстрого добавления завершён. Добавлено: " + (", ".join(added) or "ничего"), reply_markup=MENU)
    except asyncio.CancelledError:
        pass


async def register_fast_channel(message: Message, state: FSMContext) -> None:
    channel = origin_channel(message)
    if not channel:
        return
    try:
        ok, title = await verify_bot_admin(channel.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer("Не удалось проверить канал. Убедитесь, что бот добавлен в него.")
        return
    if not ok:
        await message.answer("Бот не является администратором этого канала.")
        return
    await db.add_channel(channel.id, title)
    data = await state.get_data()
    added = data.get("fast_added", [])
    if title not in added:
        added.append(title)
    await state.update_data(fast_added=added)
    await message.answer(f"Добавлен: {title}")


@router.message(Draft.channel)
async def add_channel_or_fast(message: Message, state: FSMContext):
    if not await guard_message(message): return
    channel = origin_channel(message)
    if not channel:
        await message.answer("Нужно переслать сообщение именно из канала.")
        return
    try:
        ok, title = await verify_bot_admin(channel.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer("Не удалось проверить канал. Убедитесь, что бот добавлен в него.")
        return
    if not ok:
        await message.answer("Бот не является администратором этого канала.")
        return
    await db.add_channel(channel.id, title)
    data = await state.get_data()
    if data.get("fast_add"):
        added = data.get("fast_added", [])
        if title not in added: added.append(title)
        await state.update_data(fast_added=added)
        await message.answer(f"Добавлен: {title}")
    else:
        await state.clear()
        await message.answer(f"Канал добавлен: {title}", reply_markup=MENU)


@router.message(Draft.text, F.text)
async def draft_text(message: Message, state: FSMContext):
    if not await guard_message(message): return
    if len(message.text) > 4096:
        await message.answer("Текст длиннее 4096 символов. Сократите его.")
        return
    await state.update_data(text=message.text)
    await state.set_state(Draft.media_choice)
    await message.answer("Добавить медиа?", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Пропустить"), KeyboardButton(text="Добавить медиа")]], resize_keyboard=True, one_time_keyboard=True))


@router.message(Draft.media_choice, F.text == "Пропустить")
async def skip_media(message: Message, state: FSMContext):
    await state.update_data(media=None)
    await state.set_state(Draft.channel)
    await render_channel_page(message.chat.id, 0, "post")


@router.message(Draft.media_choice)
async def request_media(message: Message, state: FSMContext):
    await state.set_state(Draft.media)
    await message.answer("Отправьте фото или видео.", reply_markup=ReplyKeyboardRemove())


@router.message(Draft.media, F.content_type.in_({ContentType.PHOTO, ContentType.VIDEO}))
async def receive_media(message: Message, state: FSMContext):
    media = ("photo", message.photo[-1].file_id) if message.photo else ("video", message.video.file_id)
    await state.update_data(media=media)
    await state.set_state(Draft.channel)
    await render_channel_page(message.chat.id, 0, "post")


@router.callback_query(F.data.startswith("post:"))
async def post_channel_callback(call: CallbackQuery, state: FSMContext):
    if not await guard_callback(call): return
    _, action, value = call.data.split(":", 2)
    if action == "page":
        await render_channel_page(call.message.chat.id, int(value), "post", call.message)
    elif action == "search":
        await state.set_state(Draft.channel_search)
        await call.message.answer("Введите часть названия, @username или ID канала:")
    elif action == "pick":
        row = await db.get_channel(value)
        if not row:
            await call.answer("Канал не найден", show_alert=True); return
        await state.update_data(channel_id=value, channel_title=row["title"])
        await state.set_state(Draft.button)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Без кнопки", callback_data="button:no"), InlineKeyboardButton(text="С кнопкой", callback_data="button:yes")]])
        await call.message.edit_text(f"Канал: {row['title']}\nДобавить кнопку проверки подписки?", reply_markup=kb)
    await call.answer()


@router.message(Draft.channel_search, F.text)
async def search_channel_for_post(message: Message, state: FSMContext):
    if not await guard_message(message):
        return
    query = message.text.strip()
    if len(query) < 2:
        await message.answer("Введите минимум два символа.")
        return
    rows = await db.search_channels(query)
    if not rows:
        await message.answer("Совпадений нет. Повторите запрос или используйте список каналов.")
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=row["title"][:45], callback_data=f"post:pick:{row['chat_id']}")]
        for row in rows
    ])
    await state.set_state(Draft.channel)
    await message.answer(f"Найдено: {len(rows)}. Выберите канал:", reply_markup=markup)


@router.callback_query(F.data.startswith("button:"))
async def button_choice(call: CallbackQuery, state: FSMContext):
    if not await guard_callback(call): return
    await state.update_data(button=call.data.endswith("yes"))
    await preview(call.message.chat.id, state)
    await call.answer()


async def preview(chat_id: int, state: FSMContext):
    data = await state.get_data()
    text = data["text"]
    actions = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Опубликовать", callback_data="publish"), InlineKeyboardButton(text="Сохранить шаблон", callback_data="save_template")]])
    await bot.send_message(chat_id, f"Предпросмотр\nКанал: {data['channel_title']}\nКнопка: {'да' if data.get('button') else 'нет'}\n\n{text}", reply_markup=actions)


@router.callback_query(F.data == "publish")
async def publish(call: CallbackQuery, state: FSMContext):
    if not await guard_callback(call): return
    data = await state.get_data()
    if not data.get("channel_id"):
        await call.answer("Черновик не найден", show_alert=True); return
    try:
        ok, _ = await verify_bot_admin(int(data["channel_id"]))
        if not ok:
            await call.answer("Бот больше не администратор выбранного канала.", show_alert=True)
            return
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Смотреть", callback_data=f"sub:{data['channel_id']}")]]) if data.get("button") else None
        media = data.get("media")
        if media and media[0] == "photo": await bot.send_photo(data["channel_id"], media[1], caption=data["text"], reply_markup=markup)
        elif media: await bot.send_video(data["channel_id"], media[1], caption=data["text"], reply_markup=markup)
        else: await bot.send_message(data["channel_id"], data["text"], reply_markup=markup)
    except Exception:
        logging.exception("Publication failed")
        await call.answer("Не удалось опубликовать. Проверьте права и длину текста.", show_alert=True); return
    await state.clear()
    await call.message.answer("Опубликовано.", reply_markup=MENU)
    await call.answer("Опубликовано")


@router.callback_query(F.data.startswith("sub:"))
async def subscription_check(call: CallbackQuery):
    try:
        channel_id = int(call.data.split(":", 1)[1])
        member = await bot.get_chat_member(channel_id, call.from_user.id)
        subscribed = member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
        if subscribed:
            await db.add_click()
            await call.answer("Благодарим за подписку! Нажмите «HD», чтобы начать просмотр.", show_alert=True)
        else:
            await call.answer("Подпишитесь на канал, чтобы получить доступ к просмотру.", show_alert=True)
    except Exception:
        await call.answer("Не удалось проверить подписку. Попробуйте позже.", show_alert=True)


@router.callback_query(F.data.startswith("list:"))
async def list_callback(call: CallbackQuery):
    if not await guard_callback(call): return
    _, action, value = call.data.split(":", 2)
    if action == "page": await render_channel_page(call.message.chat.id, int(value), "list", call.message)
    else:
        row = await db.get_channel(value)
        if row:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Удалить", callback_data=f"delch:{value}")]])
            await call.message.edit_text(f"{row['title']}\nID: {row['chat_id']}\nПоследняя проверка: {row['checked_at'] or 'нет'}", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("delch:"))
async def delete_channel(call: CallbackQuery):
    if not await guard_callback(call): return
    await db.delete_channel(call.data.split(":", 1)[1])
    await call.message.edit_text("Канал удалён.")
    await call.answer()


async def show_templates(message: Message):
    rows = await db.templates(message.from_user.id)
    if not rows:
        await message.answer("Шаблонов пока нет."); return
    kb = [[InlineKeyboardButton(text=row["name"], callback_data=f"tpl:{row['name']}")] for row in rows]
    await message.answer("Выберите шаблон:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data == "save_template")
async def save_template_start(call: CallbackQuery, state: FSMContext):
    if not await guard_callback(call): return
    await state.set_state(Draft.template_name)
    await call.message.answer("Введите название шаблона (до 40 символов).")
    await call.answer()


@router.message(Draft.template_name, F.text)
async def save_template_name(message: Message, state: FSMContext):
    if not await guard_message(message): return
    name = message.text.strip()
    if not name or len(name) > 40:
        await message.answer("Название должно содержать от 1 до 40 символов."); return
    data = await state.get_data()
    await db.save_template(message.from_user.id, name, {k: data.get(k) for k in ("text", "media", "button")})
    await message.answer("Шаблон сохранён.", reply_markup=MENU)
    await state.clear()


@router.callback_query(F.data.startswith("tpl:"))
async def use_template(call: CallbackQuery, state: FSMContext):
    if not await guard_callback(call): return
    name = call.data.split(":", 1)[1]
    row = await db.template(call.from_user.id, name)
    if not row: await call.answer("Шаблон не найден", show_alert=True); return
    data = json.loads(row["payload"])
    if data.get("media"): data["media"] = tuple(data["media"])
    await state.clear(); await state.update_data(**data); await state.set_state(Draft.channel)
    await render_channel_page(call.message.chat.id, 0, "post")
    await call.answer()


async def check_all_channels(chat_id: int):
    rows = await db.all_channels(); ok = bad = errors = 0
    for row in rows:
        try:
            status, title = await verify_bot_admin(int(row["chat_id"]))
            await db.update_channel_status(row["chat_id"], status, title)
            ok += int(status); bad += int(not status)
        except Exception:
            errors += 1
    await bot.send_message(chat_id, f"Проверка завершена. Доступ есть: {ok}; нет доступа: {bad}; ошибок API: {errors}.", reply_markup=MENU)


async def refresh_imported_titles() -> None:
    """Resolve legacy placeholder titles in the background without blocking polling."""
    rows = await db.all_channels()
    pending = [row for row in rows if row["title"].startswith("Канал -")]
    if not pending:
        return
    limiter = asyncio.Semaphore(6)

    async def refresh(row):
        async with limiter:
            try:
                status, title = await verify_bot_admin(int(row["chat_id"]))
                await db.update_channel_status(row["chat_id"], status, title)
            except Exception:
                logging.debug("Could not refresh channel %s", row["chat_id"], exc_info=True)

    await asyncio.gather(*(refresh(row) for row in pending))


def valid_webapp_user(init_data: str) -> int | None:
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", settings.token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not received_hash or not hmac.compare_digest(expected, received_hash):
        return None
    try:
        return int(json.loads(values["user"])["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


async def web_channels(request: web.Request) -> web.Response:
    user_id = valid_webapp_user(request.headers.get("X-Telegram-Init-Data", ""))
    if user_id is None or not admins.contains(user_id):
        raise web.HTTPForbidden()
    query = request.query.get("q", "").strip()
    rows = await db.search_channels(query, 50) if query else (await db.channel_page(0, 50))[0]
    return web.json_response([{"id": row["chat_id"], "title": row["title"], "checked_at": row["checked_at"]} for row in rows])


async def web_select_channel(request: web.Request) -> web.Response:
    user_id = valid_webapp_user(request.headers.get("X-Telegram-Init-Data", ""))
    if user_id is None or not admins.contains(user_id):
        raise web.HTTPForbidden()
    try:
        channel_id = str((await request.json())["channel_id"])
    except (KeyError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest()
    row = await db.get_channel(channel_id)
    if not row or not row["is_admin"]:
        raise web.HTTPNotFound()
    await bot.send_message(user_id, f"Выбран канал: {row['title']}. Теперь нажмите «Создать пост».", reply_markup=MENU)
    return web.json_response({"title": row["title"]})


async def web_user(request: web.Request) -> int:
    user_id = valid_webapp_user(request.headers.get("X-Telegram-Init-Data", ""))
    if user_id is None or not admins.contains(user_id):
        raise web.HTTPForbidden()
    return user_id


async def web_overview(request: web.Request) -> web.Response:
    await web_user(request)
    _, total = await db.channel_page(0, 1)
    return web.json_response({"channels": total, "clicks": await db.click_count()})


async def web_templates(request: web.Request) -> web.Response:
    user_id = await web_user(request)
    if request.method == "GET":
        return web.json_response([{"name": row["name"], "payload": json.loads(row["payload"])} for row in await db.templates(user_id)])
    data = await request.json()
    name = str(data.get("name", "")).strip()[:40]
    if not name or not isinstance(data.get("payload"), dict):
        raise web.HTTPBadRequest()
    await db.save_template(user_id, name, data["payload"])
    return web.json_response({"ok": True})


async def web_request_add(request: web.Request) -> web.Response:
    user_id = await web_user(request)
    awaiting_channel_add.add(user_id)
    await bot.send_message(user_id, "Перешлите любое сообщение из добавляемого канала. Бот должен быть в нём администратором.")
    return web.json_response({"ok": True})


async def start_webapp() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/api/channels", web_channels)
    app.router.add_post("/api/select-channel", web_select_channel)
    app.router.add_get("/api/overview", web_overview)
    app.router.add_route("*", "/api/templates", web_templates)
    app.router.add_post("/api/request-add-channel", web_request_add)
    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(Path(__file__).parent / "webapp" / "index.html")
    app.router.add_get("/", index)
    app.router.add_static("/", Path(__file__).parent / "webapp", show_index=False)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", settings.webapp_port); await site.start()
    return runner


@router.message(StateFilter(None), F.forward_origin)
async def fast_add_media(message: Message, state: FSMContext):
    """Text forwards are handled by the menu router; media forwards arrive here."""
    if not await guard_message(message):
        return
    if message.from_user.id in awaiting_channel_add and origin_channel(message):
        awaiting_channel_add.discard(message.from_user.id)
        await state.set_state(Draft.channel)
        await add_channel_or_fast(message, state)
        return
    if (await state.get_data()).get("fast_add"):
        await register_fast_channel(message, state)


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()


@router.callback_query()
async def legacy_subscription_check(call: CallbackQuery):
    """Keeps already published posts from the old bot functional after migration."""
    channel_id = await db.legacy_channel(call.data or "")
    if not channel_id:
        await call.answer("Кнопка больше не поддерживается.", show_alert=True)
        return
    try:
        member = await bot.get_chat_member(channel_id, call.from_user.id)
        subscribed = member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
        if subscribed:
            await db.add_click()
            await call.answer("Благодарим за подписку! Нажмите «HD», чтобы начать просмотр.", show_alert=True)
        else:
            await call.answer("Подпишитесь на канал, чтобы получить доступ к просмотру.", show_alert=True)
    except Exception:
        logging.exception("Legacy subscription check failed")
        await call.answer("Не удалось проверить подписку. Попробуйте позже.", show_alert=True)


@router.message(F.web_app_data)
async def selected_in_webapp(message: Message, state: FSMContext):
    if not await guard_message(message):
        return
    try:
        data = json.loads(message.web_app_data.data)
        channel_id = str(data["channel_id"])
        if data["type"] != "select_channel":
            raise ValueError
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        await message.answer("Некорректные данные панели.")
        return
    row = await db.get_channel(channel_id)
    if not row or not row["is_admin"]:
        await message.answer("Канал недоступен.")
        return
    await state.update_data(channel_id=channel_id, channel_title=row["title"])
    await state.set_state(Draft.button)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Без кнопки", callback_data="button:no"), InlineKeyboardButton(text="С кнопкой", callback_data="button:yes")]])
    await message.answer(f"Выбран канал: {row['title']}\nДобавить кнопку проверки подписки?", reply_markup=kb)


async def main() -> None:
    global settings, db, bot_id, admins, MENU
    settings = load_settings()
    db = Database(str(settings.database_path)); await db.connect()
    admins = AdminRegistry(settings.admins_file)
    MENU = make_menu()
    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    bot = Bot(settings.token, session=session)
    globals()["bot"] = bot
    bot_id = (await bot.get_me()).id
    dp = Dispatcher(); dp.include_router(router)
    asyncio.create_task(refresh_imported_titles())
    web_runner = await start_webapp()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await web_runner.cleanup()
        await db.close(); await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
