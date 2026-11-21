"""Conector resiliente de notificaciones operacionales para Proyecto Fénix."""

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
    """Configuración del conector leída desde variables de entorno."""

    channel: str = "console"  # console | telegram | slack
    telegram_token: str = ""
    telegram_chat_id: str = ""
    slack_webhook_url: str = ""
    connect_timeout: float = 3.0
    read_timeout: float = 5.0
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> NotificationConfig:
        return cls(
            channel=os.getenv("FENIX_NOTIFY_CHANNEL", "console").strip().lower(),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", "").strip(),
            connect_timeout=float(os.getenv("FENIX_NOTIFY_CONNECT_TIMEOUT", "3.0")),
            read_timeout=float(os.getenv("FENIX_NOTIFY_READ_TIMEOUT", "5.0")),
            max_retries=int(os.getenv("FENIX_NOTIFY_RETRIES", "3")),
        )


class NotificationDeliveryError(RuntimeError):
    """Se lanza cuando falla el envío de una notificación tras agotar reintentos."""


def _compute_retry_after_seconds(headers: Any, attempt: int) -> float:
    """Calcula la espera respetando Retry-After cuando exista."""
    if headers is not None:
        retry_after_raw = headers.get("Retry-After")
        if retry_after_raw is not None:
            try:
                parsed = float(str(retry_after_raw).strip())
                if parsed >= 0:
                    return parsed
            except ValueError:
                pass
    return float(2 ** (attempt - 1))


def _post_json_with_resilience(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    max_retries: int,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Envía un POST JSON con timeout y manejo de HTTP 429 (Retry-After)."""
    encoded_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=encoded_body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ProyectoFenix-Notifier/1.0",
        },
        method="POST",
    )

    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw_body = response.read().decode("utf-8")
                return json.loads(raw_body) if raw_body else {"ok": True}

        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < max_retries:
                wait_s = _compute_retry_after_seconds(exc.headers, attempt)
                if logger:
                    logger.warning(
                        "HTTP 429 recibido; aplicando backoff según Retry-After",
                        extra={
                            "action": "notify_rate_limited",
                            "status": 429,
                            "attempt": attempt,
                            "retry_after_s": wait_s,
                        },
                    )
                time.sleep(wait_s)
                continue

            if exc.code >= 500 and attempt < max_retries:
                wait_s = float(2 ** (attempt - 1))
                time.sleep(wait_s)
                continue

            raise NotificationDeliveryError(
                f"Error HTTP {exc.code} al despachar notificación: {exc.reason}"
            ) from exc

        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < max_retries:
                time.sleep(float(2 ** (attempt - 1)))
                continue
            raise NotificationDeliveryError(
                f"Timeout o fallo de conexión tras {max_retries} intentos: {exc}"
            ) from exc

    raise NotificationDeliveryError("Se agotaron los reintentos de notificación.")


class NotificationService:
    """Servicio de despacho de notificaciones operacionales."""

    def __init__(
        self,
        config: NotificationConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config or NotificationConfig.from_env()
        self.logger = logger

    def send(
        self,
        message: str,
        *,
        level: str = "INFO",
        context: dict[str, Any] | None = None,
    ) -> bool:
        severity = level.upper()
        ctx_suffix = f" | context={json.dumps(context, ensure_ascii=False)}" if context else ""
        text = f"[{severity}] Fénix: {message}{ctx_suffix}"
        effective_timeout = min(self.config.connect_timeout, self.config.read_timeout)

        if self.config.channel == "console":
            print(text)
            if self.logger:
                self.logger.info(
                    "Notificación emitida en consola",
                    extra={"action": "notify_console", "status": "ok"},
                )
            return True

        if self.config.channel == "telegram":
            if not self.config.telegram_token or not self.config.telegram_chat_id:
                raise NotificationDeliveryError(
                    "Configuración incompleta para Telegram: faltan token o chat_id."
                )
            endpoint = f"https://api.telegram.org/bot{self.config.telegram_token}/sendMessage"
            _post_json_with_resilience(
                endpoint,
                {"chat_id": self.config.telegram_chat_id, "text": text},
                timeout=effective_timeout,
                max_retries=self.config.max_retries,
                logger=self.logger,
            )
            if self.logger:
                self.logger.info(
                    "Notificación enviada a Telegram",
                    extra={"action": "notify_telegram", "status": "ok"},
                )
            return True

        if self.config.channel == "slack":
            if not self.config.slack_webhook_url:
                raise NotificationDeliveryError(
                    "Configuración incompleta para Slack: falta slack_webhook_url."
                )
            _post_json_with_resilience(
                self.config.slack_webhook_url,
                {"text": text},
                timeout=effective_timeout,
                max_retries=self.config.max_retries,
                logger=self.logger,
            )
            if self.logger:
                self.logger.info(
                    "Notificación enviada a Slack",
                    extra={"action": "notify_slack", "status": "ok"},
                )
            return True

        raise NotificationDeliveryError(f"Canal no soportado: {self.config.channel}")


def send_notification(
    message: str,
    *,
    level: str = "INFO",
    context: dict[str, Any] | None = None,
    config: NotificationConfig | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """Función de conveniencia para despachar una notificación."""
    service = NotificationService(config=config, logger=logger)
    return service.send(message, level=level, context=context)
