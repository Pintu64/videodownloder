import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ["8975252170:AAESG_NZ7P59tJw-pn1JTwYPSMCUTt674Zg"]
TERABOX_GATEWAY = os.getenv(
    "TERABOX_GATEWAY",
    "https://tbx-proxy.shakir-ansarii075.workers.dev/"
)
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "49"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "900"))
INSTAGRAM_COOKIES_FILE = os.getenv("INSTAGRAM_COOKIES_FILE", "").strip()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("media-bot")

TERABOX_HOSTS = (
    "terabox.com", "www.terabox.com", "1024terabox.com",
    "www.1024terabox.com", "terabox.app", "www.terabox.app",
    "freeterabox.com", "www.freeterabox.com", "teraboxshare.com",
    "teraboxlink.com", "terasharefile.com", "terafileshare.com",
    "terasharelink.com", "teraboxurl.com", "1024tera.com",
)

URL_RE = re.compile(r"https?://[^\s<>()]+")


def is_terabox(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in TERABOX_HOSTS)


def is_instagram(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"instagram.com", "www.instagram.com", "m.instagram.com"}


def clean_url(url: str) -> str:
    return url.rstrip(".,!?)]}>\"'")


def safe_name(name: str, default: str = "video.mp4") -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip()
    if not name:
        name = default
    if not Path(name).suffix:
        name += ".mp4"
    return name[:180]


async def http_json(url: str, params=None) -> dict:
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            url,
            params=params,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"Resolver HTTP {r.status}: {text[:300]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise RuntimeError("Resolver returned invalid JSON") from e


def extract_terabox_items(data: dict) -> list[dict]:
    """
    Accept several known TeraBox gateway response shapes.
    Each returned item needs either a direct link or a stream/share URL.
    """
    items = []
    payload = data.get("data", data)

    if isinstance(payload, dict):
        candidates = payload.get("files") or payload.get("list") or []
        if isinstance(candidates, dict):
            candidates = [candidates]
    elif isinstance(payload, list):
        candidates = payload
    else:
        candidates = []

    if not candidates and isinstance(payload, dict):
        # Some resolvers return a single file in data itself.
        if any(k in payload for k in ("dlink", "download_link", "direct_link", "url")):
            candidates = [payload]

    for i, item in enumerate(candidates, 1):
        if not isinstance(item, dict):
            continue
        name = (
            item.get("name")
            or item.get("filename")
            or item.get("server_filename")
            or f"terabox_{i}.mp4"
        )
        link = (
            item.get("dlink")
            or item.get("download_link")
            or item.get("direct_link")
            or item.get("url")
        )
        stream = item.get("stream_url") or item.get("m3u8")
        size = item.get("size") or item.get("filesize") or 0
        items.append({
            "name": safe_name(str(name)),
            "link": link,
            "stream": stream,
            "size": int(size) if str(size).isdigit() else 0,
        })
    return items


async def resolve_terabox(share_url: str) -> list[dict]:
    # First use the public gateway's resolver. It is a replaceable endpoint;
    # no API key is required by this default gateway.
    data = await http_json(
        TERABOX_GATEWAY,
        {"mode": "resolve", "surl": extract_surl(share_url)},
    )
    items = extract_terabox_items(data)
    if not items:
        # Try the legacy URL mode supported by the same project.
        data = await http_json(
            TERABOX_GATEWAY.rstrip("/") + "/api",
            {"url": share_url, "resolve": "true"},
        )
        items = extract_terabox_items(data)
    if not items:
        raise RuntimeError(
            "TeraBox resolver returned no downloadable files. "
            "The public resolver may be temporarily unavailable or the share is protected."
        )
    return items


def extract_surl(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    # Most current links are /s/<short-id>, but keep the final path component
    # for mirror formats as well.
    parts = path.split("/")
    if "s" in parts:
        idx = parts.index("s")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return parts[-1] if parts else url


async def download_http(url: str, output: Path) -> None:
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers, allow_redirects=True) as r:
            if r.status >= 400:
                raise RuntimeError(f"Download HTTP {r.status}")
            with output.open("wb") as f:
                async for chunk in r.content.iter_chunked(1024 * 1024):
                    f.write(chunk)


async def download_hls(url: str, output: Path) -> None:
    # ffmpeg handles HLS and remuxes the stream into a normal MP4.
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", url,
        "-c", "copy",
        "-movflags", "+faststart",
        str(output),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=DOWNLOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("ffmpeg timed out")
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore")[-1000:] or "ffmpeg failed")


async def download_instagram(url: str, directory: Path) -> list[Path]:
    output_template = str(directory / "%(playlist_index&{}|)s%(id)s.%(ext)s")
    # A simple template is more portable; yt-dlp sanitizes the title/id itself.
    output_template = str(directory / "%(playlist_index)03d_%(id)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--ignore-errors",
        "--yes-playlist",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", output_template,
        url,
    ]
    if INSTAGRAM_COOKIES_FILE:
        cmd[1:1] = ["--cookies", INSTAGRAM_COOKIES_FILE]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=DOWNLOAD_TIMEOUT
    )
    if proc.returncode != 0:
        err = stderr.decode(errors="ignore")
        raise RuntimeError(
            "Instagram download failed. The post may be private, rate-limited, "
            "or require cookies.\n" + err[-900:]
        )

    results = [p for p in directory.iterdir() if p.is_file()]
    if not results:
        raise RuntimeError("Instagram returned no downloadable media.")
    return sorted(results)


async def send_one(bot, chat_id: int, path: Path, caption: str = "") -> None:
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        raise RuntimeError(
            f"{path.name} is {size_mb:.1f} MB. "
            f"This deployment uses Telegram's standard Bot API upload limit; "
            f"maximum configured here is {MAX_FILE_MB} MB."
        )

    # Telegram clients can play MP4s when sent as video.
    if path.suffix.lower() == ".mp4":
        with path.open("rb") as f:
            await bot.send_video(
                chat_id=chat_id,
                video=f,
                caption=caption[:1024],
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )
    else:
        with path.open("rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                caption=caption[:1024],
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )


async def process_url(update: Update, url: str) -> None:
    chat_id = update.effective_chat.id
    work = Path(tempfile.mkdtemp(prefix="media_bot_"))
    try:
        await update.message.chat.send_action(ChatAction.TYPING)

        if is_instagram(url):
            await update.message.reply_text("🔎 Checking Instagram…")
            files = await download_instagram(url, work)

        elif is_terabox(url):
            await update.message.reply_text("🔎 Resolving TeraBox…")
            items = await resolve_terabox(url)
            files = []

            for index, item in enumerate(items, 1):
                name = safe_name(item["name"], f"terabox_{index}.mp4")
                target = work / name

                if item.get("stream"):
                    await update.message.reply_text(
                        f"⬇️ Downloading {index}/{len(items)}: {name}"
                    )
                    await download_hls(item["stream"], target)
                elif item.get("link"):
                    await update.message.reply_text(
                        f"⬇️ Downloading {index}/{len(items)}: {name}"
                    )
                    await download_http(item["link"], target)
                else:
                    continue

                files.append(target)
        else:
            await update.message.reply_text(
                "Send an Instagram or TeraBox video link."
            )
            return

        if not files:
            raise RuntimeError("No media was downloaded.")

        for index, path in enumerate(files, 1):
            await update.message.reply_text(
                f"📤 Sending {index}/{len(files)}: {path.name}"
            )
            try:
                await send_one(bot=update.get_bot(), chat_id=chat_id, path=path)
            except Exception as exc:
                await update.message.reply_text(f"⚠️ {path.name}: {exc}")

        await update.message.reply_text(
            f"✅ Finished. Sent {len(files)} file(s) separately."
        )

    except Exception as exc:
        log.exception("Processing failed")
        await update.message.reply_text(f"❌ {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎬 Media Downloader Bot\n\n"
        "Send an Instagram Reel/video URL or a public TeraBox share URL.\n"
        "Each video/file is sent separately — no ZIP files."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Supported:\n"
        "• Instagram Reel/video URLs\n"
        "• Public TeraBox share URLs\n\n"
        "Notes:\n"
        "• Private/login-required Instagram posts need cookies.\n"
        "• Telegram's standard Bot API has an upload-size limit; this bot rejects oversized files instead of silently failing."
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    urls = [clean_url(x) for x in URL_RE.findall(update.message.text)]
    supported = [u for u in urls if is_instagram(u) or is_terabox(u)]
    if not supported:
        await update.message.reply_text(
            "Please send a supported Instagram or TeraBox URL."
        )
        return

    # Process URLs sequentially to avoid filling Railway disk/RAM.
    for url in supported[:5]:
        await process_url(update, url)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled bot error", exc_info=context.error)


def main() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required and was not found in PATH.")
    if shutil.which("yt-dlp") is None:
        raise RuntimeError("yt-dlp is required and was not found in PATH.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    log.info("Bot started with long polling.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
