# Telegram Instagram + TeraBox Downloader

Railway-ready Python Telegram bot.

## What it does

- Accepts Instagram Reel/video URLs.
- Accepts public TeraBox share URLs.
- Downloads media to temporary disk.
- Sends every video/file as a separate Telegram message.
- Does NOT create ZIP archives.
- Uses `yt-dlp` for Instagram; no Instagram API key is required.
- Uses a configurable TeraBox gateway. The default is a public resolver, so no TeraBox API key is required to start.
- Deletes temporary files after processing.

## Railway deployment

1. Create a Telegram bot with `@BotFather`.
2. Create a new Railway service from this folder/repository.
3. Railway will build the included Dockerfile.
4. Add this variable in Railway:
   - `BOT_TOKEN` = your BotFather token
5. Deploy.
6. Open your bot and send `/start`.
7. Paste an Instagram or TeraBox URL.

Railway supports long-running Telegram bots using long polling, so this bot does not need a webhook or public domain.

## Important Telegram size limit

The standard hosted Telegram Bot API currently documents bot uploads of up to 50 MB. This project intentionally uses a 49 MB safety limit and reports an error for larger files instead of pretending the upload succeeded.

If you specifically need multi-hundred-MB/GB files, you need Telegram's Local Bot API Server, which is a different deployment and requires Telegram API credentials (`api_id` and `api_hash`) in addition to the bot token.

## Instagram limitations

Public Instagram media can work without credentials, but Instagram may require login, cookies, or rate-limit access. If a public Reel fails, that is normally an Instagram access restriction rather than a Telegram problem.

For private/login-required media, set `INSTAGRAM_COOKIES_FILE` and provide a valid Netscape-format cookies file. Do not commit cookies to GitHub.

## TeraBox limitations

TeraBox changes its web/API behavior frequently. The bot isolates TeraBox resolution behind `TERABOX_GATEWAY`.

The default public gateway is used without an API key. Public third-party resolvers can go offline, rate-limit, or change behavior. If that happens, deploy your own compatible TeraBox gateway and change:

`TERABOX_GATEWAY=https://YOUR-GATEWAY/`

The gateway used by this bot should return file metadata containing a direct download link (`dlink`/`download_link`/`direct_link`) or an HLS stream URL (`stream_url`/`m3u8`).

## Local test

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN="..."
python bot.py
```

On Windows PowerShell:

```powershell
$env:BOT_TOKEN="..."
python bot.py
```

FFmpeg must be installed locally.

## Usage

Send:

- `https://www.instagram.com/reel/...`
- `https://www.instagram.com/p/...`
- `https://terabox.com/s/...`
- `https://1024terabox.com/s/...`

For a post containing several downloadable videos, the bot sends them individually.
