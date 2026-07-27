"""Minimal Telegram Bot API transport with no Hermes runtime dependency."""

from __future__ import annotations

import http.client
import json
import urllib.parse


class DeliveryError(RuntimeError):
    """Telegram did not confirm delivery."""


def render_message(event: dict) -> str:
    icons = {
        "FAILED": "🚨",
        "REMINDER": "⏰",
        "RECOVERED": "✅",
        "HEARTBEAT": "💚",
        "TEST": "🧪",
    }
    return (
        f"{icons[event['kind']]} [{event['label']}] "
        f"{event['component']} {event['kind']}\n"
        f"{event['reason']}\n"
        f"UTC {event['created_at']} · {event['event_id'][:8]}"
    )[:4096]


def send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    timeout_seconds: int = 15,
    silent: bool = False,
) -> str:
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
        "disable_notification": "true" if silent else "false",
    })
    connection = http.client.HTTPSConnection(
        "api.telegram.org", timeout=timeout_seconds
    )
    try:
        # The credential stays inside this process.  Never put this path in an
        # exception or log: Telegram embeds the token in the URL.
        connection.request(
            "POST",
            f"/bot{token}/sendMessage",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        payload = response.read(64 * 1024)
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise DeliveryError(f"telegram_transport_{type(error).__name__}") from error
    finally:
        connection.close()
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeliveryError(
            f"telegram_invalid_response http={response.status}"
        ) from error
    if (
        response.status != 200
        or not isinstance(decoded, dict)
        or decoded.get("ok") is not True
    ):
        code = decoded.get("error_code") if isinstance(decoded, dict) else None
        raise DeliveryError(f"telegram_rejected http={response.status} code={code}")
    result = decoded.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
        raise DeliveryError("telegram_missing_message_id")
    return str(result["message_id"])
