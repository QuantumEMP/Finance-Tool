"""Telegram front-end: proposals with Approve/Reject buttons, commands, scheduled jobs."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from agent.config import AppConfig
from agent.cycle import CycleRunner
from agent.executor import Executor
from agent.reports import format_proposal, heartbeat_text, report_text, status_text
from agent.runtime import AccountRuntime
from agent.store import Store

log = logging.getLogger(__name__)
CALLBACK_RE = re.compile(r"^(approve|reject):([0-9a-f]{32})$")
ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)
WEEKDAYS = (1, 2, 3, 4, 5)  # python-telegram-bot v20+: 0 = Sunday


def parse_callback(data: str | None) -> tuple[str, str] | None:
    m = CALLBACK_RE.match(data or "")
    return (m.group(1), m.group(2)) if m else None


def is_owner(chat_id: int | None, owner: int) -> bool:
    return chat_id is not None and chat_id == owner


def is_owner_update(chat_id: int | None, user_id: int | None, owner: int) -> bool:
    return is_owner(chat_id, owner) and user_id is not None and user_id == owner


def keyboard(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{pid}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject:{pid}"),
    ]])


class TradingBot:
    def __init__(self, *, token: str, owner_chat_id: int, config: AppConfig,
                 runtimes: dict[str, AccountRuntime], store: Store, cycle: CycleRunner,
                 executor: Executor, now: Callable[[], datetime]) -> None:
        self._token, self._owner, self._config = token, owner_chat_id, config
        self._runtimes, self._store, self._cycle = runtimes, store, cycle
        self._executor, self._now = executor, now

    def build(self) -> Application:
        app = Application.builder().token(self._token).build()
        owner_only = filters.Chat(chat_id=self._owner)
        for name, handler in [("run", self._cmd_run), ("status", self._cmd_status),
                              ("report", self._cmd_report), ("pause", self._cmd_pause),
                              ("resume", self._cmd_resume)]:
            app.add_handler(CommandHandler(name, handler, filters=owner_only))
        app.add_handler(CallbackQueryHandler(self._on_button))
        app.add_error_handler(self._on_error)

        jq = app.job_queue
        for name, rt in self._runtimes.items():
            s = rt.config.schedule
            jq.run_daily(self._job_cycle, time=s.run_at.replace(tzinfo=ZoneInfo(s.timezone)),
                         days=WEEKDAYS if s.weekdays_only else ALL_DAYS, data=name, name=f"cycle:{name}")
        hb = self._config.heartbeat
        jq.run_daily(self._job_heartbeat, time=hb.run_at.replace(tzinfo=ZoneInfo(hb.timezone)),
                     name="heartbeat")
        jq.run_repeating(self._job_housekeeping, interval=300, first=30, name="housekeeping")
        return app

    async def _send(self, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
        return await context.bot.send_message(chat_id=self._owner, text=text, **kwargs)

    async def _run_account(self, context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
        rt = self._runtimes[name]
        result = await asyncio.to_thread(self._cycle.run, rt)
        for alert in result.alerts:
            await self._send(context, f"⚠️ {alert}")
        if result.skipped_reason:
            await self._send(context, f"{name}: skipped ({result.skipped_reason})")
            return
        for p in result.proposals:
            msg = await self._send(context, format_proposal(p, rt.config.quote_currency, rt.mode),
                                   reply_markup=keyboard(p.id))
            await asyncio.to_thread(self._store.set_message_id, p.id, msg.message_id)
        if not result.proposals:
            text = f"{name}: no trades proposed."
            if result.vetoes:
                text += "\nVetoed by risk limits:\n" + "\n".join(f"• {v}" for v in result.vetoes)
            await self._send(context, text)

    # --- commands ------------------------------------------------------------

    async def _cmd_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        for name in context.args or list(self._runtimes):
            if name not in self._runtimes:
                await update.effective_message.reply_text(
                    f"Unknown account {name!r}. Options: {', '.join(self._runtimes)}")
                continue
            await update.effective_message.reply_text(f"Running {name}…")
            await self._run_account(context, name)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = await asyncio.to_thread(status_text, self._store, self._runtimes)
        await update.effective_message.reply_text(text or "No accounts configured.")

    async def _cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        days = 7
        if context.args:
            try:
                days = max(1, min(int(context.args[0]), 365))
            except ValueError:
                await update.effective_message.reply_text("Usage: /report [days]")
                return
        text = await asyncio.to_thread(report_text, self._store, list(self._runtimes), self._now(), days)
        await update.effective_message.reply_text(text)

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await asyncio.to_thread(self._store.set_flag, "paused", "1")
        await update.effective_message.reply_text("⏸ Paused. No proposals or executions until /resume.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        def resume():
            self._store.set_flag("paused", None)
            for key in self._store.flags_with_prefix("paused:"):
                self._store.set_flag(key, None)
        await asyncio.to_thread(resume)
        await update.effective_message.reply_text("▶️ Resumed.")

    # --- buttons -------------------------------------------------------------

    async def _on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None
        if not is_owner_update(chat_id, user_id, self._owner):
            await query.answer()
            return
        parsed = parse_callback(query.data)
        if parsed is None:
            await query.answer("Unknown action")
            return
        action, pid = parsed
        await query.answer("Working…")
        await query.edit_message_reply_markup(reply_markup=None)
        fn = self._executor.approve if action == "approve" else self._executor.reject
        outcome = await asyncio.to_thread(fn, pid)
        original = query.message.text if query.message else ""
        await query.edit_message_text(f"{original}\n\n{outcome.message}")

    # --- jobs ----------------------------------------------------------------

    async def _job_cycle(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_account(context, context.job.data)

    async def _job_housekeeping(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        for p in await asyncio.to_thread(self._executor.expire_stale):
            if p.message_id:
                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=self._owner, message_id=p.message_id, reply_markup=None)
                except TelegramError:
                    pass
            await self._send(context, f"⌛ Expired, not executed: {p.order.side.value} "
                                      f"{p.order.symbol} ({p.order.account})")
        for outcome in await asyncio.to_thread(self._executor.reconcile):
            await self._send(context, outcome.message)

    async def _job_heartbeat(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = await asyncio.to_thread(heartbeat_text, self._store, self._runtimes, self._now())
        await self._send(context, text)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("unhandled error", exc_info=context.error)
        try:
            await self._send(context, f"⚠️ Internal error ({type(context.error).__name__}). Check the service log.")
        except TelegramError:
            pass
