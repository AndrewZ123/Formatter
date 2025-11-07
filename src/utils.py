from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import random
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv()


class Settings(BaseModel):
    discord_token: str
    channel_id: int
    mavely_email: str
    mavely_password: str
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = "gpt-4o"
    openai_web_model: Optional[str] = None
    enable_openai_web: bool = False
    headful: bool = True
    debug_extract: bool = False
    debug_dir: str = "debug-artifacts"

    model_config = {
        "extra": "ignore"
    }


def _parse_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def load_settings() -> Settings:
    raw = {
        "discord_token": os.getenv("DISCORD_TOKEN"),
        "channel_id": os.getenv("CHANNEL_ID"),
        "mavely_email": os.getenv("MAVELY_EMAIL"),
        "mavely_password": os.getenv("MAVELY_PASSWORD"),
        "openai_api_key": os.getenv("OPENAI_API_KEY"),
    "openai_model": os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
    "openai_web_model": os.getenv("OPENAI_WEB_MODEL"),
    "enable_openai_web": _parse_bool(os.getenv("ENABLE_OPENAI_WEB"), False),
    "headful": _parse_bool(os.getenv("HEADFUL"), True),
        "debug_extract": _parse_bool(os.getenv("DEBUG_EXTRACT"), False),
        "debug_dir": os.getenv("DEBUG_DIR") or "debug-artifacts",
    }

    try:
        raw["channel_id"] = int(raw["channel_id"]) if raw["channel_id"] else None
        settings = Settings(**raw)
    except (ValidationError, ValueError) as exc:
        missing = [key for key, value in raw.items() if value in (None, "") and key in {"discord_token", "channel_id", "mavely_email", "mavely_password"}]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}") from exc
        raise RuntimeError(f"Invalid configuration: {exc}") from exc

    debug_dir = pathlib.Path(settings.debug_dir)
    if settings.debug_extract:
        debug_dir.mkdir(parents=True, exist_ok=True)

    return settings


@dataclass
class ProductInfo:
    title: Optional[str] = None
    original_price: Optional[str] = None
    sale_price: Optional[str] = None
    currency: Optional[str] = None
    confidence: float = 0.0
    source: Optional[str] = None
    image_url: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PageSnapshot:
    url: str
    html: str
    text: str
    json_ld: list[str]
    price_strings: list[str]
    metadata: Dict[str, Any]
    screenshot_path: Optional[str] = None


_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.78 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

_VIEWPORT_CHOICES = [
    (1280, 720),
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1920, 1080),
]

_TIMEZONES = ["America/New_York", "America/Los_Angeles", "Europe/London", "America/Chicago"]


class PlaywrightSession:
    """Single shared Playwright session with helper factories."""

    def __init__(self, headful: bool) -> None:
        self._headful = headful
        self._playwright = None
        self._chromium = None
        self._startup_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._playwright:
            return
        async with self._startup_lock:
            if self._playwright:
                return
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._chromium = self._playwright.chromium

    async def stop(self) -> None:
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
            self._chromium = None

    def _default_launch_args(self) -> list[str]:
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-infobars",
            "--allow-running-insecure-content",
        ]
        if self._headful:
            args.append("--start-maximized")
        return args

    def _default_context_options(self) -> Dict[str, Any]:
        width, height = random.choice(_VIEWPORT_CHOICES)
        return {
            "viewport": {"width": width, "height": height},
            "user_agent": random.choice(_USER_AGENTS),
            "timezone_id": random.choice(_TIMEZONES),
            "locale": "en-US",
            "color_scheme": random.choice(["light", "dark"]),
        }

    async def _apply_stealth(self, context) -> None:
        # Stealth disabled for now
        pass

    async def launch_transient_context(self, **kwargs):
        """Create a fresh browser + context for one-off interactions."""
        await self.start()

        context_options = self._default_context_options()
        context_options.update(kwargs)

        launch_kwargs: Dict[str, Any] = {
            "headless": not self._headful,
            "args": self._default_launch_args(),
        }
        slow_mo = random.randint(50, 150) if self._headful else 0
        if slow_mo:
            launch_kwargs["slow_mo"] = slow_mo

        browser = await self._chromium.launch(**launch_kwargs)
        context = await browser.new_context(**context_options)
        context.set_default_navigation_timeout(45000)
        context.set_default_timeout(45000)
        await self._apply_stealth(context)
        if not context.pages:
            await context.new_page()
        return browser, context

    async def launch_persistent_context(self, user_data_dir: str, **kwargs):
        """Launch a persistent Chromium context stored at user_data_dir."""
        await self.start()

        context_options = self._default_context_options()
        context_options.update(kwargs)

        launch_kwargs: Dict[str, Any] = {
            "user_data_dir": user_data_dir,
            "headless": not self._headful,
            "args": self._default_launch_args(),
        }
        slow_mo = random.randint(50, 150) if self._headful else 0
        if slow_mo:
            launch_kwargs["slow_mo"] = slow_mo

        context = await self._chromium.launch_persistent_context(**launch_kwargs, **context_options)
        context.set_default_navigation_timeout(45000)
        context.set_default_timeout(45000)
        await self._apply_stealth(context)
        return context


def configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def dump_debug_payload(debug_dir: str, prefix: str, payload: Dict[str, Any]) -> pathlib.Path:
    path = pathlib.Path(debug_dir) / f"{prefix}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_site_profiles(path: str = "site-profiles.json") -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        logging.info("site-profiles.json not found; continuing without custom rules")
        return {}
    except json.JSONDecodeError as exc:
        logging.warning("Failed to parse site profiles: %s", exc)
        return {}
