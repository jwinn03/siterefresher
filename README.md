# Site Refresher

Perpetually polls a website at a **random interval between 70 and 90 seconds**,
watches for a target phrase (default: **"Tickets Currently Not Available"**), and
sends a **Telegram** notification when that phrase changes — appears, disappears,
or is removed (e.g. when tickets become available).

Cross-platform: runs on **Windows** and **Linux / Raspberry Pi OS**. The only
dependency is [`requests`](https://pypi.org/project/requests/).

## How it works

1. Fetches the page over HTTPS with a browser-like User-Agent.
2. Checks whether the configured phrase is present in the HTML.
3. Compares to the last-seen state (persisted in `state.json`, so restarts don't
   re-alert).
4. On a change, sends a Telegram message.
5. Sleeps a random 70–90 s (configurable) and repeats forever.

It also optionally alerts if the site becomes unreachable, and again when it
recovers.

## Setup

### 1. Install Python & dependencies

Python 3.9+ is required.

```bash
# Linux / Raspberry Pi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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
| `watch`    | `request_timeout`| `30`                             | HTTP timeout (seconds)                     |
| `watch`    | `case_sensitive` | `false`                          | Match phrase case-sensitively             |
| `watch`    | `verify_tls`     | `true`                           | Verify TLS certificates                    |
| `watch`    | `state_file`     | `state.json`                     | Where last-seen state is stored           |
| `watch`    | `user_agent`     | *(Chrome-like)*                  | HTTP User-Agent header                     |
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
