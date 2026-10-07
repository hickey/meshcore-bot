"""Periodic battery monitoring for configured remote MeshCore nodes."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Optional

from .base_service import BaseServicePlugin


_PUBLIC_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class BatteryMonitorService(BaseServicePlugin):
    """Poll configured repeaters and room servers for battery telemetry."""

    config_section = "Battery_Monitor_Service"
    description = "Records battery readings from configured remote MeshCore nodes"

    settings_schema = [
        {
            "key": "nodes",
            "label": "Nodes",
            "type": "pubkey_list",
            "default": [],
            "pattern": r"[0-9a-fA-F]{64}",
            "help": "Public keys for repeaters and room servers to poll.",
        },
        {
            "key": "check_interval",
            "label": "Check interval",
            "type": "int",
            "min": 1,
            "default": 3600,
            "unit": "s",
            "help": "How often to query every configured node.",
        },
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        section = self.config_section
        self.check_interval = max(
            1,
            self.bot.config.getint(section, "check_interval", fallback=3600),
        )
        self.nodes = self._load_nodes()
        self._poll_task: Optional[asyncio.Task[Any]] = None
        self._query_lock = asyncio.Lock()

    def _load_nodes(self) -> list[str]:
        raw = self.bot.config.get(self.config_section, "nodes", fallback="")
        nodes: list[str] = []
        seen: set[str] = set()
        for value in raw.split(","):
            public_key = value.strip().lower()
            if not public_key:
                continue
            if not _PUBLIC_KEY_RE.fullmatch(public_key):
                self.logger.warning(
                    "Ignoring invalid Battery_Monitor_Service node public key: %s",
                    value.strip(),
                )
                continue
            if public_key not in seen:
                seen.add(public_key)
                nodes.append(public_key)
        return nodes

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("Battery monitor service is disabled, not starting")
            return
        if self._poll_task and not self._poll_task.done():
            self._running = True
            return
        self._running = True
        self._poll_task = asyncio.create_task(
            self.run_periodic(
                self._check_nodes,
                lambda: float(self.check_interval),
                "Error in battery monitor poll loop",
            )
        )
        self.logger.info(
            "Battery monitor service started (%d node(s), interval=%ds)",
            len(self.nodes),
            self.check_interval,
        )

    async def stop(self) -> None:
        self._running = False
        await self._cancel_tasks(self._poll_task)
        self._poll_task = None
        self.logger.info("Battery monitor service stopped")

    async def _check_nodes(self) -> None:
        if not self.nodes or not getattr(self.bot, "connected", False):
            return
        meshcore = getattr(self.bot, "meshcore", None)
        commands = getattr(meshcore, "commands", None)
        request = getattr(commands, "req_status_sync", None)
        if meshcore is None or not callable(request):
            self.logger.debug("Battery monitor skipped: remote status API unavailable")
            return

        for public_key in self.nodes:
            try:
                async with self._query_lock:
                    contact = self._get_contact(meshcore, public_key)
                    if contact is None:
                        self.logger.warning("Battery monitor node is not in contacts: %s", public_key)
                        continue
                    await commands.reset_path(contact)
                    status = await request(contact, timeout=30.0)
                    voltage = self._voltage_from_status(status)
                    counters = self._message_counters_from_status(status)
                    percentage = None
                if voltage is None:
                    self.logger.warning("Battery monitor returned no voltage for node %s", public_key)
                    continue
                self._store_reading(public_key, percentage, voltage, counters)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("Battery monitor query failed for %s: %s", public_key, exc)

    @staticmethod
    def _get_contact(meshcore: Any, public_key: str) -> Any:
        contacts = getattr(meshcore, "contacts", None)
        if isinstance(contacts, dict):
            contact = contacts.get(public_key)
            if contact is None:
                contact = contacts.get(public_key.upper())
            if contact is not None:
                return contact
        lookup = getattr(meshcore, "get_contact_by_key_prefix", None)
        return lookup(public_key) if callable(lookup) else None

    @staticmethod
    def _message_counters_from_status(status: Any) -> tuple[Optional[int], Optional[int]]:
        payload = getattr(status, "payload", status)
        if not isinstance(payload, dict):
            return None, None
        counters = []
        for key in ("nb_recv", "nb_sent"):
            try:
                value = payload.get(key)
                counters.append(int(value) if value is not None and int(value) >= 0 else None)
            except (TypeError, ValueError):
                counters.append(None)
        return counters[0], counters[1]

    @staticmethod
    def _voltage_from_status(status: Any) -> Optional[float]:
        payload = getattr(status, "payload", status)
        if not isinstance(payload, dict):
            return None
        raw = payload.get("bat")
        try:
            millivolts = float(raw)
        except (TypeError, ValueError):
            return None
        return millivolts / 1000.0 if millivolts >= 0 else None

    async def _percentage_from_telemetry(self, commands: Any, contact: Any) -> Optional[float]:
        request = getattr(commands, "req_telemetry_sync", None)
        if not callable(request):
            return None
        try:
            telemetry = await request(contact, timeout=30.0)
        except (asyncio.CancelledError,):
            raise
        except Exception as exc:
            self.logger.debug("Battery telemetry unavailable: %s", exc)
            return None
        if isinstance(telemetry, dict):
            telemetry = telemetry.get("lpp", telemetry.get("data", []))
        if not isinstance(telemetry, (list, tuple)):
            return None
        for item in telemetry:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type", "")).strip().lower()
            if kind not in {"percentage", "percent", "battery_percentage"}:
                continue
            try:
                value = float(item.get("value"))
            except (TypeError, ValueError):
                continue
            if 0 <= value <= 100:
                return value
        return None

    def _store_reading(
        self,
        public_key: str,
        percentage: Optional[float],
        voltage: float,
        counters: tuple[Optional[int], Optional[int]],
    ) -> None:
        db_manager = getattr(self.bot, "db_manager", None)
        if db_manager is None:
            self.logger.warning("Battery monitor cannot persist without a database manager")
            return
        timestamp = datetime.now(timezone.utc).isoformat()
        store = getattr(db_manager, "store_battery_observation", None)
        if callable(store):
            store(
                public_key,
                timestamp,
                percentage,
                voltage,
                counters[0],
                counters[1],
                None,
            )
            return
        self.logger.warning("Battery monitor database manager lacks atomic observation support")
