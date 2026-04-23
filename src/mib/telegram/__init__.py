"""Telegram bot — polling mode, outbound-only, whitelisted users."""

from __future__ import annotations

from typing import Any

from telegram.ext import Application

# PTB's ``Application`` is generic over 6 type parameters (ContextT, BotDataT,
# ChatDataT, UserDataT, CallbackDataCacheT, JobQueueT). We don't customise
# any of them, so alias to ``Application[Any, ...]`` once and reuse it across
# bot.py / jobs / middleware — keeps mypy happy without noise at every call site.
type BotApp = Application[Any, Any, Any, Any, Any, Any]
