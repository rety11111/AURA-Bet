"""Тесты Circuit Breaker для защиты от сбоев внешних API (app/sources/base.py)."""

from __future__ import annotations

import pytest

from app.sources.base import CircuitBreaker, CircuitBreakerOpen


def test_circuit_breaker_normal_cycle() -> None:
    cb = CircuitBreaker("test_api", failure_threshold=3, recovery_time_sec=10.0)
    assert cb.state == "CLOSED"

    # Успешный запрос не меняет состояние
    cb.before_request()
    cb.record_success()
    assert cb.state == "CLOSED"
    assert cb.consecutive_failures == 0


def test_circuit_breaker_opens_after_threshold() -> None:
    cb = CircuitBreaker("test_api", failure_threshold=3, recovery_time_sec=10.0)

    cb.record_failure()
    assert cb.state == "CLOSED"
    assert cb.consecutive_failures == 1

    cb.record_failure()
    assert cb.state == "CLOSED"

    # Третий сбой открывает цепь
    cb.record_failure()
    assert cb.state == "OPEN"
    assert cb.consecutive_failures == 3

    # Следующий запрос сразу выбрасывает CircuitBreakerOpen без реального вызова
    with pytest.raises(CircuitBreakerOpen) as exc_info:
        cb.before_request()
    assert "circuit breaker OPEN" in str(exc_info.value)


def test_circuit_breaker_recovery() -> None:
    cb = CircuitBreaker("test_api", failure_threshold=2, recovery_time_sec=0.1)

    cb.record_failure()
    cb.record_failure()
    assert cb.state == "OPEN"

    # Имитируем прошествие времени восстановления
    cb.opened_at -= 0.2

    # При следующем запросе переходит в HALF_OPEN для проверки
    cb.before_request()
    assert cb.state == "HALF_OPEN"

    # Успех в HALF_OPEN закрывает цепь
    cb.record_success()
    assert cb.state == "CLOSED"
    assert cb.consecutive_failures == 0
