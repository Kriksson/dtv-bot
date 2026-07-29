# Copyright (c) 2026 Kriksson. All rights reserved.

import os
import argparse
import json
import time
import tempfile
import hashlib
import hmac
import threading
from datetime import datetime, timedelta
from collections import Counter
from urllib.parse import urlparse

from dotenv import load_dotenv
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not TOKEN:
    raise RuntimeError("Установите переменную окружения TELEGRAM_BOT_TOKEN")

def _get_startup_args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--proxy",
        default=os.getenv("TELEGRAM_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"),
        help="Telegram API proxy URL, e.g. http://user:pass@host:port or socks5://user:pass@host:port",
    )
    args, _ = parser.parse_known_args()
    return args


def _format_proxy_for_log(proxy_url):
    parsed = urlparse(proxy_url)
    host = parsed.hostname or parsed.netloc or "<unknown>"
    port = f":{parsed.port}" if parsed.port else ""
    scheme = f"{parsed.scheme}://" if parsed.scheme else ""
    return f"{scheme}{host}{port}"


startup_args = _get_startup_args()
if startup_args.proxy:
    telebot.apihelper.proxy = {
        "http": startup_args.proxy,
        "https": startup_args.proxy,
    }
    print(f"Telegram proxy enabled: {_format_proxy_for_log(startup_args.proxy)}")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

CHANNELS_FILE = "channels.txt"
STATS_FILE = "stats.txt"
BUTTONS_FILE = "buttons.json"
TEMPLATES_FILE = "templates.json"

for path in (CHANNELS_FILE, STATS_FILE):
    if not os.path.exists(path):
        open(path, "w", encoding="utf-8").close()

hash_map = {}
hash_lock = threading.RLock()

user_page = {}
channels_list_page = {}
post_data = {}

channel_cache = {}
CACHE_TTL = 300

QUICK_ADD_TIMEOUT = 120
quick_mode = {}
quick_mode_lock = threading.RLock()

# ===================== AUTH START =====================
# Пароль берём из переменной окружения, иначе — из дефолта (смените!)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    raise RuntimeError("Set the ADMIN_PASSWORD environment variable before starting the bot")

# Авторизованные пользователи (память процесса — сбрасывается при рестарте)
authorized_users = set()
# Ожидание ввода пароля (для register_next_step_handler не мешаем остальным шагам)
awaiting_password = set()
auth_lock = threading.RLock()

def is_private_chat(message_or_call) -> bool:
    try:
        chat = message_or_call.chat if hasattr(message_or_call, "chat") else message_or_call.message.chat
        return getattr(chat, "type", "") == "private"
    except Exception:
        return False

def is_authorized(user_id: int) -> bool:
    with auth_lock:
        return user_id in authorized_users

def start_password_flow(chat_id: int, user_id: int):
    with auth_lock:
        awaiting_password.add(user_id)
    msg = bot.send_message(chat_id, "🔐 Доступ ограничен. Введите пароль для авторизации:")
    bot.register_next_step_handler(msg, handle_password_input)

def handle_password_input(message):
    uid = message.from_user.id
    txt = (message.text or "").strip()
    with auth_lock:
        waiting = uid in awaiting_password
    if not waiting:
        # Кто-то прислал что-то не в контексте пароля — отдадим на общий роутинг
        return route_public_message_or_prompt_auth(message)
    if hmac.compare_digest(txt, ADMIN_PASSWORD):
        with auth_lock:
            awaiting_password.discard(uid)
            authorized_users.add(uid)
        bot.send_message(message.chat.id, "Доступ разрешён.", reply_markup=main_menu())
    else:
        # Не снимаем флаг awaiting — просим повторить
        msg = bot.send_message(message.chat.id, "Неверный пароль (введи еще раз)")
        bot.register_next_step_handler(msg, handle_password_input)

def require_auth_msg(handler_func):
    """Декоратор для message handlers: в личке просим пароль, в группах пропускаем (но мы и так работаем в личке)."""
    def wrapper(message, *args, **kwargs):
        if is_private_chat(message) and not is_authorized(message.from_user.id):
            return start_password_flow(message.chat.id, message.from_user.id)
        return handler_func(message, *args, **kwargs)
    return wrapper

def require_auth_cb(handler_func):
    """Декоратор для callback handlers: разрешаем кнопки постов (hash_map) без авторизации,
    остальное в личке — по паролю."""
    def wrapper(call, *args, **kwargs):
        # Кнопки на постах (хэши) — всегда доступны
        if call.data in hash_map:
            return handler_func(call, *args, **kwargs)
        # Всё остальное — если это не личка, просто игнорируем; если личка — проверяем пароль
        if is_private_chat(call) and not is_authorized(call.from_user.id):
            start_password_flow(call.message.chat.id, call.from_user.id)
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            return
        return handler_func(call, *args, **kwargs)
    return wrapper

def route_public_message_or_prompt_auth(message):
    """Если пользователь не авторизован и пишет в личку — всегда предлагаем ввести пароль.
    Это обеспечивает «авторизацию без команд»."""
    if is_private_chat(message) and not is_authorized(message.from_user.id):
        return start_password_flow(message.chat.id, message.from_user.id)
    # Иначе ничего не делаем — падает в другие хендлеры/или игнор
# ===================== AUTH END =====================

def _quick_cancel_timer(chat_id: int):
    state = quick_mode.get(chat_id)
    if state and state.get("timer"):
        try:
            state["timer"].cancel()
        except Exception:
            pass
        state["timer"] = None

def _quick_arm_timer(chat_id: int, bot_send):
    _quick_cancel_timer(chat_id)
    def _auto_off():
        with quick_mode_lock:
            st = quick_mode.get(chat_id, {})
            if st.get("on"):
                st["on"] = False
        _quick_summary(chat_id, bot_send, auto=True)
    t = threading.Timer(QUICK_ADD_TIMEOUT, _auto_off)
    quick_mode.setdefault(chat_id, {})["timer"] = t
    t.daemon = True
    t.start()

def quick_on(chat_id: int, bot_send):
    with quick_mode_lock:
        quick_mode[chat_id] = {"on": True, "added": set(), "timer": None}
    _quick_arm_timer(chat_id, bot_send)

def quick_off(chat_id: int, bot_send, show_summary=True):
    with quick_mode_lock:
        st = quick_mode.get(chat_id)
        if not st:
            return
        st["on"] = False
    _quick_cancel_timer(chat_id)
    if show_summary:
        _quick_summary(chat_id, bot_send, auto=False)

def quick_is_on(chat_id: int) -> bool:
    return bool(quick_mode.get(chat_id, {}).get("on"))

def quick_added(chat_id: int) -> set:
    return set(quick_mode.get(chat_id, {}).get("added", set()))

def _quick_summary(chat_id: int, bot_send, auto: bool):
    added = list(quick_added(chat_id))
    if added:
        lines = "\n".join(f"• {get_channel_link(ch)}" for ch in added)
        txt = ("⏱ Режим быстрого добавления выключен по таймауту.\n\n" 
               "Добавлено:\n" + lines) if auto else ("⏹ Режим быстрого добавления выключен.\n\nДобавлено:\n" + lines)
    else:
        txt = "⏹ Режим быстрого добавления выключен. Ничего не добавлено."
    try:
        bot_send(chat_id, txt, reply_markup=main_menu())
    except Exception:
        pass

def _quick_controls_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⏹ Выключить", callback_data="quick_off"))
    return kb

def md5_hash(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()

def atomic_write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)

def save_buttons():
    """Сохраняем словарь хэшей в новом формате."""
    with hash_lock:
        atomic_write_json(BUTTONS_FILE, hash_map)

def load_buttons():
    """
    Загружаем кнопки. Поддерживаем старый формат:
    {hash: (ch_id, url, button_text)} -> {hash: {"ch": ch_id}}
    """
    global hash_map
    if os.path.exists(BUTTONS_FILE):
        try:
            with open(BUTTONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                res = {}
                if isinstance(data, dict):
                    for k, v in data.items():
                        # Новый формат
                        if isinstance(v, dict) and "ch" in v:
                            res[k] = {"ch": str(v["ch"])}
                        # Старый формат (кортеж/список из 3 элементов)
                        elif isinstance(v, (list, tuple)) and len(v) >= 1:
                            res[k] = {"ch": str(v[0])}
                        else:
                            continue
                    hash_map = res
                else:
                    hash_map = {}
        except Exception:
            hash_map = {}
    else:
        hash_map = {}

def load_channels():
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

def save_channel(channel: str):
    channels = load_channels()
    channel = str(channel)
    if channel not in channels:
        with open(CHANNELS_FILE, "a", encoding="utf-8") as f:
            f.write(channel + "\n")

def remove_channel(channel_id: str) -> bool:
    channels = load_channels()
    channel_id = str(channel_id)
    if channel_id in channels:
        channels.remove(channel_id)
        with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
            for ch in channels:
                f.write(ch + "\n")
        try:
            channel_cache.pop(channel_id, None)
        except Exception:
            pass
        return True
    return False

def get_channel_link(channel_id: str) -> str:
    try:
        chat = bot.get_chat(channel_id)
        return f"https://t.me/{chat.username}" if chat.username else (chat.title or f"ID: {channel_id}")
    except Exception:
        return f"ID: {channel_id}"

def add_stat():
    today = datetime.now().strftime("%d.%m.%Y")
    with open(STATS_FILE, "a", encoding="utf-8") as f:
        f.write(today + "\n")

def load_stats():
    with open(STATS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

def get_stats_text():
    stats = load_stats()
    total = len(stats)
    today = datetime.now().date()
    days = [(today - timedelta(days=i)).strftime("%d.%m.%Y") for i in range(29, -1, -1)]
    c = Counter(stats)
    lines = "\n".join(f"{d} — {c.get(d, 0)}" for d in days)
    return f"📊 <b>Статистика</b>\nВсего кликов: <b>{total}</b>\nЗа последние 30 дней:\n{lines}"

def get_channel_info(ch_id: str):
    now = time.time()
    ch_id = str(ch_id)
    cached = channel_cache.get(ch_id)
    if cached and now - cached["ts"] <= CACHE_TTL:
        return cached
    info = {"title": f"Канал {ch_id}", "is_admin": False, "ts": now}
    try:
        chat = bot.get_chat(ch_id)
        info["title"] = chat.title or info["title"]
        me = bot.get_me()
        status = bot.get_chat_member(ch_id, me.id).status
        info["is_admin"] = status in ("administrator", "creator")
    except Exception:
        pass
    channel_cache[ch_id] = info
    return info

def list_valid_admin_channels():
    channels = load_channels()
    valid = []
    for ch in channels:
        inf = get_channel_info(ch)
        if inf["is_admin"]:
            valid.append(ch)
    return valid

def load_templates() -> dict:
    if not os.path.exists(TEMPLATES_FILE):
        return {}
    try:
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_templates(data: dict):
    atomic_write_json(TEMPLATES_FILE, data)

def get_user_templates(user_id: int) -> dict:
    data = load_templates()
    return data.get(str(user_id), {})

def put_user_template(user_id: int, name: str, tpl: dict):
    data = load_templates()
    u = data.get(str(user_id), {})
    u[name] = tpl
    data[str(user_id)] = u
    save_templates(data)

def delete_user_template(user_id: int, name: str) -> bool:
    data = load_templates()
    u = data.get(str(user_id), {})
    if name in u:
        del u[name]
        data[str(user_id)] = u
        save_templates(data)
        return True
    return False

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Создать пост", "Шаблоны")
    kb.add("Добавить канал в список", "Список каналов")
    kb.add("Статистика")
    kb.add("Быстро добавить каналы")
    kb.add("Проверить права во всех каналах")
    return kb

@bot.message_handler(commands=['start'])
def start(message):
    # Если не авторизован — сразу просим пароль
    if is_private_chat(message) and not is_authorized(message.from_user.id):
        return start_password_flow(message.chat.id, message.from_user.id)
    bot.send_message(message.chat.id, "✨ Привет! Я помогу опубликовать посты в каналах.\nВыберите действие ниже:", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "Статистика", content_types=['text'])
@require_auth_msg
def show_stats(message):
    bot.send_message(message.chat.id, get_stats_text(), reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "Добавить канал в список", content_types=['text'])
@require_auth_msg
def ask_add_channel(message):
    msg = bot.send_message(message.chat.id, "📨 Перешлите <b>любое сообщение</b> из канала, который хотите добавить:")
    bot.register_next_step_handler(msg, save_channel_from_forward)

def save_channel_from_forward(message):
    # Этот шаг доступен только после успешной авторизации (т.к. мы попадаем сюда из ask_add_channel)
    if not getattr(message, "forward_from_chat", None):
        bot.send_message(message.chat.id, "⚠️ Нужно переслать сообщение именно <b>из канала</b>.", reply_markup=main_menu())
        return
    chat_id = message.forward_from_chat.id
    try:
        me = bot.get_me()
        bot_member = bot.get_chat_member(chat_id, me.id)
        if bot_member.status not in ['administrator', 'creator']:
            bot.send_message(message.chat.id, f"🔐 Бот должен быть админом канала {chat_id}.", reply_markup=main_menu())
            return
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка при проверке прав бота: {e}", reply_markup=main_menu())
        return
    save_channel(str(chat_id))
    bot.send_message(message.chat.id, f"✅ Канал добавлен: {get_channel_link(chat_id)}", reply_markup=main_menu())

@bot.message_handler(func=lambda m: quick_is_on(m.chat.id) and getattr(m, "forward_from_chat", None) is not None,
                     content_types=['text','audio','document','photo','sticker','video','video_note','voice','location','contact','new_chat_members','left_chat_member','venue','animation'])
@require_auth_msg
def quick_add_from_forward(message):
    ch = message.forward_from_chat.id
    ch_str = str(ch)

    _quick_arm_timer(message.chat.id, bot.send_message)

    try:
        me = bot.get_me()
        bot_member = bot.get_chat_member(ch, me.id)
        if bot_member.status not in ['administrator', 'creator']:
            bot.send_message(message.chat.id, f"⚠️ Бот не админ в {get_channel_link(ch)} — пропуск.")
            return
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка проверки прав бота в канале {ch}: {e}")
        return

    channels = load_channels()
    if ch_str in channels:
        bot.send_message(message.chat.id, f"ℹ️ Уже в списке: {get_channel_link(ch)}")
    else:
        save_channel(ch_str)
        with quick_mode_lock:
            quick_mode.setdefault(message.chat.id, {"on": True, "added": set(), "timer": None})
            quick_mode[message.chat.id]["added"].add(ch_str)
        bot.send_message(message.chat.id, f"➕ Добавлен: {get_channel_link(ch)}")

@bot.message_handler(func=lambda m: quick_is_on(m.chat.id) and getattr(m, "forward_from_chat", None) is None,
                     content_types=['text'])
@require_auth_msg
def quick_add_hint(message):
    _quick_arm_timer(message.chat.id, bot.send_message)
    bot.send_message(message.chat.id, "💡 Это не пересланное из канала. Пожалуйста, перешлите сообщение из нужного канала.")

@bot.message_handler(commands=['fast_on'])
@require_auth_msg
def cmd_fast_on(message):
    quick_on(message.chat.id, bot.send_message)
    bot.send_message(
        message.chat.id,
        "✅ Режим быстрого добавления включён.\nПересылайте сообщения из нужных каналов.\n" \
        f"Автовыключение через {QUICK_ADD_TIMEOUT} сек бездействия.\n\n" \
        "Когда закончите — нажмите «Выключить» ниже или используйте /fast_off.",
        reply_markup=_quick_controls_kb()
    )

@bot.message_handler(commands=['fast_off'])
@require_auth_msg
def cmd_fast_off(message):
    quick_off(message.chat.id, bot.send_message, show_summary=True)

@bot.message_handler(func=lambda m: m.text == "Быстро добавить каналы", content_types=['text'])
@require_auth_msg
def quick_add_entry(message):
    if quick_is_on(message.chat.id):
        bot.send_message(
            message.chat.id,
            "⚙️ Режим уже включён. Пересылайте сообщения из каналов.\n" \
            f"Автовыключение через {QUICK_ADD_TIMEOUT} сек бездействия.",
            reply_markup=_quick_controls_kb()
        )
    else:
        quick_on(message.chat.id, bot.send_message)
        bot.send_message(
            message.chat.id,
            "✅ Режим быстрого добавления включён.\nТеперь просто пересылайте сообщения из каналов, где бот — админ.",
            reply_markup=_quick_controls_kb()
        )

@bot.callback_query_handler(func=lambda call: call.data == "quick_off")
@require_auth_cb
def cb_quick_off(call):
    quick_off(call.message.chat.id, bot.send_message, show_summary=True)
    bot.answer_callback_query(call.id, "Выключено")

@bot.message_handler(func=lambda m: m.text == "Список каналов", content_types=['text'])
@require_auth_msg
def list_channels(message):
    valid_channels = list_valid_admin_channels()
    if not valid_channels:
        bot.send_message(message.chat.id, "📭 Нет каналов, где бот является админом.", reply_markup=main_menu())
        return
    send_channels_page(message.chat.id, 0, None)

def send_channels_page(chat_id, page=0, message_id=None):
    valid_channels = list_valid_admin_channels()
    if not valid_channels:
        bot.send_message(chat_id, "📭 Нет каналов, где бот является админом.", reply_markup=main_menu())
        return
    channels_list_page[chat_id] = page
    per_page = 5
    start_i, end_i = page * per_page, min((page + 1) * per_page, len(valid_channels))
    markup = types.InlineKeyboardMarkup()
    for i in range(start_i, end_i):
        ch = valid_channels[i]
        title = get_channel_info(ch)["title"]
        txt = title if len(title) <= 30 else title[:27] + "..."
        markup.add(types.InlineKeyboardButton(f"{i + 1}. {txt}", callback_data=f"view_channel_{ch}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️ Назад", callback_data=f"channels_page_{page - 1}"))
    if end_i < len(valid_channels):
        nav.append(types.InlineKeyboardButton("Вперед ▶️", callback_data=f"channels_page_{page + 1}"))
    if nav:
        markup.add(*nav)
    markup.add(types.InlineKeyboardButton("❌ Закрыть", callback_data="close_channels_list"))
    total_pages = (len(valid_channels) - 1) // per_page + 1
    text = f"📚 Список каналов (<b>{page + 1}</b>/{total_pages}):"
    if message_id:
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
        except ApiTelegramException:
            bot.send_message(chat_id, text, reply_markup=markup)
    else:
        bot.send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("channels_page_"))
@require_auth_cb
def handle_channels_page(call):
    page = int(call.data.split("_")[2])
    send_channels_page(call.message.chat.id, page, call.message.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_channel_"))
@require_auth_cb
def handle_view_channel(call):
    ch = call.data.split("_")[2]
    txt = get_channel_info(ch)["title"]
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗑️ Удалить канал", callback_data=f"delete_channel_{ch}"))
    markup.add(types.InlineKeyboardButton("◀️ Назад к списку", callback_data="back_to_channels_list"))
    try:
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                              text=f"🛰️ Канал: <b>{txt}</b>", reply_markup=markup)
    except ApiTelegramException:
        bot.send_message(call.message.chat.id, f"🛰️ Канал: <b>{txt}</b>", reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_channel_"))
@require_auth_cb
def handle_delete_channel(call):
    ch = call.data.split("_")[2]
    if remove_channel(ch):
        page = channels_list_page.get(call.message.chat.id, 0)
        send_channels_page(call.message.chat.id, page, call.message.message_id)
        bot.answer_callback_query(call.id, "Канал удалён")
    else:
        bot.answer_callback_query(call.id, "Ошибка при удалении канала")

@bot.callback_query_handler(func=lambda call: call.data == "back_to_channels_list")
@require_auth_cb
def handle_back_to_list(call):
    page = channels_list_page.get(call.message.chat.id, 0)
    send_channels_page(call.message.chat.id, page, call.message.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "close_channels_list")
@require_auth_cb
def handle_close_list(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except ApiTelegramException:
        try:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text="Список каналов закрыт")
        except Exception as e:
            print(f"Ошибка при закрытии списка каналов: {e}")
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=['check_rights'])
@bot.message_handler(func=lambda m: m.text == "Проверить права во всех каналах", content_types=['text'])
@require_auth_msg
def check_rights_entry(message):
    bot.send_message(message.chat.id, "🔎 Начинаю проверку прав бота во всех каналах… Пожалуйста, подождите.")
    threading.Thread(target=_check_rights_worker, args=(message.chat.id,), daemon=True).start()

def _check_rights_worker(chat_id: int):
    channels = load_channels()
    total = len(channels)
    kept = 0
    removed = 0
    errors = 0
    removed_list = []

    me = bot.get_me()
    for ch in channels:
        try:
            member = bot.get_chat_member(ch, me.id)
            is_admin = member.status in ['administrator', 'creator']
            channel_cache[str(ch)] = {
                "title": get_channel_info(ch)["title"],
                "is_admin": is_admin,
                "ts": time.time()
            }
            if is_admin:
                kept += 1
            else:
                if remove_channel(str(ch)):
                    removed += 1
                    removed_list.append(ch)
        except Exception:
            try:
                if remove_channel(str(ch)):
                    removed += 1
                    removed_list.append(ch)
            except Exception:
                errors += 1

    if removed_list:
        lines = "\n".join(f"• {get_channel_link(ch)}" for ch in removed_list[:20])
        more = f"\n…и ещё {len(removed_list)-20}" if len(removed_list) > 20 else ""
        details = f"\n\n🧹 Удалены (нет прав/нет доступа):\n{lines}{more}"
    else:
        details = "\n\nУдалённых каналов нет."

    text = (f"✅ Проверка завершена.\n"
            f"Всего в файле: <b>{total}</b>\n"
            f"Оставлено (бот админ): <b>{kept}</b>\n"
            f"Удалено: <b>{removed}</b>\n"
            f"Ошибок: <b>{errors}</b>"
            f"{details}")
    try:
        bot.send_message(chat_id, text, reply_markup=main_menu())
    except Exception as e:
        print(f"Не удалось отправить результат проверки: {e}")

# -----------------------
# Конструктор поста
# -----------------------
def reset_draft(chat_id: int):
    post_data[chat_id] = {
        "text": None,
        "media": None,
        "button_enabled": False,
        "button_text": "▶️ Смотреть",
        "channel": None
    }

@bot.message_handler(func=lambda m: m.text == "Создать пост", content_types=['text'])
@require_auth_msg
def post_constructor(message):
    reset_draft(message.chat.id)
    msg = bot.send_message(message.chat.id, "✍️ Введите текст поста:")
    bot.register_next_step_handler(msg, save_post_text)

def save_post_text(message):
    draft = post_data.get(message.chat.id)
    if not draft:
        reset_draft(message.chat.id)
        draft = post_data[message.chat.id]
    draft["text"] = (message.text or "").strip()
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add("Пропустить", "Добавить фото/видео")
    msg = bot.send_message(message.chat.id, "🖼 Хотите добавить фото/видео?", reply_markup=markup)
    bot.register_next_step_handler(msg, choose_media_option)

def choose_media_option(message):
    if message.text == "Пропустить":
        post_data[message.chat.id]["media"] = None
        ask_channel_selection(message)
    else:
        msg = bot.send_message(message.chat.id, "📤 Отправьте фото или видео:", reply_markup=types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, save_post_media)

def save_post_media(message):
    if message.content_type == "photo":
        post_data[message.chat.id]["media"] = ("photo", message.photo[-1].file_id)
    elif message.content_type == "video":
        post_data[message.chat.id]["media"] = ("video", message.video.file_id)
    else:
        msg = bot.send_message(message.chat.id, "Неверный формат. Отправьте фото или видео.")
        bot.register_next_step_handler(msg, save_post_media)
        return
    ask_channel_selection(message)

def ask_channel_selection(message):
    valid_channels = list_valid_admin_channels()
    if not valid_channels:
        bot.send_message(message.chat.id, "📭 Нет каналов для публикации. Добавьте канал, где бот — админ.", reply_markup=main_menu())
        return
    send_channel_selection_inline(message.chat.id, 0, None)

def send_channel_selection_inline(chat_id, page=0, message_id=None):
    valid_channels = list_valid_admin_channels()
    if not valid_channels:
        bot.send_message(chat_id, "📭 Нет каналов для публикации.", reply_markup=main_menu())
        return
    channels_with_names = []
    for ch in valid_channels:
        title = get_channel_info(ch)["title"]
        channels_with_names.append((title, ch))
    channels_with_names.sort(key=lambda x: x[0].lower())
    sorted_channels = [ch for _, ch in channels_with_names]
    user_page[chat_id] = page
    per_page = 9
    start_i, end_i = page * per_page, min((page + 1) * per_page, len(sorted_channels))
    markup = types.InlineKeyboardMarkup()
    for i in range(start_i, end_i):
        ch = sorted_channels[i]
        title = get_channel_info(ch)["title"]
        markup.add(types.InlineKeyboardButton(title, callback_data=f"select_channel_{ch}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️ Назад", callback_data=f"channel_select_page_{page-1}"))
    if end_i < len(sorted_channels):
        nav.append(types.InlineKeyboardButton("Вперед ▶️", callback_data=f"channel_select_page_{page+1}"))
    if nav:
        markup.row(*nav)
    total_pages = (len(sorted_channels) - 1) // per_page + 1
    text = f"📡 Выберите канал для публикации (<b>{page+1}</b>/{total_pages}):"
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
        except Exception as e:
            print(f"Ошибка при редактировании сообщения: {e}")
    else:
        bot.send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("channel_select_page_"))
@require_auth_cb
def handle_channel_select_page(call):
    page = int(call.data.split("_")[3])
    send_channel_selection_inline(call.message.chat.id, page, call.message.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_channel_"))
@require_auth_cb
def handle_select_channel(call):
    channel_id = call.data.split("_")[2]
    post_data.setdefault(call.message.chat.id, {})["channel"] = channel_id
    bot.answer_callback_query(call.id, f"Выбран канал: {get_channel_info(channel_id)['title']}")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    ask_add_button(call.message)

# ---------- Новая логика «Добавить кнопку?» ----------
def ask_add_button(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add("Пропустить", "Добавить кнопку")
    msg = bot.send_message(message.chat.id, "🔘 Хотите добавить кнопку?", reply_markup=markup)
    bot.register_next_step_handler(msg, choose_button_option)

def choose_button_option(message):
    if message.text == "Пропустить":
        post_data[message.chat.id]["button_enabled"] = False
        bot.send_message(message.chat.id, "⏭ Кнопка не будет добавлена.", reply_markup=types.ReplyKeyboardRemove())
        send_preview(message)
    else:
        post_data[message.chat.id]["button_enabled"] = True
        bot.send_message(message.chat.id, "✅ Кнопка будет добавлена.", reply_markup=types.ReplyKeyboardRemove())
        send_preview(message)

def make_button_hash_for_channel(ch_id: str) -> str:
    with hash_lock:
        h = md5_hash(f"subcheck|{str(ch_id)}")
        hash_map[h] = {"ch": str(ch_id)}
        save_buttons()
        return h

def render_preview_kb(draft: dict):
    markup = None
    if draft.get("button_enabled"):
        btn = draft.get("button_text", "▶️ Смотреть")
        ch = draft.get("channel")
        h = make_button_hash_for_channel(str(ch))
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(btn, callback_data=h))
    return markup
# ----------------------------------------------------

def send_preview(message):
    data = post_data.get(message.chat.id) or {}
    if not data.get("channel"):
        bot.send_message(message.chat.id, "🚫 Канал не выбран. Начните заново: «Создать пост».", reply_markup=main_menu())
        return
    ch_title = get_channel_info(data["channel"])["title"]
    markup = render_preview_kb(data)

    header = f"<b>🔎 Предпросмотр</b>\nКанал: <b>{ch_title}</b>\nКнопка: {'есть' if data.get('button_enabled') else 'нет'}"
    if data.get("media"):
        t, fid = data["media"]
        if t == "photo":
            bot.send_photo(message.chat.id, fid, caption=f"{data.get('text')}\n\n{header}", reply_markup=markup)
        else:
            bot.send_video(message.chat.id, fid, caption=f"{data.get('text')}\n\n{header}", reply_markup=markup)
    else:
        bot.send_message(message.chat.id, f"{data.get('text')}\n\n{header}", reply_markup=markup)

    actions = types.InlineKeyboardMarkup()
    actions.add(types.InlineKeyboardButton("✅ Опубликовать", callback_data="publish"))
    actions.add(types.InlineKeyboardButton("💾 Сохранить как шаблон", callback_data="save_as_template"))
    actions.add(types.InlineKeyboardButton("✏️ Изменить текст", callback_data="edit_text"))
    actions.add(types.InlineKeyboardButton("🖼️ Изменить медиа", callback_data="edit_media"))
    bot.send_message(message.chat.id, "Что дальше?", reply_markup=actions)

@bot.callback_query_handler(func=lambda call: call.data in ("edit_text","edit_media"))
@require_auth_cb
def handle_edit_parts(call):
    if call.data == "edit_text":
        msg = bot.send_message(call.message.chat.id, "✏️ Введите новый текст поста:")
        bot.register_next_step_handler(msg, _edit_text_finish)
    elif call.data == "edit_media":
        msg = bot.send_message(call.message.chat.id, "🖼 Отправьте новое фото или видео (или «отмена» текстом чтобы удалить медиа):")
        bot.register_next_step_handler(msg, _edit_media_finish)
    bot.answer_callback_query(call.id)

def _edit_text_finish(message):
    txt = (message.text or "").strip()
    post_data.setdefault(message.chat.id, {})["text"] = txt
    send_preview(message)

def _edit_media_finish(message):
    if message.content_type == "text" and (message.text or "").strip().lower() == "отмена":
        post_data.setdefault(message.chat.id, {})["media"] = None
    elif message.content_type == "photo":
        post_data.setdefault(message.chat.id, {})["media"] = ("photo", message.photo[-1].file_id)
    elif message.content_type == "video":
        post_data.setdefault(message.chat.id, {})["media"] = ("video", message.video.file_id)
    else:
        msg = bot.send_message(message.chat.id, "Неверный формат. Отправьте фото/видео или «отмена».")
        bot.register_next_step_handler(msg, _edit_media_finish)
        return
    send_preview(message)

@bot.callback_query_handler(func=lambda call: call.data == "publish")
@require_auth_cb
def publish_post(call):
    data = post_data.get(call.message.chat.id) or {}
    ch = data.get("channel")
    if not ch:
        bot.answer_callback_query(call.id, "Канал не выбран", show_alert=True)
        return

    markup = None
    if data.get("button_enabled"):
        btn = data.get("button_text", "▶️ Смотреть")
        h = make_button_hash_for_channel(str(ch))
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(btn, callback_data=h))
    try:
        if data.get("media"):
            t, fid = data["media"]
            if t == "photo":
                bot.send_photo(ch, fid, caption=data.get("text"), reply_markup=markup)
            else:
                bot.send_video(ch, fid, caption=data.get("text"), reply_markup=markup)
        else:
            bot.send_message(ch, data.get("text"), reply_markup=markup)
        bot.answer_callback_query(call.id, "Опубликовано ✅", show_alert=True)
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Ошибка публикации: {e}")
        bot.answer_callback_query(call.id)

# -------------
# Обработчик клика по кнопке (новая логика)
# -------------
@bot.callback_query_handler(func=lambda call: call.data in hash_map)
def link_handler(call):
    try:
        with hash_lock:
            ch = hash_map[call.data]["ch"]
        user = call.from_user.id
        member = bot.get_chat_member(ch, user)
        if member.status in ["member", "administrator", "creator"]:
            bot.answer_callback_query(call.id, "Благодарим вас за подписку на наш канал!\nНажмите на «HD», чтобы начать просмотр", show_alert=True)
            add_stat()
        else:
            bot.answer_callback_query(call.id, "Подпишитесь на наш канал, чтобы получить доступ к просмотру!", show_alert=True)
    except Exception as e:
        print(f"Ошибка: {e}")
        bot.answer_callback_query(call.id, "Ошибка при проверке.")

@bot.message_handler(func=lambda m: m.text == "Шаблоны", content_types=['text'])
@require_auth_msg
def templates_menu(message):
    uid = message.from_user.id
    tpls = get_user_templates(uid)
    if not tpls:
        bot.send_message(message.chat.id, "📁 У вас пока нет шаблонов. Создайте пост и сохраните его как шаблон.", reply_markup=main_menu())
        return
    kb = types.InlineKeyboardMarkup()
    for name in sorted(tpls.keys()):
        kb.add(types.InlineKeyboardButton(f"📄 {name}", callback_data=f"use_tpl::{name}"))
    kb.add(types.InlineKeyboardButton("➖ Удалить шаблон…", callback_data="tpl_delete_mode"))
    bot.send_message(message.chat.id, "Ваши шаблоны:", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("use_tpl::"))
@require_auth_cb
def use_template(call):
    name = call.data.split("::", 1)[1]
    uid = call.from_user.id
    tpls = get_user_templates(uid)
    tpl = tpls.get(name)
    if not tpl:
        bot.answer_callback_query(call.id, "Шаблон не найден", show_alert=True)
        return
    reset_draft(call.message.chat.id)
    draft = post_data[call.message.chat.id]
    draft["text"] = tpl.get("text")
    draft["media"] = tuple(tpl["media"]) if tpl.get("media") else None
    if tpl.get("button_url") is not None:
        draft["button_enabled"] = True
    else:
        draft["button_enabled"] = bool(tpl.get("button_enabled", False))
    draft["button_text"] = tpl.get("button_text", "▶️ Смотреть")

    bot.answer_callback_query(call.id, f"Шаблон «{name}» применён. Выберите канал.")
    ask_channel_selection(call.message)

@bot.callback_query_handler(func=lambda call: call.data == "tpl_delete_mode")
@require_auth_cb
def tpl_delete_mode(call):
    uid = call.from_user.id
    tpls = get_user_templates(uid)
    if not tpls:
        bot.answer_callback_query(call.id, "Нет шаблонов", show_alert=True)
        return
    kb = types.InlineKeyboardMarkup()
    for name in sorted(tpls.keys()):
        kb.add(types.InlineKeyboardButton(f"🗑️ {name}", callback_data=f"del_tpl::{name}"))
    bot.edit_message_text("Выберите шаблон для удаления:", call.message.chat.id, call.message.message_id, reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("del_tpl::"))
@require_auth_cb
def delete_tpl(call):
    name = call.data.split("::", 1)[1]
    uid = call.from_user.id
    ok = delete_user_template(uid, name)
    bot.answer_callback_query(call.id, "Удалён" if ok else "Шаблон не найден", show_alert=False)
    templates_menu(call.message)

@bot.callback_query_handler(func=lambda call: call.data == "save_as_template")
@require_auth_cb
def save_as_template(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "💾 Введите название шаблона:")
    bot.register_next_step_handler(msg, _save_template_finish)

def _serialize_media(media):
    if not media:
        return None
    return [media[0], media[1]]

def _save_template_finish(message):
    name = (message.text or "").strip()
    if not name:
        bot.send_message(message.chat.id, "⚠️ Пустое имя. Отмена.", reply_markup=main_menu())
        return
    draft = post_data.get(message.chat.id) or {}
    tpl = {
        "text": draft.get("text"),
        "media": _serialize_media(draft.get("media")),
        "button_enabled": bool(draft.get("button_enabled", False)),
        "button_text": draft.get("button_text", "▶️ Смотреть")
    }
    put_user_template(message.from_user.id, name, tpl)
    bot.send_message(message.chat.id, f"✅ Шаблон «{name}» сохранён.", reply_markup=main_menu())
    send_preview(message)

# ===== Общий ловец сообщений для неавторизованных (без команд) =====
@bot.message_handler(func=lambda m: True, content_types=['text','audio','document','photo','sticker','video','video_note','voice','location','contact','new_chat_members','left_chat_member','venue','animation'])
def fallback_router(message):
    # Если пользователь не авторизован и пишет боту в личке — попросим пароль
    return route_public_message_or_prompt_auth(message)

if __name__ == "__main__":
    load_buttons()
    bot.infinity_polling(
        skip_pending=True,
        timeout=60,
        long_polling_timeout=60,
        allowed_updates=["message", "callback_query"]
    )
