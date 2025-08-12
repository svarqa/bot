# bot.py
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import json
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
import threading
import http.server
import socketserver
import sys
from typing import Optional

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN не задан в окружении")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="/", intents=intents)
trackers: dict[int, "Tracker"] = {}  # channel_id -> Tracker
SAVE_FILE = "trackers.json"

# ------- Tracker -------
class Tracker:
    def __init__(
        self,
        channel: discord.TextChannel,
        message: str,
        timeout_minutes: int,
        repeat: int,
        last_activity: Optional[datetime] = None,
    ):
        self.channel = channel
        self.message = message
        self.timeout_minutes = max(1, int(timeout_minutes))
        self.timeout = self.timeout_minutes * 60
        self.repeat = max(1, int(repeat))
        self.last_activity = last_activity or datetime.now(timezone.utc)
        self.active = True
        # Запускаем таск в текущем event loop
        self.task = asyncio.create_task(self.monitor())

    def update_time(self):
        self.last_activity = datetime.now(timezone.utc)

    async def monitor(self):
        try:
            while self.active:
                await asyncio.sleep(10)  # интервал проверки
                now = datetime.now(timezone.utc)
                elapsed = (now - self.last_activity).total_seconds()
                if elapsed >= self.timeout:
                    for _ in range(self.repeat):
                        try:
                            await self.channel.send(self.message)
                        except Exception as e:
                            print(f"[Tracker] Ошибка отправки в канал {self.channel.id}: {e}")
                        await asyncio.sleep(1)
                    self.last_activity = datetime.now(timezone.utc)
                    save_trackers()
        except asyncio.CancelledError:
            # корректное завершение таска
            pass
        except Exception as e:
            print(f"[Tracker] Неожиданная ошибка в monitor для {self.channel.id}: {e}")

# ------- Persistence -------
def save_trackers():
    data = {}
    for channel_id, tracker in trackers.items():
        data[str(channel_id)] = {
            "message": tracker.message,
            "timeout_minutes": tracker.timeout_minutes,
            "repeat": tracker.repeat,
            "last_activity": tracker.last_activity.isoformat(),
        }
    try:
        with open(SAVE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("Ошибка сохранения трекеров:", e)

async def load_trackers():
    if not os.path.exists(SAVE_FILE):
        return
    try:
        with open(SAVE_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        print("Ошибка чтения файла трекеров:", e)
        return

    now = datetime.now(timezone.utc)
    for channel_id_str, tdata in data.items():
        try:
            channel_id = int(channel_id_str)
        except Exception:
            continue
        if channel_id in trackers:
            # уже есть трекер — пропускаем
            continue
        # сначала пробуем get_channel, если None — fetch
        channel = bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                print(f"[load_trackers] Не удалось получить канал {channel_id}, пропускаю")
                continue
        try:
            tracker = Tracker(
                channel,
                tdata.get("message", "ping"),
                tdata.get("timeout_minutes", 1),
                tdata.get("repeat", 1),
                now,
            )
            trackers[channel_id] = tracker
        except Exception as e:
            print(f"[load_trackers] Ошибка создания трекера для {channel_id}: {e}")

# ------- Safe interaction send (защита от Unknown interaction) -------
async def safe_interaction_send(interaction: discord.Interaction, content: str, ephemeral: bool = False):
    """
    Попытка корректно ответить на интеракцию:
    1) если response не использован — response.send_message
    2) если response уже использован — followup.send
    3) если получаем NotFound (Unknown interaction) — fallback: отправка в канал
    """
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content, ephemeral=ephemeral)
            return
        await interaction.followup.send(content, ephemeral=ephemeral)
    except discord.NotFound:
        # interaction token устарел — отправляем обычное сообщение в канал
        try:
            if interaction.channel:
                await interaction.channel.send(content)
        except Exception as e:
            print("[safe_interaction_send] Фоллбек не удался:", e)
    except Exception as e:
        print("[safe_interaction_send] Ошибка отправки интеракции:", e)

# ------- Events -------
@bot.event
async def on_ready():
    import os as _os
    print(f"[on_ready] {bot.user} запущен, PID={_os.getpid()}")
    await load_trackers()
    await bot.tree.sync()
    print("[on_ready] Синхронизация команд завершена")

@bot.event
async def on_resumed():
    now = datetime.now(timezone.utc)
    for tracker in trackers.values():
        tracker.last_activity = now
    save_trackers()
    print("[on_resumed] Сессия возобновлена, сброс last_activity для всех трекеров")

@bot.event
async def on_message(message):
    # Для команд text-based
    await bot.process_commands(message)
    # Обновляем last_activity
    tracker = trackers.get(getattr(message.channel, "id", None))
    if tracker and message.author != bot.user:
        tracker.update_time()
        save_trackers()

# ------- Slash commands -------
@bot.tree.command(name="start", description="старт")
@app_commands.describe(message="сообщение", timeout_minutes="минуты", repeat="повторы")
async def start(interaction: discord.Interaction, message: str, timeout_minutes: int, repeat: int):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await safe_interaction_send(interaction, "команда работает только в текстовых каналах", ephemeral=True)
        return

    if channel.id in trackers:
        await safe_interaction_send(interaction, "Трекер уже запущен в этом канале.", ephemeral=True)
        return

    # если готовишься делать долгую работу -> можно defer
    # await interaction.response.defer(ephemeral=True)

    tracker = Tracker(channel, message, timeout_minutes, repeat)
    trackers[channel.id] = tracker
    save_trackers()

    await safe_interaction_send(
        interaction,
        f"если нет сообщений {timeout_minutes} минут, отправлю {repeat} раз \"{message}\""
    )

@bot.tree.command(name="stop", description="стоп")
async def stop(interaction: discord.Interaction):
    tracker = trackers.get(interaction.channel.id)
    if tracker:
        tracker.active = False
        if tracker.task:
            tracker.task.cancel()
        del trackers[interaction.channel.id]
        save_trackers()
        await safe_interaction_send(interaction, "остановочка")
    else:
        await safe_interaction_send(interaction, "нечего останавливать")

@bot.tree.command(name="list", description="показать активные трекеры")
async def list_trackers(interaction: discord.Interaction):
    if not trackers:
        await safe_interaction_send(interaction, "Нет активных трекеров.", ephemeral=True)
        return
    lines = []
    for ch_id, tracker in trackers.items():
        channel = bot.get_channel(ch_id)
        name = channel.mention if channel else str(ch_id)
        lines.append(f"{name}: сообщение \"{tracker.message}\", таймаут {tracker.timeout_minutes} мин, повторов {tracker.repeat}")
    text = "\n".join(lines)
    # разбиваем по 1900 символов, чтобы не превысить лимит
    for i in range(0, len(text), 1900):
        await safe_interaction_send(interaction, text[i : i + 1900])

# ------- Global app command error handler (чтобы не плодить стектрейсы для NotFound) -------
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # если ошибка — именно NotFound при попытке отправить ответ, игнорируем
    if isinstance(error, discord.NotFound):
        return
    print("[app_command_error]", error)

# ------- Webserver (чтобы Render видел порт) -------
class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return

def run_webserver():
    port = int(os.environ.get("PORT", 8000))
    try:
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        with socketserver.ThreadingTCPServer(("0.0.0.0", port), QuietHandler) as httpd:
            print(f"[webserver] listening on 0.0.0.0:{port}, PID={os.getpid()}")
            httpd.serve_forever()
    except Exception as e:
        print("[webserver] failed to start:", e, file=sys.stderr)

t = threading.Thread(target=run_webserver, daemon=True)
t.start()

# ------- Run bot -------
bot.run(TOKEN)
