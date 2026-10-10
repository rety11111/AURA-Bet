"""Тесты меню команд, прав администратора и клавиатуры Telegram-бота."""

from __future__ import annotations

import pytest

from app.bot.handlers import (
    get_admin_inline_keyboard,
    get_main_keyboard,
    is_admin_user,
)
from app.config import settings


def test_is_admin_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_admin_ids", "111,222")
    assert is_admin_user(111) is True
    assert is_admin_user(222) is True
    assert is_admin_user(333) is False
    assert is_admin_user(None) is False


def test_main_keyboard_regular_vs_admin() -> None:
    user_kb = get_main_keyboard(is_admin=False)
    user_buttons = [btn.text for row in user_kb.keyboard for btn in row]
    assert "📊 Статистика" in user_buttons
    assert "🎯 Сигналы" in user_buttons
    assert "ℹ️ Справка" in user_buttons
    assert "⚙️ Админка" not in user_buttons

    admin_kb = get_main_keyboard(is_admin=True)
    admin_buttons = [btn.text for row in admin_kb.keyboard for btn in row]
    assert "⚙️ Админка" in admin_buttons


def test_admin_inline_keyboard() -> None:
    kb = get_admin_inline_keyboard()
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "admin_costs" in callbacks
    assert "admin_system" in callbacks
    assert "admin_collect_now" in callbacks
    assert "admin_analyze_now" in callbacks
