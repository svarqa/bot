import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="/", intents=intents)
trackers = {}  # channel_id: Tracker
SAVE_FILE = "trackers.json"

class Tracker:
    def __init__(self, channel: discord.TextChannel, message: str, timeout_minutes: int, repeat: int, last_activity=None):
        self.channel = channel
        self.message = message
        self.timeout_minutes = timeout_minutes
        self.timeout = timeout_minutes * 60
        self.repeat = repeat
        self.last_activity = last_activity or datetime.now(timezone.utc)
        self.active = True
        self.task = asyncio.create_task(self.monitor())

    def update_time(self):
        self.last_activity = datetime.now(timezone.utc)

    async def monitor(self):
        while self.active:
            await asyncio.sleep(10)
            now = datetime.now(timezone.utc)
            elapsed = (now - self.last_activity).total_seconds()
            if elapsed >= self.timeout:
                for _ in range(self.repeat):
                    await self.channel.send(self.message)
                    await asyncio.sleep(1)
                self.last_activity = datetime.now(timezone.utc)
                save_trackers()


def save_trackers():
    data = {}
    for channel_id, tracker in trackers.items():
        data[str(channel_id)] = {
            "message": tracker.message,
            "timeout_minutes": tracker.timeout_minutes,
            "repeat": tracker.repeat,
            "last_activity": tracker.last_activity.isoformat()
        }
    with open(SAVE_FILE, "w") as f:
        json.dump(data, f)

async def load_trackers():
    if not os.path.exists(SAVE_FILE):
        return
    with open(SAVE_FILE, "r") as f:
        data = json.load(f)
    now = datetime.now(timezone.utc)
    for channel_id_str, tdata in data.items():
        channel_id = int(channel_id_str)
        channel = bot.get_channel(channel_id)
        if not channel:
            continue
        # Восстанавливаем last_activity на текущее время
        tracker = Tracker(
            channel,
            tdata["message"],
            tdata["timeout_minutes"],
            tdata["repeat"],
            now
        )
        trackers[channel_id] = tracker

@bot.event
async def on_ready():
    print(f"{bot.user} запущен")
    await load_trackers()
    await bot.tree.sync()

@bot.event
async def on_resumed():
    # при переподключении сбрасываем last_activity, чтобы не было моментального спама
    now = datetime.now(timezone.utc)
    for tracker in trackers.values():
        tracker.last_activity = now
    save_trackers()
    print("Сессия возобновлена, сброс last_activity для всех трекеров")

@bot.event
async def on_message(message):
    await bot.process_commands(message)
    tracker = trackers.get(message.channel.id)
    if tracker and message.author != bot.user:
        tracker.update_time()
        save_trackers()

@bot.tree.command(name="start", description="старт")
@app_commands.describe(
    message="сообщение",
    timeout_minutes="минуты",
    repeat="повторы"
)
async def start(interaction: discord.Interaction, message: str, timeout_minutes: int, repeat: int):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("команда работает только в текстовых каналах", ephemeral=True)
        return

    tracker = Tracker(channel, message, timeout_minutes, repeat)
    trackers[channel.id] = tracker
    save_trackers()

    await interaction.response.send_message(
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
        await interaction.response.send_message("остановочка")
    else:
        await interaction.response.send_message("нечего останавливать")

@bot.tree.command(name="list", description="показать активные трекеры")
async def list_trackers(interaction: discord.Interaction):
    if not trackers:
        await interaction.response.send_message("Нет активных трекеров.", ephemeral=True)
        return
    lines = []
    for ch_id, tracker in trackers.items():
        channel = bot.get_channel(ch_id)
        name = channel.mention if channel else str(ch_id)
        lines.append(f"{name}: сообщение \"{tracker.message}\", таймаут {tracker.timeout_minutes} мин, повторов {tracker.repeat}")
    await interaction.response.send_message("\n".join(lines))

bot.run(TOKEN)


async def run_webserver():
    async def handle(request):
        return web.Response(text="OK")

    app = web.Application()
    app.add_routes([web.get("/", handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    # Порт берем из переменной окружения Render
    port = int(os.environ.get("PORT", 8000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Web server запущен на порту {port}")

async def main():
    # Запускаем вебсервер параллельно с ботом
    await run_webserver()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
