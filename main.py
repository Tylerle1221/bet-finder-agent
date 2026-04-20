"""
Bet Finder Agent - Main orchestrator.
Reads open bets from ibetcoin.win, searches platforms, sends Telegram alerts.
Supports /status and /help commands via Telegram.

Speed architecture:
  - One persistent browser session per platform (login once at startup)
  - All platforms search concurrently; Telegram notifies as each book finishes (fastest first)
  - Sessions auto-refresh on expiry
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from rich.console import Console
from rich.panel import Panel
from rich.logging import RichHandler

from telegram_notifier import TelegramNotifier, TelegramCommandServer, AgentState
from bet_matcher import BetMatcher, is_hedge
from ibetcoin_reader import IbetcoinReader
from platform_pool import PlatformPool
from platforms import PLATFORM_MAP

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)
logger = logging.getLogger("bet_agent")
for noisy in ("httpx", "telegram", "playwright", "apscheduler"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

console = Console()
CONFIG_PATH = Path(__file__).parent / "config.json"


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


# ─── Health server ────────────────────────────────────────────────────────────

def _start_health_server():
    """Start a minimal HTTP server for Render health checks — always runs."""
    port = int(os.environ.get("PORT", 10000))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args):
            pass

    Thread(target=HTTPServer(("0.0.0.0", port), Handler).serve_forever, daemon=True).start()
    logger.info(f"Health server on port {port}")


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg: dict = {"telegram": {}, "platforms": {}, "agent": {}, "ibetcoin": {}}

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)

    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg.setdefault("telegram", {})["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg.setdefault("telegram", {})["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.environ.get("IBETCOIN_USERNAME"):
        cfg.setdefault("ibetcoin", {})["username"] = os.environ["IBETCOIN_USERNAME"]
    if os.environ.get("IBETCOIN_PASSWORD"):
        cfg.setdefault("ibetcoin", {})["password"] = os.environ["IBETCOIN_PASSWORD"]

    PLATFORM_ENVS = {
        "smash66":     ("SMASH66_USERNAME",     "SMASH66_PASSWORD",     "https://smash66.com/v2/#/sports"),
        "diamondsb":   ("DIAMONDSB_USERNAME",   "DIAMONDSB_PASSWORD",   "https://diamondsb.com/pla/#/msg"),
        "sports411":   ("SPORTS411_USERNAME",   "SPORTS411_PASSWORD",   "https://be.sports411.ag/en/sports/"),
        "leftcoast797":("LEFTCOAST797_USERNAME","LEFTCOAST797_PASSWORD","https://leftcoast797.com/v2/#/sports"),
    }
    for name, (u_var, p_var, default_url) in PLATFORM_ENVS.items():
        u, p = os.environ.get(u_var), os.environ.get(p_var)
        if u or p:
            ex = cfg.get("platforms", {}).get(name, {})
            cfg.setdefault("platforms", {})[name] = {
                "enabled": True,
                "url": ex.get("url", default_url),
                "username": u or ex.get("username", ""),
                "password": p or ex.get("password", ""),
            }

    a = cfg.setdefault("agent", {})
    if os.environ.get("CHECK_INTERVAL"):
        a["check_interval_seconds"] = int(os.environ["CHECK_INTERVAL"])
    a.setdefault("check_interval_seconds", 60)   # default 60s — fast enough for live bets
    a.setdefault("headless", True)
    a.setdefault("odds_tolerance", 0.05)
    a.setdefault("similarity_threshold", 75)
    a.setdefault("line_slippage", 1.0)
    a.setdefault("juice_slippage", 20)
    a.setdefault("notify_on_exact", True)
    a.setdefault("notify_on_similar", True)
    a.setdefault("report_only_mode", True)
    a.setdefault("auto_submit_enabled", False)
    a.setdefault("auto_submit_exact_only", False)
    a.setdefault("auto_submit_stake", 25.0)
    a.setdefault("auto_submit_max_risk", 50.0)
    a.setdefault("auto_submit_dry_run", False)
    a.setdefault("submit_confirm_password", "")
    a.setdefault("submit_confirm_passwords", {})

    a["auto_submit_enabled"] = _env_bool("AUTO_SUBMIT_ENABLED", a["auto_submit_enabled"])
    a["report_only_mode"] = _env_bool("REPORT_ONLY_MODE", a["report_only_mode"])
    a["auto_submit_exact_only"] = _env_bool("AUTO_SUBMIT_EXACT_ONLY", a["auto_submit_exact_only"])
    a["auto_submit_dry_run"] = _env_bool("AUTO_SUBMIT_DRY_RUN", a["auto_submit_dry_run"])
    if os.environ.get("AUTO_SUBMIT_STAKE"):
        try:
            a["auto_submit_stake"] = float(os.environ["AUTO_SUBMIT_STAKE"])
        except Exception:
            pass
    if os.environ.get("AUTO_SUBMIT_MAX_RISK"):
        try:
            a["auto_submit_max_risk"] = float(os.environ["AUTO_SUBMIT_MAX_RISK"])
        except Exception:
            pass
    if os.environ.get("SUBMIT_CONFIRM_PASSWORD"):
        a["submit_confirm_password"] = os.environ["SUBMIT_CONFIRM_PASSWORD"]

    per_book_env = {
        "smash66": "SUBMIT_CONFIRM_PASSWORD_SMASH66",
        "diamondsb": "SUBMIT_CONFIRM_PASSWORD_DIAMONDSB",
        "sports411": "SUBMIT_CONFIRM_PASSWORD_SPORTS411",
        "leftcoast797": "SUBMIT_CONFIRM_PASSWORD_LEFTCOAST797",
    }
    pw_map = dict(a.get("submit_confirm_passwords") or {})
    for k, env_name in per_book_env.items():
        if os.environ.get(env_name):
            pw_map[k] = os.environ[env_name]
    a["submit_confirm_passwords"] = pw_map
    if a["report_only_mode"]:
        a["auto_submit_enabled"] = False
    return cfg


def get_enabled_platforms(config: dict) -> list[str]:
    return [
        n for n, c in config.get("platforms", {}).items()
        if c.get("enabled", False) and n in PLATFORM_MAP
    ]


PLATFORM_LABELS = {
    "smash66": "Smash66",
    "diamondsb": "DiamondSB",
    "sports411": "Sports411 (proxy)",
    "leftcoast797": "Leftcoast797",
}


def _confirm_password_for(config: dict, platform_name: str) -> str:
    agent_cfg = config.get("agent", {})
    per_book = agent_cfg.get("submit_confirm_passwords") or {}
    if per_book.get(platform_name):
        return str(per_book.get(platform_name))
    if agent_cfg.get("submit_confirm_password"):
        return str(agent_cfg.get("submit_confirm_password"))
    platform_cfg = config.get("platforms", {}).get(platform_name, {})
    if platform_cfg.get("confirm_password"):
        return str(platform_cfg.get("confirm_password"))
    return ""


# ─── Core search — concurrent per-platform tasks, Telegram as each finishes ─

async def process_one_platform(
    platform_name: str,
    bet: dict,
    pool: PlatformPool,
    notifier: TelegramNotifier,
    matcher: BetMatcher,
    all_open_bets: list[dict],
    agent_cfg: dict,
    config: dict,
) -> tuple[int, int, int, int]:
    """
    Search one book, send match Telegrams immediately, then send “book done”.
    Returns (matcher_hits, alerts_sent, submit_attempts, submit_success) for this platform.
    """
    notify_exact = agent_cfg.get("notify_on_exact", True)
    notify_similar = agent_cfg.get("notify_on_similar", True)
    report_only_mode = bool(agent_cfg.get("report_only_mode", True))
    auto_submit_enabled = bool(agent_cfg.get("auto_submit_enabled", False)) and not report_only_mode
    auto_submit_exact_only = bool(agent_cfg.get("auto_submit_exact_only", False))
    submit_stake = float(agent_cfg.get("auto_submit_stake", 25.0) or 25.0)
    submit_max_risk = float(agent_cfg.get("auto_submit_max_risk", 50.0) or 50.0)
    submit_dry_run = bool(agent_cfg.get("auto_submit_dry_run", False))
    label = PLATFORM_LABELS.get(platform_name, platform_name)
    t0 = time.time()
    matcher_hits = 0
    alerts_sent = 0
    submit_attempts = 0
    submit_success = 0
    num_candidates = 0
    error_msg = None
    try:
        candidates = await pool._search_one(platform_name, bet)
        candidates = candidates or []
        num_candidates = len(candidates)
        matches = matcher.filter_results(bet, candidates) if candidates else []
        console.print(
            f"  [{platform_name}] {num_candidates} candidates -> "
            f"[yellow]{len(matches)} matches[/yellow] "
            f"([dim]{time.time() - t0:.1f}s[/dim])"
        )
        submit_candidate = None
        for match in matches:
            matcher_hits += 1
            score = match["similarity_score"]
            # Hedge check must use the original incoming bet fields (side/line/event),
            # not only the matched book row.
            if is_hedge(all_open_bets, bet, same_account=True):
                console.print("    [dim]-> hedge detected, skipping[/dim]")
                continue
            tag = "EXACT" if match["is_exact"] else f"SIMILAR ({score:.0f}%)"
            console.print(
                f"    [bold green]{tag}[/bold green] on [bold]{platform_name}[/bold]: "
                f"{match.get('event', '?')[:50]} | odds={match.get('odds')}"
            )
            if match["is_exact"] and notify_exact:
                await notifier.notify_exact_match(platform_name, bet, match)
                alerts_sent += 1
            elif match["is_similar"] and not match["is_exact"] and notify_similar:
                await notifier.notify_similar_match(platform_name, bet, match, score)
                alerts_sent += 1

            if submit_candidate is None:
                if auto_submit_exact_only and not match["is_exact"]:
                    pass
                else:
                    submit_candidate = match

        if auto_submit_enabled and submit_candidate is not None:
            submit_attempts += 1
            confirm_password = _confirm_password_for(config, platform_name)
            submit_result = await pool.submit_one(
                name=platform_name,
                bet=bet,
                matched=submit_candidate,
                stake=submit_stake,
                max_risk=submit_max_risk,
                confirm_password=confirm_password,
                dry_run=submit_dry_run,
            )
            if submit_result.get("success"):
                submit_success += 1
            await notifier.notify_bet_submit_result(
                platform=label,
                bet=bet,
                matched=submit_candidate,
                submit_result=submit_result,
                stake=submit_stake,
                max_risk=submit_max_risk,
                dry_run=submit_dry_run,
            )
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        logger.exception(
            "Platform %s failed for ticket %s", platform_name, bet.get("ticket_id")
        )
    finally:
        elapsed = time.time() - t0
        await notifier.notify_platform_search_done(
            label,
            bet,
            num_candidates,
            matcher_hits,
            alerts_sent,
            elapsed,
            error=error_msg,
        )
    return matcher_hits, alerts_sent, submit_attempts, submit_success


async def process_bet(
    bet: dict,
    pool: PlatformPool,
    notifier: TelegramNotifier,
    matcher: BetMatcher,
    all_open_bets: list[dict],
    state: AgentState,
    cfg: dict,
) -> tuple[int, int, int, int]:
    """Runs all platform searches concurrently; Telegram fires as each completes."""
    agent_cfg = cfg.get("agent", {})
    side_str = ""
    if bet.get("bet_side") and bet.get("line"):
        side_str = f" {bet['bet_side'].upper()} {bet['line']}"
    odds_str = str(bet.get("odds_american") or bet.get("odds") or "")
    console.print(
        f"  [cyan]Searching:[/cyan] [{bet.get('sport', '')}] [bold]{bet.get('event', '?')[:55]}[/bold]"
        f"{side_str}  odds={odds_str}  ticket={bet.get('ticket_id')}"
    )

    platforms = pool.platform_names()
    tasks = [
        process_one_platform(
            p, bet, pool, notifier, matcher, all_open_bets, agent_cfg, cfg
        )
        for p in platforms
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    matcher_hits_total = 0
    alerts_total = 0
    submit_attempts_total = 0
    submit_success_total = 0
    for r in results:
        if isinstance(r, Exception):
            logger.error("Platform task error: %s", r)
            continue
        mh, al, sa, ss = r
        matcher_hits_total += mh
        alerts_total += al
        submit_attempts_total += sa
        submit_success_total += ss

    state.total_matches_found += alerts_total
    state.total_submit_attempts += submit_attempts_total
    state.total_submit_success += submit_success_total
    return matcher_hits_total, alerts_total, submit_attempts_total, submit_success_total


# ─── Main loop ────────────────────────────────────────────────────────────────

async def polling_loop(
    config: dict,
    pool: PlatformPool,
    notifier: TelegramNotifier,
    state: AgentState,
    reader: IbetcoinReader,
):
    agent_cfg = config.get("agent", {})
    ibet = config.get("ibetcoin", {})

    matcher = BetMatcher(
        similarity_threshold=agent_cfg.get("similarity_threshold", 75),
        odds_tolerance=agent_cfg.get("odds_tolerance", 0.05),
        line_slippage=agent_cfg.get("line_slippage", 1.0),
        juice_slippage=agent_cfg.get("juice_slippage", 20),
    )

    interval = agent_cfg.get("check_interval_seconds", 300)
    active = pool.platform_names()

    console.print(
        f"\n[bold green]Agent running[/bold green]\n"
        f"  Platforms ready: {', '.join(active)}\n"
        f"  ibetcoin poll interval: {interval}s\n"
        f"  Slippage: line±{agent_cfg['line_slippage']}pt, juice±{agent_cfg['juice_slippage']}\n"
        f"  Report-only mode: {agent_cfg.get('report_only_mode', True)}\n"
        f"  Auto-submit: {agent_cfg.get('auto_submit_enabled', False)} "
        f"(stake={agent_cfg.get('auto_submit_stake', 25)}, "
        f"max_risk={agent_cfg.get('auto_submit_max_risk', 50)}, "
        f"exact_only={agent_cfg.get('auto_submit_exact_only', False)}, "
        f"dry_run={agent_cfg.get('auto_submit_dry_run', False)})\n"
        f"  Search mode: [bold cyan]concurrent[/bold cyan] — Telegram as each book finishes\n"
    )
    state.platforms_enabled = active
    await notifier.notify_agent_started(active)

    while True:
        state.total_cycles += 1
        from datetime import datetime, timezone
        state.last_cycle_at = datetime.now(timezone.utc)
        console.rule(f"[dim]Cycle #{state.total_cycles} — {time.strftime('%H:%M:%S')}[/dim]")

        console.print("[dim]Fetching open bets from ibetcoin.win...[/dim]")
        all_bets = await reader.fetch_open_bets()
        new_bets = await reader.fetch_new_bets()
        state.total_bets_scraped += len(new_bets)

        console.print(f"[dim]Total open: {len(all_bets)} | New this cycle: {len(new_bets)}[/dim]")

        if not new_bets:
            console.print("[dim]No new bets to process[/dim]")
        else:
            await notifier.notify_new_bets_found(new_bets)

            for bet in new_bets:
                try:
                    matcher_hits, _alerts_sent, submit_attempts, submit_success = await process_bet(
                        bet, pool, notifier, matcher, all_bets, state, config
                    )
                except Exception as e:
                    err_msg = f"{type(e).__name__}: {e}"
                    logger.exception("process_bet failed for ticket %s", bet.get("ticket_id"))
                    state.last_error = err_msg[:500]
                    await notifier.notify_bet_search_error(bet, err_msg)
                    continue

                if matcher_hits == 0:
                    console.print("  [dim]No matcher hits on any platform[/dim]")
                if submit_attempts > 0:
                    console.print(
                        f"  [magenta]Submit attempts:[/magenta] {submit_attempts} | "
                        f"[green]success:[/green] {submit_success}"
                    )
                # Per-platform completion messages already sent to Telegram

        console.print(f"[dim]Next check in {interval}s...[/dim]")
        await asyncio.sleep(interval)


# ─── Entry point ─────────────────────────────────────────────────────────────

async def main():
    _start_health_server()
    console.print(Panel.fit(
        "[bold cyan]Bet Finder Agent[/bold cyan]\n"
        "[dim]ibetcoin.win -> per-book search + incremental Telegram[/dim]",
        border_style="cyan"
    ))

    config = load_config()
    tg = config.get("telegram", {})

    if not tg.get("bot_token"):
        console.print("[red]TELEGRAM_BOT_TOKEN not set.[/red]"); sys.exit(1)
    if not tg.get("chat_id"):
        console.print("[red]TELEGRAM_CHAT_ID not set. Run setup_telegram.py first.[/red]"); sys.exit(1)
    if not config.get("ibetcoin", {}).get("username"):
        console.print("[red]IBETCOIN_USERNAME not set.[/red]"); sys.exit(1)

    enabled_platforms = get_enabled_platforms(config)
    if not enabled_platforms:
        console.print("[red]No platforms enabled.[/red]"); sys.exit(1)

    state = AgentState()
    notifier = TelegramNotifier(tg["bot_token"], tg["chat_id"])
    cmd_server = TelegramCommandServer(tg["bot_token"], tg["chat_id"], state)

    # Test Telegram
    console.print("[dim]Testing Telegram...[/dim]")
    await notifier.test_connection()

    # Start command listener
    await cmd_server.start()

    # Initialize platform pool — login ONCE, keep browsers alive
    pool = PlatformPool(config)
    console.print(f"[dim]Starting browsers and logging in to {len(enabled_platforms)} platforms...[/dim]")
    ok = await pool.initialize(enabled_platforms)
    if ok == 0:
        console.print("[red]All platform logins failed.[/red]"); sys.exit(1)

    # Give the status command access to real session data
    state.pool = pool

    # ── IMPORTANT: mark ALL currently open bets as already seen ──────────────
    # This ensures we only process BRAND NEW bets from this point forward,
    # not the bets that were already sitting on ibetcoin.win when we started.
    ibet = config.get("ibetcoin", {})
    agent_cfg = config.get("agent", {})
    reader = IbetcoinReader(
        username=ibet["username"],
        password=ibet["password"],
        headless=agent_cfg.get("headless", True),
    )
    console.print("[dim]Checking current open bets (marking as already seen)...[/dim]")
    existing = await reader.fetch_new_bets()  # adds all current IDs to _known_tickets
    if existing:
        console.print(
            f"[dim]Skipped {len(existing)} pre-existing open bet(s) — "
            f"only NEW bets will trigger alerts.[/dim]"
        )
    else:
        console.print("[dim]No pre-existing open bets. Ready for new bets.[/dim]")

    try:
        await polling_loop(config, pool, notifier, state, reader)
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[yellow]Stopping...[/yellow]")
        await notifier.notify_agent_stopped("Manual stop")
    finally:
        await cmd_server.stop()
        await pool.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
