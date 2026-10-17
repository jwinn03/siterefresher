# Site Refresher

Perpetually polls a website at a **random interval between 70 and 90 seconds**,
watches for a target phrase (default: **"Tickets Currently Not Available"**), and
sends a **Telegram** notification when that phrase changes — appears, disappears,
or is removed (e.g. when tickets become available).

Cross-platform: runs on **Windows** and **Linux / Raspberry Pi OS**. The only
dependency is [`requests`](https://pypi.org/project/requests/).

## How it works

1. Fetches the page (via plain HTTP or a real browser — see *Fetch backends*).
2. Checks whether the configured phrase is present in the HTML.
3. Compares to the last-seen state (persisted in `state.json`, so restarts don't
   re-alert).
4. On a change, sends a Telegram message.
5. Sleeps a random 70–90 s (configurable) and repeats forever.

It also optionally alerts if the site becomes unreachable, and again when it
recovers.

## Fetch backends

Set `fetch_backend` in `config.ini`:

- **`requests`** (default) — fast, no browser. Works for ordinary
  server-rendered pages. If the target returns a bot-protection challenge, the
  program raises a clear error instead of silently misreporting the phrase.
- **`selenium`** — drives a real Chromium browser, executes JavaScript, and gets
  past bot-protection challenges. **Required for JS-rendered / protected sites.**

### Important: anti-bot protection (e.g. etix.com)

Some ticketing sites layer bot protection in front of the page:

- **AWS WAF** serves a non-interactive JavaScript challenge. A browser (the
  `selenium` backend) solves it automatically.
- **DataDome** performs a "device check" that **specifically detects and blocks
  headless browsers**. To get past it you must run the browser **non-headless**
  (`headless = false`). On a desktop a window appears; on a headless Pi/server
  you supply a virtual display with `xvfb` (see below).

`etix.com` uses *both*. The working configuration is `fetch_backend = selenium`
with `headless = false`, plus the anti-detection measures below.

### Getting past DataDome (the "Access Temporarily Blocked" page)

DataDome fingerprints the browser and blocks anything that looks automated. If
you see **"Access Temporarily Blocked" / "temporarily restricted access"**, the
client was flagged. To avoid it:

1. **Leave `user_agent` blank.** A spoofed UA that disagrees with the real
   platform (e.g. a *Windows* UA sent from a *Linux ARM* Pi) is an instant
   giveaway. Blank = the browser's own, consistent UA.
2. **Set `use_undetected = true`** (`pip install undetected-chromedriver`). This
   drives a patched Chromedriver that strips the Selenium automation markers.
3. **Set `headless = false`** (with `xvfb` on a headless Pi — see below).
4. **Warm a persistent profile.** Point `user_data_dir` at a folder, open
   Chromium with that profile once, pass the page manually, and the `datadome`
   cookie persists so automated runs stay unblocked:
   ```bash
   chromium --user-data-dir=/home/pi/siterefresher/chrome-profile \
            https://www.etix.com/ticket/p/70542252
   # solve/verify, then close. The watcher reuses this profile.
   ```
5. **A flagged block is temporary** — wait a while (minutes–hours) for it to lift
   before retrying, and keep the 70–90 s interval (don't hammer it).

> Note: DataDome is an active arms race. The steps above work in practice but can
> need revisiting if the site tightens its protection. A datacenter/VPN IP is far
> more likely to be blocked than a residential one (like your Pi at home).

## Setup

### 1. Install Python & dependencies

Python 3.9+ is required.

```bash
# Linux / Raspberry Pi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# For the selenium backend on Raspberry Pi OS, also install Chromium + xvfb:
sudo apt install chromium chromium-driver xvfb
# then set in config.ini:  browser_binary = /usr/bin/chromium
#                          driver_path    = /usr/bin/chromedriver
```

```powershell
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Create a Telegram bot

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts.
   Copy the **bot token** it gives you.
2. Send any message to your new bot (this lets it message you back).
3. Get your chat id — open this URL in a browser (replace `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   and read `result[].message.chat.id`.

### 3. Configure

```bash
cp config.example.ini config.ini   # (copy config.example.ini config.ini on Windows)
```

Edit `config.ini` and set at least `url`, `telegram token`, and `chat_id`.

### 4. Test Telegram

```bash
python site_refresher.py --test-telegram
```

You should receive a test message. Then run a single check:

```bash
python site_refresher.py --once
```

### 5. Run

```bash
python site_refresher.py
```

Leave it running; stop with `Ctrl+C`.

## Running 24/7 on a Raspberry Pi (systemd)

The provided service file wraps the program in `xvfb-run` so a non-headless
Chromium works without a desktop (needed for DataDome sites like etix.com).

```bash
# Place the project in /home/pi/siterefresher and create the venv there.
sudo cp site-refresher.service /etc/systemd/system/
# Edit the service file first if your user/paths differ.
sudo systemctl daemon-reload
sudo systemctl enable --now site-refresher.service

# Check status / logs:
systemctl status site-refresher.service
journalctl -u site-refresher.service -f
```

The service restarts automatically on crash or reboot.

## Running on Windows at startup

Use **Task Scheduler** → *Create Task* → trigger "At log on" → action:
`...\.venv\Scripts\python.exe` with argument `...\site_refresher.py`, and set
"Start in" to the project folder. Or just run it in a terminal.

## Configuration reference

All settings live in `config.ini` (see `config.example.ini`). Every value can be
overridden by an environment variable `SR_<SECTION>_<KEY>`, e.g.:

```bash
export SR_WATCH_URL="https://example.com/tickets"
export SR_TELEGRAM_TOKEN="123:ABC"
export SR_TELEGRAM_CHAT_ID="987654321"
```

| Section    | Key              | Default                          | Meaning                                   |
|------------|------------------|----------------------------------|-------------------------------------------|
| `watch`    | `url`            | *(required)*                     | Page to monitor                           |
| `watch`    | `phrase`         | `Tickets Currently Not Available`| Phrase whose presence is tracked          |
| `watch`    | `min_interval`   | `70`                             | Min seconds between checks                 |
| `watch`    | `max_interval`   | `90`                             | Max seconds between checks                 |
| `watch`    | `fetch_backend`  | `requests`                       | `requests` or `selenium`                  |
| `watch`    | `page_load_timeout`| `30`                           | Max s to wait for JS/challenge to render  |
| `watch`    | `request_timeout`| `30`                             | HTTP timeout (seconds, requests backend)  |
| `watch`    | `case_sensitive` | `false`                          | Match phrase case-sensitively             |
| `watch`    | `verify_tls`     | `true`                           | Verify TLS certificates                    |
| `watch`    | `state_file`     | `state.json`                     | Where last-seen state is stored           |
| `watch`    | `user_agent`     | *(Chrome-like)*                  | HTTP User-Agent header                     |
| `selenium` | `headless`       | `true`                           | Run browser headless (set `false` for DataDome) |
| `selenium` | `use_undetected` | `false`                          | Use undetected-chromedriver (set `true` for DataDome) |
| `selenium` | `user_data_dir`  | *(none)*                         | Persistent profile dir (warm the DataDome cookie) |
| `selenium` | `browser_binary` | *(auto)*                         | Path to Chrome/Chromium (Pi: `/usr/bin/chromium`) |
| `selenium` | `driver_path`    | *(auto)*                         | Path to chromedriver (Pi: `/usr/bin/chromedriver`) |
| `telegram` | `token`          | *(empty)*                        | Bot token from @BotFather                 |
| `telegram` | `chat_id`        | *(empty)*                        | Your chat id                              |
| `telegram` | `notify_on_start`| `true`                           | Send a message when the watcher starts    |
| `telegram` | `notify_on_errors`| `true`                          | Alert when the site is unreachable        |
| `telegram` | `error_threshold`| `3`                              | Consecutive failures before erroring out  |

## Command-line options

| Flag               | Purpose                                      |
|--------------------|----------------------------------------------|
| `-c, --config`     | Path to config file (default `config.ini`)   |
| `-v, --verbose`    | Debug logging                                |
| `--test-telegram`  | Send a test message and exit                 |
| `--once`           | Run a single check and exit                  |

## Notes

- If Telegram isn't configured, the program still runs and logs changes to the
  console — it just won't message you.
- The first check after a fresh start only records a baseline; it won't alert.
  Subsequent changes trigger notifications.
- Be considerate of the target site's terms of service and rate limits.
