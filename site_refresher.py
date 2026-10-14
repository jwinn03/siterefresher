#!/usr/bin/env python3
"""
Site Refresher
==============

Perpetually polls a website at a random interval (default 70-90 seconds),
watches for the presence of a target phrase (default:
"Tickets Currently Not Available"), and sends a Telegram notification when the
phrase appears, disappears, or otherwise changes.

Runs on Windows and Linux (including Raspberry Pi OS). Only external dependency
is `requests` (see requirements.txt).
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

LOG = logging.getLogger("site_refresher")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    url: str
    phrase: str
    telegram_token: str
    telegram_chat_id: str
    min_interval: float
    max_interval: float
    request_timeout: float
    user_agent: str
    state_file: Path
    notify_on_start: bool
    notify_on_errors: bool
    error_threshold: int
    case_sensitive: bool
    verify_tls: bool
    fetch_backend: str
    page_load_timeout: float
    browser_binary: str
    driver_path: str
    headless: bool

    @staticmethod
    def load(path: Path) -> "Config":
        """Load configuration from an INI file, with environment overrides.

        Any value can be overridden with an environment variable named
        SR_<SECTION>_<KEY> (upper-case), e.g. SR_WATCH_URL or
        SR_TELEGRAM_TOKEN. This is handy for systemd / containers.
        """
        parser = configparser.ConfigParser()
        if path.exists():
            parser.read(path, encoding="utf-8")
        else:
            LOG.warning("Config file %s not found; relying on environment variables.", path)

        def get(section: str, key: str, fallback: Optional[str] = None) -> Optional[str]:
            env_key = f"SR_{section.upper()}_{key.upper()}"
            if env_key in os.environ:
                return os.environ[env_key]
            if parser.has_option(section, key):
                return parser.get(section, key)
            return fallback

        def get_float(section: str, key: str, fallback: float) -> float:
            raw = get(section, key)
            return float(raw) if raw is not None and raw != "" else fallback

        def get_int(section: str, key: str, fallback: int) -> int:
            raw = get(section, key)
            return int(raw) if raw is not None and raw != "" else fallback

        def get_bool(section: str, key: str, fallback: bool) -> bool:
            raw = get(section, key)
            if raw is None or raw == "":
                return fallback
            return raw.strip().lower() in ("1", "true", "yes", "on")

        url = get("watch", "url")
        if not url:
            raise SystemExit(
                "ERROR: No URL configured. Set [watch] url in config.ini "
                "or the SR_WATCH_URL environment variable."
            )

        token = get("telegram", "token", "")
        chat_id = get("telegram", "chat_id", "")

        state_file = get("watch", "state_file") or "state.json"

        cfg = Config(
            url=url,
            phrase=get("watch", "phrase", "Tickets Currently Not Available"),
            telegram_token=token or "",
            telegram_chat_id=chat_id or "",
            min_interval=get_float("watch", "min_interval", 70.0),
            max_interval=get_float("watch", "max_interval", 90.0),
            request_timeout=get_float("watch", "request_timeout", 30.0),
            user_agent=get(
                "watch",
                "user_agent",
                "Mozilla/5.0 (compatible; SiteRefresher/1.0; +https://github.com/)",
            ),
            state_file=Path(state_file),
            notify_on_start=get_bool("telegram", "notify_on_start", True),
            notify_on_errors=get_bool("telegram", "notify_on_errors", True),
            error_threshold=get_int("telegram", "error_threshold", 3),
            case_sensitive=get_bool("watch", "case_sensitive", False),
            verify_tls=get_bool("watch", "verify_tls", True),
            fetch_backend=(get("watch", "fetch_backend", "requests") or "requests").strip().lower(),
            page_load_timeout=get_float("watch", "page_load_timeout", 30.0),
            browser_binary=get("selenium", "browser_binary", "") or "",
            driver_path=get("selenium", "driver_path", "") or "",
            headless=get_bool("selenium", "headless", True),
        )

        if cfg.min_interval > cfg.max_interval:
            raise SystemExit("ERROR: min_interval must be <= max_interval.")

        if cfg.fetch_backend not in ("requests", "selenium"):
            raise SystemExit(
                f"ERROR: unknown fetch_backend '{cfg.fetch_backend}' "
                "(expected 'requests' or 'selenium')."
            )

        return cfg


# --------------------------------------------------------------------------- #
# Fetch backends
# --------------------------------------------------------------------------- #
class FetchError(Exception):
    """Raised when a page cannot be fetched/rendered for a real reason."""


class ChallengeError(FetchError):
    """Raised when the response is a bot-protection challenge, not real content."""


# Header set when AWS WAF intercepts a request with a JS challenge / CAPTCHA.
_WAF_HEADER = "x-amzn-waf-action"
# Interstitials served by DataDome (detected on sites like etix.com). These
# block headless browsers specifically — run with headless = false to pass.
_DATADOME_MARKERS = ("captcha-delivery.com", "datadome")
# The AWS WAF JS-challenge shell page. Note: the string "awswaf" can also appear
# in a fully rendered real page, so we only treat it as a challenge when the
# challenge container is present AND the document is still tiny.
_WAF_CHALLENGE_MARKER = "challenge-container"
# Below this many bytes, a document is almost certainly an interstitial/shell
# rather than a rendered page.
_MIN_CONTENT_LENGTH = 3000


def _classify(html: str):
    """Return (is_interstitial, reason) for a rendered document."""
    low = html.lower()
    if any(m in low for m in _DATADOME_MARKERS):
        return True, "DataDome device check (run with headless = false)"
    if _WAF_CHALLENGE_MARKER in low and len(html) < _MIN_CONTENT_LENGTH:
        return True, "AWS WAF JavaScript challenge"
    return False, ""


class RequestsFetcher:
    """Lightweight fetcher using `requests`. Cannot execute JavaScript, so it
    fails loudly on JS/WAF-challenged pages instead of returning junk."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def fetch(self, url: str) -> str:
        try:
            resp = self.session.get(
                url, timeout=self.cfg.request_timeout, verify=self.cfg.verify_tls
            )
        except requests.RequestException as exc:
            raise FetchError(str(exc)) from exc

        action = resp.headers.get(_WAF_HEADER)
        if action or resp.status_code == 202:
            raise ChallengeError(
                f"Site returned a bot-protection challenge "
                f"({_WAF_HEADER}={action or 'n/a'}, status={resp.status_code}). "
                "This page needs the 'selenium' fetch_backend."
            )
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code}")
        return resp.text

    def close(self) -> None:
        self.session.close()


class SeleniumFetcher:
    """Headless-Chromium fetcher. Executes JavaScript and automatically solves
    non-interactive challenges (e.g. AWS WAF 'challenge'), then returns the
    fully rendered page HTML. Keeps one browser instance alive across checks."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.driver = None
        self._build_driver()

    def _build_driver(self) -> None:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "ERROR: the 'selenium' fetch_backend requires Selenium.\n"
                "Install it with:  pip install selenium\n"
                "On Raspberry Pi also:  sudo apt install chromium chromium-driver"
            ) from exc

        opts = Options()
        if self.cfg.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1280,1696")
        opts.add_argument(f"--user-agent={self.cfg.user_agent}")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        if self.cfg.browser_binary:
            opts.binary_location = self.cfg.browser_binary

        service = Service(executable_path=self.cfg.driver_path) if self.cfg.driver_path else Service()

        LOG.info(
            "Launching Chromium (selenium backend, headless=%s)...", self.cfg.headless
        )
        self.driver = webdriver.Chrome(options=opts, service=service)
        self.driver.set_page_load_timeout(self.cfg.page_load_timeout + 15)
        # Hide the webdriver flag from page JS.
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
            )
        except Exception:  # pragma: no cover - not all drivers support CDP
            pass

    def fetch(self, url: str) -> str:
        from selenium.common.exceptions import TimeoutException, WebDriverException

        try:
            self.driver.get(url)
        except TimeoutException:
            # Page load event didn't fire in time, but the DOM may still be
            # usable (challenge pages can keep connections open). Fall through
            # and inspect what we have.
            LOG.debug("Selenium page load timed out; inspecting current DOM.")
        except WebDriverException as exc:
            raise FetchError(f"browser error: {exc}") from exc

        # Wait for any non-interactive challenge to clear and real content to
        # render. The AWS WAF challenge solves itself and reloads the page;
        # DataDome only clears for a non-headless browser.
        deadline = time.monotonic() + self.cfg.page_load_timeout
        last_src = ""
        last_reason = ""
        while True:
            last_src = self.driver.page_source or ""
            interstitial, last_reason = _classify(last_src)
            if not interstitial and len(last_src) >= _MIN_CONTENT_LENGTH:
                return last_src
            if time.monotonic() >= deadline:
                break
            time.sleep(1.0)

        interstitial, last_reason = _classify(last_src)
        if interstitial:
            raise ChallengeError(
                f"Bot-protection challenge did not clear within "
                f"{self.cfg.page_load_timeout:.0f}s: {last_reason}."
            )
        # Not an interstitial, just thin — return what we have so the phrase
        # check can run against it.
        return last_src

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:  # pragma: no cover
                pass
            self.driver = None


def make_fetcher(cfg: Config):
    if cfg.fetch_backend == "selenium":
        return SeleniumFetcher(cfg)
    return RequestsFetcher(cfg)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        if not self.enabled:
            LOG.warning(
                "Telegram not configured (missing token or chat_id); "
                "notifications will be logged only."
            )

    def send(self, text: str) -> bool:
        if not self.enabled:
            LOG.info("[telegram disabled] %s", text)
            return False
        api = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            resp = requests.post(
                api,
                data={
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_web_page_preview": "true",
                },
                timeout=20,
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                LOG.info("Telegram notification sent.")
                return True
            LOG.error("Telegram send failed: %s %s", resp.status_code, resp.text[:300])
            return False
        except requests.RequestException as exc:
            LOG.error("Telegram send error: %s", exc)
            return False


# --------------------------------------------------------------------------- #
# State persistence
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        tmp.replace(path)  # atomic on same filesystem
    except OSError as exc:
        LOG.error("Could not write state file %s: %s", path, exc)


# --------------------------------------------------------------------------- #
# Core watcher
# --------------------------------------------------------------------------- #
class Watcher:
    def __init__(self, cfg: Config, telegram: Telegram, fetcher):
        self.cfg = cfg
        self.telegram = telegram
        self.fetcher = fetcher
        self.state = load_state(cfg.state_file)
        self.consecutive_errors = 0
        self.error_notified = False
        self._stop = False

    def request_stop(self, *_args) -> None:
        LOG.info("Stop requested; finishing current cycle and exiting.")
        self._stop = True

    def fetch(self) -> str:
        return self.fetcher.fetch(self.cfg.url)

    def phrase_present(self, html: str) -> bool:
        if self.cfg.case_sensitive:
            return self.cfg.phrase in html
        return self.cfg.phrase.lower() in html.lower()

    def check_once(self) -> None:
        try:
            html = self.fetch()
        except (FetchError, requests.RequestException) as exc:
            self.consecutive_errors += 1
            LOG.warning(
                "Fetch failed (%d consecutive): %s",
                self.consecutive_errors,
                exc,
            )
            if (
                self.cfg.notify_on_errors
                and not self.error_notified
                and self.consecutive_errors >= self.cfg.error_threshold
            ):
                self.telegram.send(
                    f"⚠️ Site Refresher: failed to reach\n{self.cfg.url}\n"
                    f"{self.consecutive_errors} times in a row.\nLast error: {exc}"
                )
                self.error_notified = True
            return

        # Successful fetch: reset error tracking (notify recovery once).
        if self.consecutive_errors > 0 and self.error_notified:
            self.telegram.send(
                f"✅ Site Refresher: recovered, {self.cfg.url} is reachable again."
            )
        self.consecutive_errors = 0
        self.error_notified = False

        present = self.phrase_present(html)
        prev_present = self.state.get("phrase_present")

        LOG.info(
            'Checked %s -> phrase "%s" %s',
            self.cfg.url,
            self.cfg.phrase,
            "PRESENT" if present else "ABSENT",
        )

        if prev_present is None:
            # First observation — record baseline, do not alert.
            LOG.info("Recording baseline state (phrase_present=%s).", present)
        elif present != prev_present:
            self._notify_change(prev_present, present)

        self.state["phrase_present"] = present
        self.state["last_checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(self.cfg.state_file, self.state)

    def _notify_change(self, prev_present: bool, present: bool) -> None:
        if prev_present and not present:
            msg = (
                f"🎟️ CHANGE DETECTED!\n\n"
                f'The phrase "{self.cfg.phrase}" has been REMOVED from the page.\n'
                f"Tickets may now be available!\n\n{self.cfg.url}"
            )
        else:  # not prev_present and present
            msg = (
                f"ℹ️ CHANGE DETECTED!\n\n"
                f'The phrase "{self.cfg.phrase}" has APPEARED on the page.\n\n'
                f"{self.cfg.url}"
            )
        LOG.warning("STATE CHANGE: phrase_present %s -> %s", prev_present, present)
        self.telegram.send(msg)

    def run(self) -> None:
        LOG.info(
            'Starting watch on %s for phrase "%s" every %.0f-%.0f s.',
            self.cfg.url,
            self.cfg.phrase,
            self.cfg.min_interval,
            self.cfg.max_interval,
        )
        if self.cfg.notify_on_start:
            self.telegram.send(
                f"🚀 Site Refresher started.\nWatching: {self.cfg.url}\n"
                f'Phrase: "{self.cfg.phrase}"'
            )

        while not self._stop:
            self.check_once()
            if self._stop:
                break
            delay = random.uniform(self.cfg.min_interval, self.cfg.max_interval)
            LOG.info("Next check in %.1f s.", delay)
            self._sleep(delay)

        LOG.info("Watcher stopped.")

    def _sleep(self, seconds: float) -> None:
        """Sleep in short slices so stop requests are honoured promptly."""
        end = time.monotonic() + seconds
        while not self._stop and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Perpetually watch a website for a phrase change.")
    ap.add_argument(
        "-c",
        "--config",
        default=os.environ.get("SR_CONFIG", "config.ini"),
        help="Path to config.ini (default: config.ini)",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    ap.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test Telegram message and exit.",
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit (useful for testing / cron).",
    )
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = Config.load(Path(args.config))
    telegram = Telegram(cfg.telegram_token, cfg.telegram_chat_id)

    if args.test_telegram:
        ok = telegram.send("✅ Site Refresher test message — Telegram is configured correctly.")
        return 0 if ok else 1

    fetcher = make_fetcher(cfg)
    watcher = Watcher(cfg, telegram, fetcher)

    # Graceful shutdown on Ctrl+C and SIGTERM (systemd stop).
    signal.signal(signal.SIGINT, watcher.request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, watcher.request_stop)

    try:
        if args.once:
            watcher.check_once()
        else:
            watcher.run()
    finally:
        fetcher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
