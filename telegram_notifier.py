"""
Telegram notification + command server for the Bet Finder Agent.
Handles outbound alerts AND inbound /status and /help commands.
"""

import asyncio
import logging
import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.error import Conflict, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger(__name__)


# ─── Shared agent state ───────────────────────────────────────────────────────

@dataclass
class AgentState:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_cycle_at: Optional[datetime] = None
    total_cycles: int = 0
    total_bets_scraped: int = 0
    total_matches_found: int = 0
    total_submit_attempts: int = 0
    total_submit_success: int = 0
    last_error: str = ""
    platforms_enabled: list = field(default_factory=list)
    ibetcoin_url: str = "https://reports.ibetcoin.win/Report/OpenBets.aspx"
    platform_urls: dict = field(default_factory=dict)
    # Reference to the platform pool — set after pool.initialize()
    pool: object = field(default=None, repr=False)

    @property
    def uptime_str(self) -> str:
        delta = datetime.now(timezone.utc) - self.started_at
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m}m {s}s"

    @property
    def last_cycle_str(self) -> str:
        if not self.last_cycle_at:
            return "never"
        delta = datetime.now(timezone.utc) - self.last_cycle_at
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        return f"{secs // 60}m ago"


# ─── Quick HTTP ping (no browser) ────────────────────────────────────────────

def _ping(url: str, timeout: int = 6) -> tuple[bool, int]:
    """Returns (reachable, http_status_code).
    Any HTTP response (including 4xx) means the server is up.
    Only connection errors / timeouts mean it's down.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        return True, resp.status
    except urllib.error.HTTPError as e:
        # 4xx/5xx still means the server responded — site is reachable
        return True, e.code
    except Exception:
        return False, 0


def _ping_label(ok: bool, code: int) -> str:
    """Human-readable label for a ping result."""
    if not ok:
        return "❌ UNREACHABLE"
    if code == 200:
        return "✅ Online"
    if code in (401, 403):
        # Site is up but blocks simple HTTP pings — that's normal for betting sites
        return "✅ Online (login required for direct access)"
    if code in (301, 302, 307, 308):
        return "✅ Online (redirect)"
    if code >= 500:
        return f"⚠️ Server error (HTTP {code})"
    return f"✅ Online (HTTP {code})"


# ─── TelegramNotifier ─────────────────────────────────────────────────────────

class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.bot = Bot(token=bot_token)

    async def send_message(self, text: str) -> bool:
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML
            )
            return True
        except TelegramError as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    async def notify_exact_match(self, platform: str, bet: dict, found_bet: dict) -> bool:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = (
            f"🎯 <b>EXACT BET FOUND!</b>\n\n"
            f"⏰ {ts}  |  🏦 <b>{platform}</b>\n\n"
            f"📋 <b>Source bet:</b>\n"
            f"  [{bet.get('sport','')}] {bet.get('event','?')[:60]}\n"
            f"  Market: {bet.get('market','?')}  |  Sel: {bet.get('selection','?')}\n"
            f"  Odds: {bet.get('odds_american') or bet.get('odds','?')}\n\n"
            f"✅ <b>Match on {platform}:</b>\n"
            f"  {found_bet.get('event','?')[:60]}\n"
            f"  Odds: {found_bet.get('odds','?')}\n"
            f"  🔗 {found_bet.get('url','')}"
        )
        return await self.send_message(msg)

    async def notify_similar_match(self, platform: str, bet: dict, found_bet: dict, similarity: float) -> bool:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = (
            f"🔍 <b>SIMILAR BET FOUND ({similarity:.0f}%)</b>\n\n"
            f"⏰ {ts}  |  🏦 <b>{platform}</b>\n\n"
            f"📋 <b>Source bet:</b>\n"
            f"  [{bet.get('sport','')}] {bet.get('event','?')[:60]}\n"
            f"  Market: {bet.get('market','?')}  |  Sel: {bet.get('selection','?')}\n"
            f"  Odds: {bet.get('odds_american') or bet.get('odds','?')}\n\n"
            f"🔄 <b>Similar on {platform}:</b>\n"
            f"  {found_bet.get('event','?')[:60]}\n"
            f"  Odds: {found_bet.get('odds','?')}\n"
            f"  🔗 {found_bet.get('url','')}"
        )
        return await self.send_message(msg)

    async def notify_agent_started(self, platforms: list[str]) -> bool:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        p_list = "  " + "\n  ".join(f"• {p}" for p in platforms)
        msg = (
            f"🤖 <b>Bet Finder Agent Started</b>\n\n"
            f"⏰ {ts}\n\n"
            f"📡 Reading bets from ibetcoin.win\n"
            f"🏦 Monitoring platforms:\n{p_list}\n\n"
            f"Send /status to check live connection health."
        )
        return await self.send_message(msg)

    async def notify_agent_stopped(self, reason: str = "Manual stop") -> bool:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return await self.send_message(
            f"🛑 <b>Bet Finder Agent Stopped</b>\n\n⏰ {ts}\n📝 {reason}"
        )

    async def notify_error(self, platform: str, error: str) -> bool:
        return await self.send_message(
            f"⚠️ <b>Error on {platform}</b>\n❌ {error[:200]}"
        )

    async def notify_new_bets_found(self, bets: list[dict]) -> bool:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"📥 <b>{len(bets)} new open bet(s) from ibetcoin.win</b>\n⏰ {ts}\n"]
        for b in bets[:5]:
            side = b.get("bet_side", "")
            line_str = f" {side.upper()} {b.get('line')}" if side and b.get("line") else ""
            odds = b.get("odds_american") or b.get("odds") or ""
            lines.append(
                f"  🎫 #{b.get('ticket_id')} [{b.get('sport','')}] "
                f"{b.get('event','?')[:50]}{line_str} @ {odds}"
            )
        if len(bets) > 5:
            lines.append(f"  ...and {len(bets)-5} more")
        lines.append(
            "\nEach book is searched on its own session — you’ll get a Telegram "
            "message <b>as each site finishes</b> (fastest first)."
        )
        return await self.send_message("\n".join(lines))

    async def notify_platform_search_done(
        self,
        platform_label: str,
        bet: dict,
        num_candidates: int,
        matcher_hits: int,
        alerts_sent: int,
        elapsed_sec: float,
        error: Optional[str] = None,
    ) -> bool:
        """One message when a single platform’s search + matching is finished."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tid = bet.get("ticket_id", "?")
        sport = bet.get("sport", "")
        ev = (bet.get("event") or bet.get("selection") or "?")[:45]
        if error:
            body = (
                f"🏁 <b>{platform_label}</b> — ticket <code>#{tid}</code>\n"
                f"⏰ {ts}  |  ⏱ {elapsed_sec:.1f}s\n"
                f"❌ <pre>{error[:400]}</pre>"
            )
            return await self.send_message(body)
        cand = f"{num_candidates} candidate line(s)" if num_candidates else "no lines scraped"
        if alerts_sent > 0:
            tail = f"🎯 <b>{alerts_sent}</b> alert(s) sent (see above). [{cand}]"
        elif matcher_hits > 0:
            tail = (
                f"ℹ️ {matcher_hits} match(es) after rules but <b>no alert</b> "
                f"(hedge/filter). [{cand}]"
            )
        else:
            tail = f"No matching line here. [{cand}]"
        body = (
            f"🏁 <b>{platform_label}</b> done — <code>#{tid}</code>\n"
            f"⏰ {ts}  |  ⏱ {elapsed_sec:.1f}s\n"
            f"[{sport}] {ev}\n{tail}"
        )
        return await self.send_message(body)

    async def notify_bet_search_complete(
        self,
        bet: dict,
        alerts_sent: int,
        matcher_hits: int,
        elapsed_sec: float,
        platforms_searched: list[str],
    ) -> bool:
        """Sent after platform search finishes so Telegram is not left at 'Searching...'."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tid = bet.get("ticket_id", "?")
        sport = bet.get("sport", "")
        ev = (bet.get("event") or bet.get("selection") or "?")[:55]
        pl = ", ".join(platforms_searched) if platforms_searched else "—"
        if alerts_sent > 0:
            body = (
                f"✅ <b>Search finished</b> — ticket <code>#{tid}</code>\n"
                f"⏰ {ts}  |  ⏱ {elapsed_sec:.1f}s\n"
                f"🎯 Sent <b>{alerts_sent}</b> match alert(s) — see messages above.\n"
                f"<i>Platforms: {pl}</i>"
            )
        elif matcher_hits > 0:
            body = (
                f"ℹ️ <b>Search finished</b> — ticket <code>#{tid}</code>\n"
                f"⏰ {ts}  |  ⏱ {elapsed_sec:.1f}s\n"
                f"[{sport}] {ev}\n"
                f"Found <b>{matcher_hits}</b> candidate line(s) on book(s) but "
                f"<b>no Telegram alert</b> (e.g. hedge filter, notify toggles, or odds rules).\n"
                f"<i>Platforms: {pl}</i>"
            )
        else:
            body = (
                f"ℹ️ <b>Search finished</b> — ticket <code>#{tid}</code>\n"
                f"⏰ {ts}  |  ⏱ {elapsed_sec:.1f}s\n"
                f"[{sport}] {ev}\n"
                f"<b>No matching lines</b> on any monitored book right now "
                f"(slippage / fuzzy rules may also filter candidates).\n"
                f"<i>Platforms: {pl}</i>"
            )
        return await self.send_message(body)

    async def notify_bet_search_error(self, bet: dict, error: str) -> bool:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tid = bet.get("ticket_id", "?")
        return await self.send_message(
            f"⚠️ <b>Search failed</b> — ticket <code>#{tid}</code>\n"
            f"⏰ {ts}\n<pre>{error[:800]}</pre>"
        )

    async def notify_bet_submit_result(
        self,
        platform: str,
        bet: dict,
        matched: dict,
        submit_result: dict,
        stake: float,
        max_risk: float,
        dry_run: bool,
    ) -> bool:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tid = bet.get("ticket_id", "?")
        status = submit_result.get("status", "unknown")
        ok = bool(submit_result.get("success"))
        total_risk = submit_result.get("total_risk")
        risk_txt = f"{total_risk}" if total_risk is not None else "n/a"
        title = "🧪 DRY-RUN submit" if dry_run else ("✅ Bet submitted" if ok else "⚠️ Submit failed")
        msg = (
            f"<b>{title}</b>\n\n"
            f"⏰ {ts}  |  🏦 <b>{platform}</b>\n"
            f"🎫 Ticket: <code>#{tid}</code>\n"
            f"🎯 Match: {(matched.get('event') or bet.get('event') or '?')[:70]}\n"
            f"🧾 Market: {matched.get('market', bet.get('market', '?'))} | {matched.get('selection', bet.get('selection', '?'))}\n"
            f"💵 Stake: {stake} (max risk {max_risk}) | risk seen: {risk_txt}\n"
            f"📌 Status: <b>{status}</b>"
        )
        if submit_result.get("error"):
            msg += f"\n<pre>{str(submit_result.get('error'))[:500]}</pre>"
        return await self.send_message(msg)

    async def test_connection(self) -> bool:
        return await self.send_message(
            "✅ <b>Bet Finder Agent - Connected</b>\n\n"
            "Bot is online and ready.\nSend /status to check all connections."
        )


# ─── Telegram Command Server ──────────────────────────────────────────────────

class TelegramCommandServer:
    """
    Runs a background Telegram polling loop that handles /status and /help.
    Share an AgentState instance to serve live data.
    """

    def __init__(self, bot_token: str, allowed_chat_id: str, state: AgentState):
        self.bot_token = bot_token
        self.allowed_chat_id = str(allowed_chat_id)
        self.state = state
        self._app: Optional[Application] = None

    def _is_allowed(self, update: Update) -> bool:
        return str(update.effective_chat.id) == self.allowed_chat_id

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update):
            return
        await update.message.reply_text("🔍 Checking status, please wait...")

        lines = [
            f"<b>Bet Finder Agent — Status</b>",
            f"",
            f"⏱ <b>Uptime:</b> {self.state.uptime_str}",
            f"🔄 <b>Cycles run:</b> {self.state.total_cycles}",
            f"📥 <b>Bets scraped:</b> {self.state.total_bets_scraped}",
            f"🎯 <b>Matches found:</b> {self.state.total_matches_found}",
            f"🧾 <b>Submit attempts:</b> {self.state.total_submit_attempts}",
            f"✅ <b>Submit success:</b> {self.state.total_submit_success}",
            f"🕐 <b>Last cycle:</b> {self.state.last_cycle_str}",
            f"",
            f"<b>Connection Health:</b>",
        ]

        # Telegram — already connected since we received the command
        lines.append(f"  ✅ Telegram — connected")

        # ibetcoin.win — quick HTTP ping (it's a standard ASP.NET page, no Playwright)
        ok, code = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _ping(self.state.ibetcoin_url)
        )
        lines.append(f"  {_ping_label(ok, code)} ibetcoin.win")

        # Platform sessions — check REAL login status from the pool
        pool = self.state.pool
        PLATFORM_NAMES = {
            "smash66":     "Smash66",
            "diamondsb":   "DiamondSB",
            "sports411":   "Sports411 (proxy)",
            "leftcoast797":"Leftcoast797",
        }

        for key, display_name in PLATFORM_NAMES.items():
            if key not in self.state.platforms_enabled:
                continue

            if pool is None:
                # Pool not yet initialized
                lines.append(f"  ⏳ {display_name} — agent still starting up")
                continue

            scraper = pool._scrapers.get(key)
            if scraper is None:
                # Platform not in pool — login failed at startup
                lines.append(f"  ❌ {display_name} — not connected (login failed at startup)")
                continue

            # During search_bets the page is often navigating — skip fragile checks
            lock = getattr(pool, "_locks", {}).get(key)
            try:
                busy = lock.locked() if lock is not None else False
            except Exception:
                busy = False
            if busy:
                lines.append(f"  ✅ {display_name} — logged in (search in progress)")
                continue

            # /status must not use page.evaluate — on Render it often false-negatives
            # (CSP, navigation, cross-origin). Trust login flag + tab still attached.
            tab_ok = scraper.browser_tab_open()
            if scraper.is_logged_in and tab_ok:
                lines.append(f"  ✅ {display_name} — logged in & ready")
            elif scraper.is_logged_in and not tab_ok:
                lines.append(
                    f"  ⚠️ {display_name} — login OK but browser tab missing (restart agent)"
                )
            else:
                lines.append(
                    f"  ⚠️ {display_name} — not logged in (next search will re-login)"
                )

        if self.state.last_error:
            lines += ["", f"⚠️ <b>Last error:</b> {self.state.last_error[:150]}"]

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update):
            return
        await update.message.reply_text(
            "<b>Bet Finder Agent — Commands</b>\n\n"
            "/status — Live health check of all connections\n"
            "/help   — Show this help message\n\n"
            "The agent polls ibetcoin.win on a timer for new open bets, "
            "then searches Smash66, DiamondSB, Sports411, and Leftcoast797 "
            "for matching lines. You get a message when each search finishes "
            "and separate alerts when a match is found.",
            parse_mode=ParseMode.HTML
        )

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        err = context.error
        if isinstance(err, Conflict):
            logger.error(
                "[Telegram] getUpdates conflict — only one process may poll this bot. "
                "Stop any second agent (local run, duplicate Render service, or wait for deploy to finish)."
            )
            return
        logger.error("Handler error: %s", err, exc_info=True)

    async def start(self):
        """
        Start the Telegram command server in polling mode.
        
        NOTE on webhooks: Telegram notifications (bet found alerts) are already
        INSTANT — they're direct API calls, not polling-dependent.
        Polling here is only for /status and /help commands, which respond in <1s.
        Webhook mode would require a separate port or integration that conflicts
        with the health server on Render, so polling is used for reliability.
        """
        self._app = Application.builder().token(self.bot_token).build()
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_error_handler(self._on_error)

        await self._app.initialize()
        await self._app.start()
        # Drop webhook if it was set — mixing webhook + polling causes API errors.
        await self._app.bot.delete_webhook(drop_pending_updates=True)
        await self._app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        logger.info("Telegram command server started (/status /help)")

    async def stop(self):
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
