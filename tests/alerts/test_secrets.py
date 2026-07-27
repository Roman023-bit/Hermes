from __future__ import annotations

import pytest

from hermes_alerts.secrets import SecretError, read_token


def test_dedicated_token_wins_over_primary(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=primary\nTELEGRAM_ALERT_BOT_TOKEN='dedicated'\n")
    assert read_token(env, allow_primary_fallback=True) == "dedicated"


def test_primary_requires_explicit_fallback(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=primary\n")
    with pytest.raises(SecretError):
        read_token(env, allow_primary_fallback=False)
    assert read_token(env, allow_primary_fallback=True) == "primary"


def test_dotenv_is_parsed_never_executed(tmp_path):
    env = tmp_path / ".env"
    marker = tmp_path / "owned"
    env.write_text(
        f"EVIL=$(touch {marker})\nTELEGRAM_ALERT_BOT_TOKEN=123:abc\n",
        encoding="utf-8",
    )
    assert read_token(env, allow_primary_fallback=False) == "123:abc"
    assert not marker.exists()


def test_duplicate_token_is_rejected(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_ALERT_BOT_TOKEN=a\nTELEGRAM_ALERT_BOT_TOKEN=b\n")
    with pytest.raises(SecretError, match="duplicate"):
        read_token(env, allow_primary_fallback=False)
