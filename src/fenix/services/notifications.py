"""Conector de notificaciones operacionales con resiliencia y backoff."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NotificationConfig:
    """Configuración del canal de alertas cargada desde variables de entorno."""

    channel: str = "console"  # console | telegram | slack
    telegram_token: str = ""
    telegram_chat_id: str = ""
    slack_webhook_url: str = ""
    timeout_seconds: float = 5.0
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> NotificationConfig:
        return cls(
            channel=os.getenv("FENIX_NOTIFY_CHANNEL", "console").strip().lower(),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", "").strip(),
            timeout_seconds=float(os.getenv("FENIX_NOTIFY_TIMEOUT", "5.0")),
            max_retries=int(os.getenv("FENIX_NOTIFY_RETRIES", "3")),
        )


class NotificationError(RuntimeError):
    """Error controlado al fallar el despacho tras agotar reintentos."""


def _post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    max_retries: int,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Envía un POST JSON manejando timeouts y HTTP 429 (Retry-After)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {"ok": True}

        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < max_retries:
                retry_after = exc.headers.get("Retry-After")
                wait_seconds = float(retry_after) if retry_after and retry_after.isdigit() else float(2**attempt)
                if logger:
                    logger.warning(
                        "Rate limit 429 recibido; esperando Retry-After",
                        extra={"attempt": attempt, "action": "notify_backoff", "status": 429},
                    )
                time.sleep(wait_seconds)
                continue
            raise NotificationError(f"HTTP {exc.code} al enviar notificación: {exc.reason}") from exc

        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < max_retries:
                time.sleep(float(2 ** (attempt - 1)))
                continue
            raise NotificationError(f"Fallo de red o timeout tras {max_retries} intentos: {exc}") from exc

    raise NotificationError("Se agotaron los reintentos de notificación.")


def send_notification(
    message: str,
    *,
    level: str = "INFO",
    context: dict[str, Any] | None = None,
    config: NotificationConfig | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """Despacha una alerta operacional al canal configurado.

    Args:
        message: Texto principal de la alerta.
        level: Severidad (INFO, WARNING, ERROR, CRITICAL).
        context: Metadatos públicos opcionales (ej. event_id, resource_id).
        config: Configuración explícita; si es None se lee del entorno.
        logger: Logger estructurado opcional para trazar el envío.

    Returns:
        True si la notificación fue despachada exitosamente.
    """
    cfg = config or NotificationConfig.from_env()
    ctx_str = f" | {json.dumps(context, ensure_ascii=False)}" if context else ""
    formatted_text = f"[{level.upper()}] Proyecto Fénix: {message}{ctx_str}"

    if cfg.channel == "console":
        print(formatted_text)
        if logger:
            logger.info("Notificación emitida en consola", extra={"action": "notify_console", "status": "ok"})
        return True

    if cfg.channel == "telegram":
        if not cfg.telegram_token or not cfg.telegram_chat_id:
            raise NotificationError("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
        url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
        payload = {"chat_id": cfg.telegram_chat_id, "text": formatted_text}
        _post_json(url, payload, cfg.timeout_seconds, cfg.max_retries, logger)
        if logger:
            logger.info("Notificación enviada a Telegram", extra={"action": "notify_telegram", "status": "ok"})
        return True

    if cfg.channel == "slack":
        if not cfg.slack_webhook_url:
            raise NotificationError("Falta SLACK_WEBHOOK_URL.")
        payload = {"text": formatted_text}
        _post_json(cfg.slack_webhook_url, payload, cfg.timeout_seconds, cfg.max_retries, logger)
        if logger:
            logger.info("Notificación enviada a Slack", extra={"action": "notify_slack", "status": "ok"})
        return True

    raise NotificationError(f"Canal de notificación no soportado: {cfg.channel}")
