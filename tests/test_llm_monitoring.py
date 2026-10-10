"""Тесты мониторинга расходов и токенов LLM (app/llm_client.py)."""

from __future__ import annotations

from app.llm_client import OpenRouterClient, estimate_cost_usd, format_llm_stats_text


def test_estimate_cost_usd() -> None:
    # 1,000,000 prompt + 1,000,000 completion на Claude 3.5 Sonnet = $3 + $15 = $18
    cost = estimate_cost_usd("anthropic/claude-3.5-sonnet", 1_000_000, 1_000_000)
    assert abs(cost - 18.0) < 1e-5

    # 10,000 prompt + 2,000 completion на DeepSeek Chat ($0.14/$0.28 per 1M)
    # (10000 / 1e6) * 0.14 + (2000 / 1e6) * 0.28 = 0.0014 + 0.00056 = 0.00196
    cost_ds = estimate_cost_usd("deepseek/deepseek-chat", 10_000, 2_000)
    assert abs(cost_ds - 0.00196) < 1e-6


def test_usage_summary_and_formatting() -> None:
    client = OpenRouterClient(api_key="test-key")
    client.stats["requests"] = 5
    client.stats["failures"] = 1
    client.stats["invalid_json"] = 0
    client.stats["prompt_tokens"] = 5000
    client.stats["completion_tokens"] = 1200
    client.stats["by_model"] = {
        "anthropic/claude-3.5-sonnet": {
            "calls": 2,
            "prompt_tokens": 3000,
            "completion_tokens": 800,
        },
        "deepseek/deepseek-chat": {
            "calls": 3,
            "prompt_tokens": 2000,
            "completion_tokens": 400,
        },
    }

    summary = client.usage_summary()
    assert summary["requests"] == 5
    assert summary["failures"] == 1
    assert summary["total_tokens"] == 6200
    assert summary["estimated_cost_usd"] > 0
    assert "anthropic/claude-3.5-sonnet" in summary["by_model"]

    text = format_llm_stats_text(client)
    assert "Мониторинг расходов" in text
    assert "6,200" in text
    assert "claude-3.5-sonnet" in text
    assert "deepseek-chat" in text
