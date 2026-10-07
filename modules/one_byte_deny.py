"""Opt-in denial responses for commands received over one-byte paths."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import threading
from typing import Any

from .utils import format_keyword_response_with_placeholders

DEFAULT_SENDER_COOLDOWN_MINUTES = 60.0
DEFAULT_MESH_COOLDOWN_MINUTES = 5.0
DEFAULT_HASH_TEMPLATE = "Denied: 1-byte path {packet_hash}"

ACTION_NORMAL = "normal"
ACTION_DENY = "deny"
ACTION_SUPPRESS = "suppress"


@dataclass(frozen=True)
class OneByteDenySettings:
    enabled: bool = False
    hash_template: str = DEFAULT_HASH_TEMPLATE
    sender_cooldown_minutes: float = DEFAULT_SENDER_COOLDOWN_MINUTES
    mesh_cooldown_minutes: float = DEFAULT_MESH_COOLDOWN_MINUTES


@dataclass(frozen=True)
class OneByteDenyDecision:
    action: str
    response: str | None = None


def _get(config: Any, key: str, fallback: str = "") -> str:
    try:
        if config is not None and config.has_option("Bot", key):
            return (config.get("Bot", key, raw=True) or "").strip()
    except Exception:
        pass
    return fallback


def _get_bool(config: Any, key: str, fallback: bool) -> bool:
    value = _get(config, key).lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return fallback


def _get_minutes(config: Any, key: str, fallback: float) -> float:
    raw = _get(config, key)
    if not raw:
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return value if math.isfinite(value) and value >= 0 else fallback


def load_settings(config: Any) -> OneByteDenySettings:
    return OneByteDenySettings(
        enabled=_get_bool(config, "deny_1_byte", False),
        hash_template=_get(config, "deny_hash_template", DEFAULT_HASH_TEMPLATE)
        or DEFAULT_HASH_TEMPLATE,
        sender_cooldown_minutes=_get_minutes(
            config, "deny_1_byte_sender_cooldown_minutes", DEFAULT_SENDER_COOLDOWN_MINUTES
        ),
        mesh_cooldown_minutes=_get_minutes(
            config, "deny_1_byte_channel_cooldown_minutes", DEFAULT_MESH_COOLDOWN_MINUTES
        ),
    )


def is_one_byte_path(message: Any) -> bool:
    routing_info = getattr(message, "routing_info", None)
    if not isinstance(routing_info, dict):
        return False
    path_byte_length = routing_info.get("path_byte_length")
    return type(path_byte_length) is int and path_byte_length == 1


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


class OneByteDenyTracker:
    """Reserve one denial response per configured cooldown window."""

    _reservation_lock = threading.Lock()

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.logger = bot.logger
        self.settings = load_settings(getattr(bot, "config", None))
        self._sender_times: dict[str, datetime] = {}
        self._mesh_time: datetime | None = None

    def reload_config(self) -> None:
        self.settings = load_settings(getattr(self.bot, "config", None))

    def decide(self, message: Any, command_name: str) -> OneByteDenyDecision:
        cached = getattr(message, "_one_byte_deny_decision", None)
        if isinstance(cached, OneByteDenyDecision):
            return cached

        decision = self._decide_uncached(message, command_name)
        try:
            setattr(message, "_one_byte_deny_decision", decision)
        except Exception:
            pass
        return decision

    def _decide_uncached(self, message: Any, command_name: str) -> OneByteDenyDecision:
        settings = self.settings
        if (
            not settings.enabled
            or getattr(message, "capture_sink", None) is not None
            or not is_one_byte_path(message)
        ):
            return OneByteDenyDecision(ACTION_NORMAL)

        sender_key = self._sender_key(message)
        now = datetime.now(timezone.utc)
        with self._reservation_lock:
            sender_last = self._latest_sender(sender_key)
            mesh_last = self._latest_mesh()
            sender_active = (
                sender_last is not None
                and (now - sender_last).total_seconds() < settings.sender_cooldown_minutes * 60
            )
            mesh_active = (
                mesh_last is not None
                and (now - mesh_last).total_seconds() < settings.mesh_cooldown_minutes * 60
            )
            if sender_active or mesh_active:
                return OneByteDenyDecision(ACTION_SUPPRESS)

            self._record_attempt(message, sender_key, command_name, now)

        response = format_keyword_response_with_placeholders(
            settings.hash_template, message, self.bot
        ).strip()
        return OneByteDenyDecision(ACTION_DENY, response)

    def _sender_key(self, message: Any) -> str:
        pubkey = str(getattr(message, "sender_pubkey", None) or "").strip().lower()
        sender = str(getattr(message, "sender_id", None) or "").strip().lower()
        return pubkey or sender or "(unknown)"

    def _latest_sender(self, sender_key: str) -> datetime | None:
        cached = self._sender_times.get(sender_key)
        db = getattr(self.bot, "db_manager", None)
        if db is None:
            return cached
        try:
            rows = db.execute_query(
                "SELECT MAX(created_at) AS last_at FROM one_byte_deny_events WHERE sender_key = ?",
                (sender_key,),
            )
            stored = _parse_timestamp(rows[0].get("last_at")) if rows else None
            return max((value for value in (cached, stored) if value is not None), default=None)
        except Exception:
            return cached

    def _latest_mesh(self) -> datetime | None:
        db = getattr(self.bot, "db_manager", None)
        if db is None:
            return self._mesh_time
        try:
            rows = db.execute_query(
                "SELECT MAX(created_at) AS last_at FROM one_byte_deny_events"
            )
            stored = _parse_timestamp(rows[0].get("last_at")) if rows else None
            return max((value for value in (self._mesh_time, stored) if value is not None), default=None)
        except Exception:
            return self._mesh_time

    def _record_attempt(
        self, message: Any, sender_key: str, command_name: str, now: datetime
    ) -> None:
        self._sender_times[sender_key] = now
        self._mesh_time = now
        created_at = now.isoformat(timespec="seconds")
        db = getattr(self.bot, "db_manager", None)
        if db is None:
            return
        try:
            with db.connection() as conn:
                conn.execute(
                    "INSERT INTO one_byte_deny_events "
                    "(created_at, sender_key, sender_id, sender_pubkey, channel, command_name, packet_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        created_at,
                        sender_key,
                        getattr(message, "sender_id", None),
                        getattr(message, "sender_pubkey", None),
                        getattr(message, "channel", None),
                        command_name,
                        (getattr(message, "routing_info", None) or {}).get("packet_hash"),
                    ),
                )
                conn.commit()
        except Exception:
            self.logger.exception("Failed to record one-byte denial attempt")
