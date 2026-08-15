#!/usr/bin/env python3
"""
Room Server Bridge Service for MeshCore Bot

Listens for messages on a configured channel and forwards them as direct
messages to a room server, preserving the original sender's name as a prefix.
Supports enable/disable via a bot command, and independent periodic
announcements to both the channel and the room on separate schedules.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger  # noqa: F401 (used by APScheduler internals)
from meshcore import EventType

from modules.service_plugins.base_service import BaseServicePlugin
from modules.scheduled_message_cron import parse_schedule_key
from modules.utils import decode_escape_sequences, get_config_timezone


# ──────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AnnouncementConfig:
    """One periodic announcement entry parsed from config."""
    target: str           # channel name or room-server contact name/key
    target_type: str      # "channel" | "room"
    message: str
    schedule_label: str
    trigger: Any          # APScheduler CronTrigger


@dataclass
class QueuedDM:
    """A direct message waiting for delivery to the room server."""
    recipient: str
    content: str
    retry_count: int = 0
    first_queued: float = field(default_factory=time.time)
    next_retry_at: float = field(default_factory=time.time)


# ──────────────────────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────────────────────

class RoomServerBridgeService(BaseServicePlugin):
    """Bridges a MeshCore channel to a room server via DMs.

    * Watches ``watch_channel`` and forwards each message to
      ``room_server_name`` as a DM, prefixed with the sender's name.
    * Exposes a bot command (keyword ``bridge``) to toggle forwarding:
          !bridge on | off | status
    * Sends independent periodic announcements to the channel and/or room
      server via cron schedules in ``[RoomServerBridge_Announcements]``.

    Config section: ``[RoomServerBridge]``
    Announcements:  ``[RoomServerBridge_Announcements]``
    """

    config_section = "RoomServerBridge"
    description = "Forwards channel messages to a room server via DM, with announcements"

    settings_schema = [
        {"key": "enabled", "label": "Service enabled", "type": "bool", "default": True,
         "help": "Master enable/disable for the room server bridge service."},
        {"key": "watch_channel", "label": "Channel to watch", "type": "str", "default": "Public",
         "help": "Channel whose messages are forwarded to the room server."},
        {"key": "room_server_name", "label": "Room server name/key", "type": "str", "default": "",
         "help": "Name or public-key prefix of the room server contact to DM."},
        {"key": "bridge_enabled", "label": "Bridge enabled at startup", "type": "bool",
         "default": True, "help": "Whether forwarding is active when the service starts."},
        {"key": "command_keyword", "label": "Control command keyword", "type": "str",
         "default": "bridge", "help": "Bot command keyword to enable/disable the bridge."},
        {"key": "include_sender_name", "label": "Include sender name", "type": "bool",
         "default": True, "help": "Prefix DMs with the original sender's name."},
        {"key": "dm_template", "label": "DM template", "type": "str",
         "default": "[{sender}] {message}",
         "help": "Template for room-server DMs. Placeholders: {sender}, {message}."},
        {"key": "max_retries", "label": "Max DM retries", "type": "int",
         "default": 3, "min": 0, "max": 10,
         "help": "Maximum retry attempts for a failed DM before dropping it."},
        {"key": "retry_delay", "label": "Retry base delay (s)", "type": "float",
         "default": 2.0,
         "help": "Base seconds for exponential back-off between DM retries."},
        {"key": "max_queue_age", "label": "Max queue age (s)", "type": "int",
         "default": 300, "min": 30,
         "help": "Drop queued DMs older than this many seconds."},
    ]

    # ──────────────────────────────────────────────────────────────────────────
    # Init
    # ──────────────────────────────────────────────────────────────────────────

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        cfg = self.bot.config
        sec = self.config_section

        self.watch_channel: str = cfg.get(sec, "watch_channel", fallback="Public").strip()
        self.room_server_name: str = cfg.get(sec, "room_server_name", fallback="").strip()
        self.bridge_enabled: bool = cfg.getboolean(sec, "bridge_enabled", fallback=True)
        self.command_keyword: str = cfg.get(sec, "command_keyword", fallback="bridge").strip().lower()
        self.include_sender_name: bool = cfg.getboolean(sec, "include_sender_name", fallback=True)
        self.dm_template: str = cfg.get(sec, "dm_template", fallback="[{sender}] {message}").strip()
        self.max_retries: int = cfg.getint(sec, "max_retries", fallback=3)
        self.retry_delay: float = cfg.getfloat(sec, "retry_delay", fallback=2.0)
        self.max_queue_age: int = cfg.getint(sec, "max_queue_age", fallback=300)

        if not self.room_server_name:
            self.logger.error(
                "RoomServerBridge: room_server_name not configured — service will be disabled."
            )
            self.enabled = False
            return

        self._dm_queue: list[QueuedDM] = []
        self._queue_task: Optional[asyncio.Task] = None
        self._announcements: list[AnnouncementConfig] = []
        self._scheduler: Optional[BackgroundScheduler] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._load_announcements()

    # ──────────────────────────────────────────────────────────────────────────
    # Config helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _load_announcements(self) -> None:
        """Parse [RoomServerBridge_Announcements] entries from config."""
        ann_section = f"{self.config_section}_Announcements"
        if not self.bot.config.has_section(ann_section):
            return

        tz, _ = get_config_timezone(self.bot.config, self.logger)

        for key, raw_value in self.bot.config.items(ann_section):
            raw_value = (raw_value or "").strip()
            parts = raw_value.split(":", 2)
            if len(parts) < 3:
                self.logger.warning(
                    "RoomServerBridge: skipping malformed announcement '%s = %s' "
                    "(expected type:target:message)", key, raw_value
                )
                continue

            ann_type = parts[0].strip().lower()
            ann_target = parts[1].strip()
            ann_message = decode_escape_sequences(parts[2].strip())

            if ann_type not in ("channel", "room"):
                self.logger.warning(
                    "RoomServerBridge: unknown announcement type '%s' for key '%s' — "
                    "use 'channel' or 'room'.", ann_type, key
                )
                continue

            parse_result = parse_schedule_key(key, tz)
            if parse_result.trigger is None:
                self.logger.warning(
                    "RoomServerBridge: cannot parse schedule '%s' for announcement '%s' — skipping.",
                    key, raw_value
                )
                continue

            self._announcements.append(
                AnnouncementConfig(
                    target=ann_target,
                    target_type=ann_type,
                    message=ann_message,
                    schedule_label=parse_result.display_label,
                    trigger=parse_result.trigger,
                )
            )
            self.logger.info(
                "RoomServerBridge: loaded %s announcement -> '%s' [%s]: %s",
                ann_type, ann_target, parse_result.display_label, ann_message[:60],
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("RoomServerBridge: disabled — not starting.")
            return

        self._loop = asyncio.get_event_loop()
        self._subscribe_channel_events()
        self._register_command_listener()
        self._queue_task = asyncio.create_task(self._process_dm_queue())
        self._start_announcement_scheduler()
        self._running = True

        self.logger.info(
            "RoomServerBridge started. Watching '%s' -> '%s'. Bridge %s.",
            self.watch_channel,
            self.room_server_name,
            "ENABLED" if self.bridge_enabled else "DISABLED",
        )

    async def on_transport_reconnected(self) -> None:
        if not self._running or not getattr(self.bot, "meshcore", None):
            return
        self._subscribe_channel_events()
        self.logger.info("RoomServerBridge: re-subscribed after transport reconnect.")

    async def stop(self) -> None:
        self.logger.info("RoomServerBridge: stopping...")
        self._running = False
        self._unregister_command_listener()

        if self._queue_task:
            self._queue_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._queue_task
            self._queue_task = None

        if self._scheduler and getattr(self._scheduler, "running", False):
            with contextlib.suppress(Exception):
                self._scheduler.shutdown(wait=False)
        self._scheduler = None
        self.logger.info("RoomServerBridge: stopped.")

    # ──────────────────────────────────────────────────────────────────────────
    # Mesh event subscription
    # ──────────────────────────────────────────────────────────────────────────

    def _subscribe_channel_events(self) -> None:
        if not getattr(self.bot, "meshcore", None):
            self.logger.error("RoomServerBridge: meshcore not available, cannot subscribe.")
            return
        self.bot.meshcore.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_message)

    # ──────────────────────────────────────────────────────────────────────────
    # Command listener
    # ──────────────────────────────────────────────────────────────────────────

    def _register_command_listener(self) -> None:
        listeners = getattr(self.bot, "extra_message_listeners", None)
        if listeners is None:
            try:
                self.bot.extra_message_listeners = []
                listeners = self.bot.extra_message_listeners
            except AttributeError:
                self.logger.warning(
                    "RoomServerBridge: bot does not support extra_message_listeners — "
                    "enable/disable command unavailable."
                )
                return
        listeners.append(self._on_incoming_message)

    def _unregister_command_listener(self) -> None:
        listeners = getattr(self.bot, "extra_message_listeners", None)
        if listeners is not None:
            with contextlib.suppress(ValueError):
                listeners.remove(self._on_incoming_message)

    # ──────────────────────────────────────────────────────────────────────────
    # Channel message handler
    # ──────────────────────────────────────────────────────────────────────────

    async def _on_channel_message(self, event: Any, metadata: Any = None) -> None:
        if not self._running or not self.bridge_enabled:
            return
        try:
            payload = copy.deepcopy(event.payload) if hasattr(event, "payload") else None
            if not payload:
                return

            channel_idx = payload.get("channel_idx", 0)
            channel_name = self.bot.channel_manager.get_channel_name(channel_idx)

            if not self._channels_match(channel_name, self.watch_channel):
                return

            raw_text: str = payload.get("text", "").strip()
            if not raw_text:
                return

            sender_name, message_body = self._split_sender_message(raw_text)
            dm_content = self._build_dm(sender_name, message_body)
            self._enqueue_dm(self.room_server_name, dm_content)

        except Exception as exc:
            self.logger.error(
                "RoomServerBridge: error handling channel message: %s", exc, exc_info=True
            )

    @staticmethod
    def _channels_match(a: Optional[str], b: str) -> bool:
        if not a:
            return False
        return a.lstrip("#").strip().lower() == b.lstrip("#").strip().lower()

    @staticmethod
    def _split_sender_message(raw: str) -> tuple[str, str]:
        """Return (sender, body) from 'Sender: body'; falls back to ('Unknown', raw)."""
        if ":" in raw and not raw.startswith("http"):
            sender, body = raw.split(":", 1)
            return sender.strip(), body.strip()
        return "Unknown", raw

    def _build_dm(self, sender: str, message: str) -> str:
        if self.include_sender_name:
            return self.dm_template.format(sender=sender, message=message)
        return message

    # ──────────────────────────────────────────────────────────────────────────
    # Enable / disable command
    # ──────────────────────────────────────────────────────────────────────────

    async def _on_incoming_message(self, message: Any) -> bool:
        """Intercept !bridge on/off/status. Returns True when handled."""
        content: str = (getattr(message, "content", "") or "").strip().lstrip("!/.")
        lower = content.lower()
        kw = re.escape(self.command_keyword)

        if re.match(rf"^{kw}\s+on\b", lower):
            return await self._cmd_set_bridge(message, True)
        if re.match(rf"^{kw}\s+off\b", lower):
            return await self._cmd_set_bridge(message, False)
        if re.match(rf"^{kw}\s+status\b", lower):
            return await self._cmd_status(message)
        return False

    async def _cmd_set_bridge(self, message: Any, enable: bool) -> bool:
        self.bridge_enabled = enable
        state = "ENABLED" if enable else "DISABLED"
        self.logger.info(
            "RoomServerBridge: bridge %s by %s.", state, getattr(message, "sender", "?")
        )
        await self._reply(message, f"Room server bridge is now {state}.")
        return True

    async def _cmd_status(self, message: Any) -> bool:
        state = "ENABLED" if self.bridge_enabled else "DISABLED"
        reply = (
            f"Room server bridge: {state}. "
            f"Watching '{self.watch_channel}' -> '{self.room_server_name}'. "
            f"Queue: {len(self._dm_queue)} pending."
        )
        await self._reply(message, reply)
        return True

    async def _reply(self, message: Any, text: str) -> None:
        cm = getattr(self.bot, "command_manager", None)
        if cm is None:
            return
        if getattr(message, "is_dm", False):
            sender_key = getattr(message, "sender_key", None) or getattr(message, "sender", None)
            if sender_key:
                await cm.send_dm(sender_key, text, skip_user_rate_limit=True)
                return
        channel = getattr(message, "channel_name", None) or self.watch_channel
        await cm.send_channel_message(channel, text, skip_user_rate_limit=True)

    # ──────────────────────────────────────────────────────────────────────────
    # DM queue
    # ──────────────────────────────────────────────────────────────────────────

    def _enqueue_dm(self, recipient: str, content: str) -> None:
        self._dm_queue.append(QueuedDM(recipient=recipient, content=content))
        self.logger.debug("RoomServerBridge: queued DM -> %s: %s", recipient, content[:60])

    async def _process_dm_queue(self) -> None:
        """Background task: drain the DM queue with retry/back-off logic."""
        while self._running:
            try:
                now = time.time()
                to_remove: list[QueuedDM] = []

                for item in list(self._dm_queue):
                    if now - item.first_queued > self.max_queue_age:
                        to_remove.append(item)
                        self.logger.warning(
                            "RoomServerBridge: dropping stale DM to %s (age %.0fs).",
                            item.recipient, now - item.first_queued,
                        )
                        continue

                    if now < item.next_retry_at:
                        continue

                    cm = getattr(self.bot, "command_manager", None)
                    if cm is None:
                        break

                    success = await cm.send_dm(
                        item.recipient, item.content, skip_user_rate_limit=True
                    )
                    if success:
                        to_remove.append(item)
                    else:
                        item.retry_count += 1
                        if item.retry_count > self.max_retries:
                            to_remove.append(item)
                            self.logger.error(
                                "RoomServerBridge: giving up on DM to %s after %d retries.",
                                item.recipient, self.max_retries,
                            )
                        else:
                            delay = self.retry_delay * (2 ** (item.retry_count - 1))
                            item.next_retry_at = now + delay

                for item in to_remove:
                    with contextlib.suppress(ValueError):
                        self._dm_queue.remove(item)

                await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.error(
                    "RoomServerBridge: error in DM queue processor: %s", exc, exc_info=True
                )
                await asyncio.sleep(2.0)

    # ──────────────────────────────────────────────────────────────────────────
    # Announcement scheduler
    # ──────────────────────────────────────────────────────────────────────────

    def _start_announcement_scheduler(self) -> None:
        if not self._announcements:
            return

        tz, _ = get_config_timezone(self.bot.config, self.logger)
        self._scheduler = BackgroundScheduler(timezone=tz)

        for idx, ann in enumerate(self._announcements):
            job_id = f"rsb_ann_{idx}_{ann.target_type}_{ann.target}"
            self._scheduler.add_job(
                self._fire_announcement,
                ann.trigger,
                args=[ann],
                id=job_id,
                replace_existing=True,
            )
            self.logger.info(
                "RoomServerBridge: scheduled %s announcement -> '%s' [%s].",
                ann.target_type, ann.target, ann.schedule_label,
            )

        self._scheduler.start()
        self.logger.info(
            "RoomServerBridge: announcement scheduler started (%d job(s)).",
            len(self._announcements),
        )

    def _fire_announcement(self, ann: AnnouncementConfig) -> None:
        """APScheduler callback (background thread) — dispatches into the asyncio loop."""
        if not self._running or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._send_announcement(ann), self._loop)

    async def _send_announcement(self, ann: AnnouncementConfig) -> None:
        cm = getattr(self.bot, "command_manager", None)
        if cm is None:
            self.logger.warning("RoomServerBridge: command_manager unavailable for announcement.")
            return
        try:
            if ann.target_type == "channel":
                sent = await cm.send_channel_message(ann.target, ann.message, skip_user_rate_limit=True)
            else:
                sent = await cm.send_dm(ann.target, ann.message, skip_user_rate_limit=True)

            if sent:
                self.logger.info(
                    "RoomServerBridge: sent %s announcement -> '%s': %s",
                    ann.target_type, ann.target, ann.message[:60],
                )
            else:
                self.logger.warning(
                    "RoomServerBridge: failed to send %s announcement -> '%s'.",
                    ann.target_type, ann.target,
                )
        except Exception as exc:
            self.logger.error(
                "RoomServerBridge: error sending announcement -> '%s': %s",
                ann.target, exc, exc_info=True,
            )
