"""Battery monitoring data for the web viewer."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any


_PUBLIC_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
BATTERY_INTERVALS = {
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
    "2w": timedelta(days=14),
    "1mo": timedelta(days=30),
    "3mo": timedelta(days=90),
}
DEFAULT_BATTERY_INTERVAL = "7d"


class BatteryMixin:
    """Read configured battery observations and contact names."""

    config: Any
    _db_connection: Any

    def _battery_nodes(self) -> list[str]:
        raw = self.config.get("Battery_Monitor_Service", "nodes", fallback="")
        nodes: list[str] = []
        seen: set[str] = set()
        for value in raw.split(","):
            key = value.strip().lower()
            if key and _PUBLIC_KEY_RE.fullmatch(key) and key not in seen:
                seen.add(key)
                nodes.append(key)
        return nodes

    def _get_battery_data(self, interval: str = DEFAULT_BATTERY_INTERVAL) -> dict[str, Any]:
        if interval not in BATTERY_INTERVALS:
            interval = DEFAULT_BATTERY_INTERVAL
        now = datetime.now(timezone.utc)
        cutoff = now - BATTERY_INTERVALS[interval]
        try:
            check_interval = max(
                1,
                self.config.getint(
                    "Battery_Monitor_Service", "check_interval", fallback=3600
                ),
            )
        except (TypeError, ValueError):
            check_interval = 3600
        stale_after = max(check_interval * 2, 2 * 60 * 60)
        nodes = self._battery_nodes()
        result = {
            "interval": interval,
            "from": cutoff.isoformat(),
            "to": now.isoformat(),
            "stale_after_seconds": stale_after,
            "nodes": [],
        }
        if not nodes:
            return result

        placeholders = ", ".join("?" for _ in nodes)
        try:
            with self._db_connection() as conn:
                contacts = conn.execute(
                    f"""
                    SELECT public_key, name
                    FROM complete_contact_tracking
                    WHERE LOWER(public_key) IN ({placeholders})
                    ORDER BY last_heard DESC, id DESC
                    """,  # noqa: S608 - placeholders are generated, not values
                    tuple(nodes),
                ).fetchall()
                names: dict[str, str] = {}
                for row in contacts:
                    key = (row["public_key"] or "").lower()
                    if key not in names and (row["name"] or "").strip():
                        names[key] = row["name"].strip()

                history = conn.execute(
                    f"""
                    SELECT LOWER(public_key) AS public_key, timestamp, battery_voltage
                    FROM battery_levels
                    WHERE LOWER(public_key) IN ({placeholders})
                      AND timestamp >= ?
                    ORDER BY timestamp ASC, id ASC
                    """,  # noqa: S608 - placeholders are generated, not values
                    (*nodes, cutoff.isoformat()),
                ).fetchall()
                latest = conn.execute(
                    f"""
                    SELECT public_key, timestamp, battery_voltage
                    FROM (
                        SELECT LOWER(public_key) AS public_key, timestamp,
                               id, battery_voltage,
                               ROW_NUMBER() OVER (
                                   PARTITION BY LOWER(public_key)
                                   ORDER BY timestamp DESC, id DESC
                               ) AS row_number
                        FROM battery_levels
                        WHERE LOWER(public_key) IN ({placeholders})
                    )
                    WHERE row_number = 1
                    """,  # noqa: S608 - placeholders are generated, not values
                    tuple(nodes),
                ).fetchall()
        except Exception as exc:
            self.logger.warning("Battery viewer data unavailable: %s", exc)
            result["nodes"] = [
                {
                    "name": "Unnamed configured node",
                    "status": "no_data",
                    "current": None,
                    "points": [],
                }
                for _ in nodes
            ]
            return result

        points_by_key: dict[str, list[dict[str, Any]]] = {key: [] for key in nodes}
        for row in history:
            try:
                voltage = float(row["battery_voltage"])
            except (TypeError, ValueError):
                continue
            if voltage < 0:
                continue
            points_by_key[row["public_key"]].append(
                {"timestamp": row["timestamp"], "voltage": voltage}
            )
        latest_by_key = {row["public_key"]: row for row in latest}
        for key in nodes:
            row = latest_by_key.get(key)
            current = None
            status = "no_data"
            if row is not None:
                try:
                    voltage = float(row["battery_voltage"])
                except (TypeError, ValueError):
                    voltage = None
                if voltage is not None and voltage >= 0:
                    timestamp = str(row["timestamp"])
                    current = {
                        "voltage": voltage,
                        "timestamp": timestamp,
                    }
                    try:
                        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        age = (now - parsed.astimezone(timezone.utc)).total_seconds()
                        status = "ok" if age <= stale_after else "not_responding"
                    except ValueError:
                        status = "not_responding"
            result["nodes"].append(
                {
                    "name": names.get(key, "Unnamed configured node"),
                    "status": status,
                    "current": current,
                    "points": points_by_key[key],
                }
            )
        return result
