import os
import json
import datetime
import sys
import re
import urllib.parse
import asyncio
from pathlib import Path
from typing import Final

import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

from keep_alive import keep_alive

keep_alive()

NG_WORDS_FILE = "ng_words.json"
INITIAL_NG_WORDS = [
    "死ね",
    "消えろ",
    "うざい",
    "クソ",
    "ゴミ",
    "雑魚",
    "きもい",
    "キモい",
    "バカ",
    "アホ",
    "カス",
    "無能",
    "頭悪い",
    "最低",
    "消え失せろ",
    "ぶっ殺す",
    "殺す",
    "氏ね",
    "そういうのいいから",
    "話にならない",
    "空気読め",
    "自分で考えて",
    "それで？",
    "で？",
    "だから何",
    "勘違いしてる",
    "見当違い",
    "的外れ",
    "それ無理でしょ",
    "無理じゃない？",
    "何言ってるの",
    "まともにできない",
    "センスない",
    "向いてない",
    "お察し",
    "浅い",
    "幼稚",
]
SEARCH_RESULT_LIMIT: Final[int] = 3
SEARCH_SNIPPET_LIMIT: Final[int] = 160
LOG_DIR = Path("/tmp/logs")
AUDIT_LOG_FILE = "audit_log.json"
AUDIT_LOG_LIMIT = 200
NEWS_CHANNELS_FILE = "news_channels.json"
NEWS_SETTINGS_FILE = "news_settings.json"
NEWS_POSTED_FILE = "news_posted.json"
NEWS_POLL_SECONDS: Final[int] = 60
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
# OpenAIのモデル名を環境変数から取得。未設定時のデフォルトを実在する「gpt-4o-mini」等にしておくと安全です
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
NEWS_FEED_URL = "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja"


def load_ng_words() -> list[str]:
    if os.path.exists(NG_WORDS_FILE):
        with open(NG_WORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_ng_words(words: list[str]) -> None:
    with open(NG_WORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False, indent=2)


def load_news_channels() -> dict[str, int]:
    if os.path.exists(NEWS_CHANNELS_FILE):
        with open(NEWS_CHANNELS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            return {str(k): int(v) for k, v in raw.items()}
    return {}


def save_news_channels(data: dict[str, int]) -> None:
    with open(NEWS_CHANNELS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_news_settings() -> dict[str, dict[str, str]]:
    if os.path.exists(NEWS_SETTINGS_FILE):
        with open(NEWS_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_news_settings(data: dict[str, dict[str, str]]) -> None:
    with open(NEWS_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_posted_news() -> dict[str, list[str]]:
    if os.path.exists(NEWS_POSTED_FILE):
        with open(NEWS_POSTED_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            return {str(k): list(v) for k, v in raw.items()}
    return {}


def save_posted_news(data: dict[str, list[str]]) -> None:
    with open(NEWS_POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_news_setting(guild_id: int) -> dict[str, str]:
    return load_news_settings().get(str(guild_id), {})


def set_news_setting(guild_id: int, key: str, value: str) -> None:
    settings = load_news_settings()
    current = settings.get(str(guild_id), {})
    current[key] = value
    settings[str(guild_id)] = current
    save_news_settings(settings)


def load_audit_log() -> list[dict[str, str]]:
    if os.path.exists(AUDIT_LOG_FILE):
        with open(AUDIT_LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_audit_log(entries: list[dict[str, str]]) -> None:
    with open(AUDIT_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(entries[-AUDIT_LOG_LIMIT:], f, ensure_ascii=False, indent=2)


def append_audit_log(action: str, detail: str) -> None:
    entries = load_audit_log()
    entries.append(
        {
            "time": discord.utils.utcnow().isoformat(),
            "action": action,
            "detail": detail,
        }
    )
    save_audit_log(entries)


def ensure_initial_ng_words() -> None:
    words = load_ng_words()
    if words:
        return
    save_ng_words(INITIAL_NG_WORDS)


ensure_initial_ng_words()


intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
owner_id: int | None = None
news_task: asyncio.Task[None] | None = None


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == owner_id


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.administrator  # type: ignore


def owner_label() -> str:
    return "（オーナー限定）"


def admin_label() -> str:
    return "（管理者限定）"
