"""
platform_pool.py
Manages persistent browser sessions for all platforms.

Key improvements over per-bet approach:
  - Browsers start ONCE at agent startup and stay alive
  - Login happens ONCE (not every 5-min cycle)
  - All 4 platforms searched in PARALLEL with asyncio.gather
  - Session health check auto-re-logs-in if a platform expires

Speed before: ~3-4 min per bet (sequential login + search x4)
Speed after:  ~15-20 sec per bet (parallel, no login overhead)
"""

import asyncio
import logging
from typing import Optional
from platforms import PLATFORM_MAP
from platforms.base import BasePlatformScraper

logger = logging.getLogger(__name__)


class PlatformPool:
    """
    Holds one live browser session per platform.
    Call initialize() once at startup, then search_all_parallel() for each bet.
    """

    def __init__(self, config: dict):
        self.config = config
        self._scrapers: dict[str, BasePlatformScraper] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self, platform_names: list[str]):
        """Start all browsers and log in — runs once at agent startup."""
        logger.info(f"[Pool] Starting browsers for: {platform_names}")
        tasks = [self._start_platform(name) for name in platform_names]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        ok = 0
        for name, result in zip(platform_names, results):
            if isinstance(result, Exception):
                logger.error(f"[Pool] Failed to start {name}: {result}")
            elif result:
                ok += 1
        logger.info(f"[Pool] {ok}/{len(platform_names)} platforms ready")
        return ok

    async def shutdown(self):
        """Close all browser sessions cleanly."""
        tasks = [s.stop() for s in self._scrapers.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._scrapers.clear()
        logger.info("[Pool] All browsers closed")

    # ── Search ────────────────────────────────────────────────────────────────

    async def search_all_parallel(self, bet: dict) -> dict[str, list[dict]]:
        """
        Search ALL live platforms at the same time.
        Returns {platform_name: [candidates...]}
        """
        tasks = {name: self._search_one(name, bet) for name in self._scrapers}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        out: dict[str, list[dict]] = {}
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"[Pool] Search error on {name}: {result}")
                out[name] = []
            else:
                out[name] = result or []

        return out

    async def _search_one(self, name: str, bet: dict) -> list[dict]:
        """Search a single platform. Re-logins reactively only on actual failure."""
        scraper = self._scrapers.get(name)
        if not scraper:
            return []

        async with self._locks[name]:
            try:
                candidates = await scraper.search_bets(bet)
                return candidates
            except Exception as e:
                # search_bets threw — attempt a re-login then retry once
                logger.warning(f"[Pool] {name} search error ({type(e).__name__}: {e}) — retrying after re-login...")
                scraper.is_logged_in = False
                try:
                    if await scraper.login_with_retry(max_attempts=2):
                        return await scraper.search_bets(bet)
                except Exception as e2:
                    logger.error(f"[Pool] {name} still failing after re-login: {e2}")
                return []

    async def submit_one(
        self,
        name: str,
        bet: dict,
        matched: dict,
        stake: float,
        max_risk: float,
        confirm_password: str = "",
        dry_run: bool = False,
    ) -> dict:
        """Submit one matched bet on a single platform with retry/re-login logic."""
        scraper = self._scrapers.get(name)
        if not scraper:
            return {"success": False, "status": "platform_not_ready", "platform": name}

        async with self._locks[name]:
            try:
                return await scraper.submit_bet(
                    bet=bet,
                    matched=matched,
                    stake=stake,
                    max_risk=max_risk,
                    confirm_password=confirm_password,
                    dry_run=dry_run,
                )
            except Exception as e:
                logger.warning(f"[Pool] {name} submit error ({type(e).__name__}: {e}) — retrying after re-login...")
                scraper.is_logged_in = False
                try:
                    if await scraper.login_with_retry(max_attempts=2):
                        return await scraper.submit_bet(
                            bet=bet,
                            matched=matched,
                            stake=stake,
                            max_risk=max_risk,
                            confirm_password=confirm_password,
                            dry_run=dry_run,
                        )
                except Exception as e2:
                    logger.error(f"[Pool] {name} still failing submit after re-login: {e2}")
                return {
                    "success": False,
                    "status": "submit_failed_after_retry",
                    "platform": getattr(scraper, "PLATFORM_NAME", name),
                    "error": f"{type(e).__name__}: {e}",
                }

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _start_platform(self, name: str) -> bool:
        agent_cfg = self.config.get("agent", {})
        platform_cfg = self.config["platforms"][name]
        PlatformClass = PLATFORM_MAP[name]
        headless = agent_cfg.get("headless", True)

        scraper = PlatformClass(platform_cfg, headless=headless)
        self._locks[name] = asyncio.Lock()

        if not await scraper.start():
            logger.error(f"[Pool] {name}: browser failed to start")
            return False

        logged_in = await scraper.login_with_retry(max_attempts=2)
        if logged_in:
            self._scrapers[name] = scraper
            logger.info(f"[Pool] {scraper.PLATFORM_NAME}: ready")
        else:
            await scraper.stop()
            logger.error(f"[Pool] {scraper.PLATFORM_NAME}: login failed at startup")

        return logged_in

    def platform_names(self) -> list[str]:
        return list(self._scrapers.keys())
