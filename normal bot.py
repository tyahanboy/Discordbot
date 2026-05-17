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

NG_WORDS_FILE = "ng_words.json"
INITIAL_NG_WORDS = [
    "死ね", "消えろ", "うざい", "クソ", "ゴミ", "雑魚", "きもい", "キモい", 
    "バカ", "アホ", "カス", "無能", "頭悪い", "最低", "消え失せろ", 
    "ぶっ殺す", "殺す", "氏ね", "そういうのいいから", "話にならない", 
    "空気読め", "自分で考えて", "それで？", "で？", "だから何", 
    "勘違いしてる", "見当違い", "的外れ", "それ無理でしょ", "無理じゃない？", 
    "何言ってるの", "まともにできない", "センスない", "向いてない", 
    "お察し", "浅い", "幼稚",
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


def clean_search_query(text: str) -> str:
    return re.sub(r"[\s@<>]+", " ", text).strip()


def html_unescape(text: str) -> str:
    return (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )


def normalize_duckduckgo_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        query = urllib.parse.parse_qs(parsed.query)
        uddg = query.get("uddg", [""])[0]
        if uddg:
            return urllib.parse.unquote(uddg)
    return url


async def fetch_web_results(query: str) -> list[dict[str, str]]:
    q = clean_search_query(query)
    if not q:
        return []
    search_url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q, "kl": "jp-ja"})
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(search_url, timeout=10) as resp:
            html = await resp.text()
    results: list[dict[str, str]] = []
    for match in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>(?:.|\n)*?<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
        html,
        re.S,
    ):
        href = normalize_duckduckgo_url(html_unescape(match.group("href")))
        title = html_unescape(re.sub(r"<.*?>", "", match.group("title"))).strip()
        snippet = html_unescape(re.sub(r"<.*?>", "", match.group("snippet"))).strip()
        results.append(
            {
                "title": title,
                "snippet": re.sub(r"\s+", " ", snippet)[:SEARCH_SNIPPET_LIMIT],
                "url": href,
            }
        )
        if len(results) >= SEARCH_RESULT_LIMIT:
            break
    return results


async def search_answer(query: str) -> str:
    results = await fetch_web_results(query)
    if not results:
        return "見つからなかった。別の言い方でもう一回投げてくれたら探し直せる。"
    lines = [f"{r['title']} — {r['snippet']}\n{r['url']}" for r in results]
    return "\n\n".join(lines)


def parse_rss_items(xml: str) -> list[dict[str, str]]:
    items = []
    for match in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        chunk = match.group(1)
        title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", chunk, re.S)
        link = re.search(r"<link>(.*?)</link>", chunk, re.S)
        if not title or not link:
            continue
        title_text = (title.group(1) or title.group(2) or "").strip()
        link_text = (link.group(1) or "").strip()
        if title_text and link_text:
            items.append({"title": title_text, "link": link_text})
    return items


async def fetch_latest_news() -> list[dict[str, str]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(NEWS_FEED_URL, timeout=15) as resp:
            xml = await resp.text()
    return parse_rss_items(xml)


async def summarize_news(theme: str, items: list[dict[str, str]]) -> str:
    lines = []
    for item in items[:3]:
        lines.append(f"・{item['title']}\n  {item['link']}")
    return "\n".join(lines)


async def news_loop() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        wait_seconds = NEWS_POLL_SECONDS
        try:
            channels = load_news_channels()
            if channels:
                posted = load_posted_news()
                items = await fetch_latest_news()
                
                for guild_id_str, channel_id in channels.items():
                    channel = bot.get_channel(channel_id)
                    if channel is None or not isinstance(channel, discord.TextChannel):
                        continue
                        
                    guild_id_int = int(guild_id_str)
                    setting = get_news_setting(guild_id_int)
                    interval = int(setting.get("interval", str(NEWS_POLL_SECONDS)))
                    
                    seen = posted.get(guild_id_str, [])
                    fresh = [item for item in items[:5] if item["link"] not in seen]
                    if not fresh:
                        continue
                        
                    theme = setting.get("theme", "最新ニュース")
                    summary = await summarize_news(theme, fresh)
                        
                    sent_message = await channel.send(f"📰 【{theme}】の新着ニュース\n\n{summary}")
                    if sent_message.embeds:
                        await sent_message.edit(suppress=True)
                        
                    posted[guild_id_str] = ([item["link"] for item in fresh] + seen)[:50]
                    wait_seconds = min(wait_seconds, interval)
                    
                save_posted_news(posted)
        except Exception as e:
            append_audit_log("news_error", f"{type(e).__name__}: {e}")
            print(f"News Loop Error: {e}")
            
        await asyncio.sleep(wait_seconds)


# --------------------------------------------------
# 🎟️ チケットシステム用のボタンコンポーネント
# --------------------------------------------------
class TicketButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None) # 24時間ボタンを有効にする設定

    @discord.ui.button(label="チケットを開設する 🎟️", style=discord.ButtonStyle.green, custom_id="create_ticket_btn")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        if guild is None:
            return

        # 既に同じ人のチケットチャンネルがないかチェック
        channel_name = f"ticket-{user.name.lower().replace(' ', '-')}"
        existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
        
        if existing_channel:
            await interaction.response.send_message(f"❌ 既にあなたのチケットチャンネル ({existing_channel.mention}) が存在します。", ephemeral=True)
            return

        # チャンネルの閲覧権限を設定（作成者と管理者、ボットだけが見えるようにする）
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        # 管理者権限を持っているロールにも見えるようにする
        for role in guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        # 実際に非公開チャンネルを作成
        ticket_channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites)
        
        # 開設完了の通知（ボタンを押した本人にだけ見える）
        await interaction.response.send_message(f"✅ チケットを開設しました！ {ticket_channel.mention} へ移動してください。", ephemeral=True)
        
        # 作成されたチャンネルに案内メッセージを投稿
        await ticket_channel.send(
            f"🎟️ **{user.mention} さんのサポートチケット**\n"
            "お問い合わせ内容、またはご要望をご記入ください。\n"
            "管理者が確認するまでしばらくお待ちください。\n\n"
            "※用件が終了したら、管理者が `/ticket_close` を実行してこのチャンネルを閉じます。"
        )
        append_audit_log("ticket_created", f"Ticket created for {user} in #{channel_name}")


@bot.event
async def on_ready():
    global owner_id, news_task
    info = await bot.application_info()
    owner_id = info.owner.id
    await bot.tree.sync()
    
    # ボット再起動後もチケットボタンが動き続けるように登録
    bot.add_view(TicketButton())
    
    if news_task is None or news_task.done():
        news_task = asyncio.create_task(news_loop())
    print(f"Logged in as {bot.user}")
    print(f"Owner ID: {owner_id}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    for word in load_ng_words():
        if word in message.content:
            await message.delete()
            append_audit_log("message_deleted", f"{message.author} in #{getattr(message.channel, 'name', 'unknown')} matched {word}")
            try:
                await message.author.send(f"⚠️ NGワード「{word}」を含むメッセージを削除しました。")
            except discord.Forbidden:
                pass
            return

    await bot.process_commands(message)


# --------------------------------------------------
# プレフィックスコマンド（!から始まるコマンド）
# --------------------------------------------------
@bot.command(name="ping")
async def text_ping(ctx: commands.Context):
    latency = round(bot.latency * 1000)
    await ctx.send(f"Pong! 🏓 {latency}ms (テキスト形式)")

@bot.command(name="hello")
async def text_hello(ctx: commands.Context):
    await ctx.send(f"こんにちは、{ctx.author.mention} さん！ (テキスト形式)")


# --------------------------------------------------
# スラッシュコマンド（/から始まるコマンド）
# --------------------------------------------------
@bot.tree.command(name="ping", description="応答速度を確認します")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! 🏓 {latency}ms")


@bot.tree.command(name="hello", description="あいさつします")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(f"こんにちは、{interaction.user.mention} さん！")


@bot.tree.command(name="search", description="Web検索します")
@app_commands.describe(query="検索したい内容")
async def search_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    try:
        answer = await search_answer(query)
        await interaction.followup.send(answer, ephemeral=False)
    except Exception as e:
        await interaction.followup.send(f"検索エラー: {e}", ephemeral=True)


# 🎟️ 新機能：チケット開設パネルを設置するコマンド
@bot.tree.command(name="ticket_setup", description=f"チケット受付窓口（ボタン）をこのチャンネルに設置します{admin_label()}")
async def ticket_setup(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ 管理者のみ使用できます。", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🎟️ サポート・お問い合わせ窓口",
        description="困ったことや、運営へのご要望、不具合報告などがある場合は、下のボタンを押してチケットを開設してください。\n\nあなたと管理者だけが見える専用のチャンネルが作成されます。",
        color=discord.Color.blue()
    )
    # ボタン付きのメッセージを送信
    await interaction.channel.send(embed=embed, view=TicketButton())
    await interaction.response.send_message("✅ チケット受付パネルを設置しました！", ephemeral=True)


# 🎟️ 新機能：用件が終わったチケットチャンネルを削除するコマンド
@bot.tree.command(name="ticket_close", description=f"このチケットチャンネルを閉じます（削除します）{admin_label()}")
async def ticket_close(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ 管理者のみ使用できます。", ephemeral=True)
        return
    
    if not interaction.channel.name.startswith("ticket-"):
        await interaction.response.send_message("❌ このコマンドはチケットチャンネル（ticket-xxx）内でのみ実行できます。", ephemeral=True)
        return

    await interaction.response.send_message("🔄 5秒後にこのチケットをクローズ（チャンネル削除）します…")
    append_audit_log("ticket_closed", f"Ticket channel #{interaction.channel.name} was closed by {interaction.user}")
    await asyncio.sleep(5)
    await interaction.channel.delete()


@bot.tree.command(name="news_set", description=f"ニュース配信先チャンネルを設定します{owner_label()}")
@app_commands.describe(channel_id="配信先のチャンネルID")
async def news_set(interaction: discord.Interaction, channel_id: str):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ オーナーのみ使用できます。", ephemeral=True)
        return
    try:
        cid = int(channel_id)
    except ValueError:
        await interaction.response.send_message("❌ チャンネルIDが無効です。", ephemeral=True)
        return
    
    channels = load_news_channels()
    channels[str(interaction.guild.id)] = cid
    save_news_channels(channels)
    
    set_news_setting(interaction.guild.id, "theme", "最新ニュース")
    set_news_setting(interaction.guild.id, "interval", str(NEWS_POLL_SECONDS))
    append_audit_log("command", f"{interaction.user} set news channel to {cid}")
    await interaction.response.send_message(f"✅ ニュース配信先を設定しました。`{cid}`", ephemeral=True)


@bot.tree.command(name="news_off", description=f"ニュース配信を停止します{owner_label()}")
async def news_off(interaction: discord.Interaction):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ オーナーのみ使用できます。", ephemeral=True)
        return
    channels = load_news_channels()
    channels.pop(str(interaction.guild.id), None)
    save_news_channels(channels)
    append_audit_log("command", f"{interaction.user} disabled news")
    await interaction.response.send_message("✅ ニュース配信を停止しました。", ephemeral=True)


@bot.tree.command(name="news_status", description=f"ニュース配信先を表示します{owner_label()}")
async def news_status(interaction: discord.Interaction):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ オーナーのみ使用できます。", ephemeral=True)
        return
    channels = load_news_channels()
    cid = channels.get(str(interaction.guild.id))
    if cid is None:
        await interaction.response.send_message("ニュース配信先は未設定です。", ephemeral=True)
        return
    setting = get_news_setting(interaction.guild.id)
    theme = setting.get("theme", "最新ニュース")
    interval = setting.get("interval", str(NEWS_POLL_SECONDS))
    await interaction.response.send_message(f"ニュース配信先: `{cid}`\nテーマ: `{theme}`\n頻度: `{interval}`秒", ephemeral=True)


@bot.tree.command(name="news_theme", description=f"ニュースの投稿テーマを設定します{owner_label()}")
@app_commands.describe(theme="例: AI, 競馬, ゲーム, 事件")
async def news_theme(interaction: discord.Interaction, theme: str):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ オーナーのみ使用できます。", ephemeral=True)
        return
    set_news_setting(interaction.guild.id, "theme", theme)
    append_audit_log("command", f"{interaction.user} set news theme to {theme}")
    await interaction.response.send_message(f"✅ テーマを `{theme}` に設定しました。", ephemeral=True)


@bot.tree.command(name="news_interval", description=f"ニュース投稿頻度を設定します{owner_label()}")
@app_commands.describe(seconds="投稿間隔（秒）")
async def news_interval(interaction: discord.Interaction, seconds: int):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ オーナーのみ使用できます。", ephemeral=True)
        return
    if seconds < 30:
        await interaction.response.send_message("❌ 30秒以上で指定してください。", ephemeral=True)
        return
    set_news_setting(interaction.guild.id, "interval", str(seconds))
    append_audit_log("command", f"{interaction.user} set news interval to {seconds}")
    await interaction.response.send_message(f"✅ 頻度を `{seconds}` 秒に設定しました。", ephemeral=True)


@bot.tree.command(name="restart", description=f"ボットを再起動します{owner_label()}")
async def restart_cmd(interaction: discord.Interaction):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ オーナーのみ使用できます。", ephemeral=True)
        return
    append_audit_log("command", f"{interaction.user} used /restart")
    await interaction.response.send_message("🔄 再起動します…", ephemeral=True)
    os.execv(sys.executable, [sys.executable] + sys.argv)


@bot.tree.command(name="say", description=f"ボットとして発言します{owner_label()}")
@app_commands.describe(channel="送信先チャンネル", message="メッセージ内容")
async def say(interaction: discord.Interaction, channel: discord.TextChannel, message: str):

    bot.run(os.environ['DISCORD_BOT_TOKEN'])
    
