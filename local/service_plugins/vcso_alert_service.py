#!/usr/bin/env python3
"""
VCSO Alert Service for MeshCore Bot.

Scrapes the Volusia County Sheriff's Office active calls page and provides
incident alerts both in query mode (via the alert command) and polling mode
(auto-posting new incidents to channels based on priority threshold).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Optional
from html.parser import HTMLParser

import requests

from modules.service_plugins.base_alert_service import (
    QUERY_TYPE_CITY,
    BaseAlertService,
)


class VCSOTableParser(HTMLParser):
    """Parser for the VCSO Active Calls HTML table."""

    def __init__(self):
        super().__init__()
        self.incidents = []
        self.current_incident = {}
        self.current_field = None
        self.in_table = False
        self.in_row = False
        self.field_map = {
            'CallNoLabel': 'call_number',
            'CallDescLabel': 'description',
            'PriorityLabel': 'priority',
            'LocationLabel': 'location',
            'TimeEnteredLabel': 'entry_time',
            'Zone': 'zone'
        }

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == 'table' and attrs_dict.get('id') == 'ActiveCallsTbl':
            self.in_table = True
        elif self.in_table and tag == 'tr':
            class_val = attrs_dict.get('class', '')
            if class_val in ('row', 'alt'):
                self.in_row = True
                self.current_incident = {}
        elif self.in_row and tag == 'span':
            span_id = attrs_dict.get('id', '')
            for label, field in self.field_map.items():
                if label in span_id:
                    self.current_field = field
                    break

    def handle_data(self, data):
        if self.current_field and self.in_row:
            self.current_incident[self.current_field] = data.strip()

    def handle_endtag(self, tag):
        if tag == 'span':
            self.current_field = None
        elif tag == 'tr' and self.in_row:
            if self.current_incident:
                self.incidents.append(self.current_incident)
            self.in_row = False
            self.current_incident = {}
        elif tag == 'table' and self.in_table:
            self.in_table = False


class VCSOAlertService(BaseAlertService):
    """Alert service that scrapes VCSO active calls page.

    Supports city-based queries and polling mode with priority-based filtering.
    Keeps incident data in memory for query responses and posts new high-priority
    incidents to configured channels.
    """

    alert_service_id = "vcso"
    config_section = "VCSO_Alert_Service"
    description = "Volusia County Sheriff's Office active calls"

    settings_schema = [
        {"key": "enabled", "label": "Enabled", "type": "bool", "default": False,
         "help": "Enable the VCSO alert service."},
        {"key": "label", "label": "Service label", "type": "str", "default": "VCSO",
         "help": "Short label (<=6 chars) prefixed to messages, e.g. [VCSO]."},
        {"key": "polling_enabled", "label": "Enable polling", "type": "bool",
         "default": False,
         "help": "Periodically post new incidents to polling_channels."},
        {"key": "polling_interval", "label": "Poll interval", "type": "int",
         "min": 30000, "default": 300000, "unit": "ms",
         "help": "How often to poll for new incidents, in milliseconds."},
        {"key": "polling_channels", "label": "Polling channels", "type": "str",
         "default": "",
         "help": "Comma-separated channels to post new incidents to (e.g. #alerts)."},
        {"key": "min_priority", "label": "Minimum priority for posting", "type": "int",
         "min": 1, "max": 5, "default": 3,
         "help": "Only post incidents with priority at or below this value (1=highest)."},
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        self.url = "https://www.vcso.us/ActiveCalls/"
        self.url_timeout = 10

        # In-memory storage of active incidents
        self._incidents: dict[str, dict] = {}

        # Priority threshold for posting (lower number = higher priority)
        self.min_priority = self.bot.config.getint(
            self.config_section, "min_priority", fallback=3
        )

        # Format strings for posting and queries
        self.post_format = self.bot.config.get(
            self.config_section,
            "post_format",
            fallback="{location} - {description} (Zone: {zone})"
        ).strip()

        self.query_summary_format = self.bot.config.get(
            self.config_section,
            "query_summary_format",
            fallback="{description}: {location} (P{priority}, {entry_time})"
        ).strip()

        self.query_detail_format = self.bot.config.get(
            self.config_section,
            "query_detail_format",
            fallback=("Call: {call_number}\n"
                     "Type: {description}\n"
                     "Priority: {priority}\n"
                     "Location: {location}\n"
                     "Zone: {zone}\n"
                     "Time: {entry_time}")
        ).strip()

    def get_capabilities(self) -> dict[str, bool]:
        """VCSO supports city-based queries."""
        return {
            QUERY_TYPE_CITY: True,
        }

    def parse_query(self, query: str) -> tuple[str, Optional[str], Optional[float], Optional[float]]:
        """Parse user query - treat everything as a city/location filter."""
        q = query.strip()
        if not q:
            return (QUERY_TYPE_CITY, None, None, None)
        return (QUERY_TYPE_CITY, q, None, None)

    async def query_alerts(self, query: str) -> list[str]:
        """Return incident lines matching the query.

        Args:
            query: Raw query text (treated as location filter, or empty for all).

        Returns:
            List of formatted incident strings.
        """
        query_type, location, _, _ = self.parse_query(query)

        # Check if query is asking for a specific call number
        if query.strip().isdigit() and len(query.strip()) >= 6:
            # Detailed query for specific incident
            call_number = query.strip()
            if call_number in self._incidents:
                incident = self._incidents[call_number]
                return [self._format_incident(incident, detail=True)]
            return []

        # Summary query - filter by location if provided
        results = []
        for call_number, incident in self._incidents.items():
            if location:
                # Case-insensitive substring match on location
                if location.lower() not in incident.get('location', '').lower():
                    continue
            results.append(self._format_incident(incident, detail=False))

        # Sort by priority (ascending) then entry time (descending/most recent first)
        results.sort(key=lambda x: (
            int(re.search(r'P(\d+)', x).group(1)) if re.search(r'P(\d+)', x) else 999,
            x
        ))

        return results

    async def fetch_new_incidents(self) -> list[tuple[str, str]]:
        """Fetch current incidents from VCSO page for polling.

        Returns:
            List of (call_number, formatted_line) tuples for incidents meeting
            the priority threshold.
        """
        try:
            # Fetch and parse in a thread to avoid blocking
            incidents = await asyncio.to_thread(self._scrape_incidents)

            # Update in-memory storage
            current_call_numbers = set()
            for incident in incidents:
                call_number = incident.get('call_number')
                if call_number:
                    current_call_numbers.add(call_number)
                    self._incidents[call_number] = incident

            # Remove incidents that are no longer on the page
            removed = set(self._incidents.keys()) - current_call_numbers
            for call_number in removed:
                del self._incidents[call_number]
                self.logger.debug(
                    "VCSO: Removed resolved incident %s", call_number
                )

            # Return incidents meeting priority threshold for posting
            results = []
            for incident in incidents:
                call_number = incident.get('call_number')
                priority = int(incident.get('priority', 999))

                if priority <= self.min_priority and call_number:
                    line = self._format_incident(incident, detail=False, for_post=True)
                    results.append((call_number, line))

            return results

        except Exception as e:
            self.logger.error("VCSO: Error fetching incidents: %s", e, exc_info=True)
            return []

    def _scrape_incidents(self) -> list[dict]:
        """Scrape the VCSO active calls page (blocking I/O).

        Returns:
            List of incident dictionaries.
        """
        try:
            response = requests.get(self.url, timeout=self.url_timeout)
            response.raise_for_status()

            parser = VCSOTableParser()
            parser.feed(response.text)

            self.logger.debug("VCSO: Scraped %d incidents", len(parser.incidents))
            return parser.incidents

        except requests.exceptions.RequestException as e:
            self.logger.error("VCSO: HTTP error scraping page: %s", e)
            raise
        except Exception as e:
            self.logger.error("VCSO: Error parsing page: %s", e)
            raise

    def _format_incident(self, incident: dict, detail: bool = False, for_post: bool = False) -> str:
        """Format an incident for display.

        Args:
            incident: Incident dictionary with fields from the table.
            detail: If True, use detailed format; otherwise summary format.
            for_post: If True, use post format for channel posting.

        Returns:
            Formatted incident string.
        """
        # Ensure all fields have defaults
        data = {
            'call_number': incident.get('call_number', 'Unknown'),
            'description': incident.get('description', 'Unknown'),
            'priority': incident.get('priority', '?'),
            'location': incident.get('location', 'Unknown'),
            'entry_time': incident.get('entry_time', 'Unknown'),
            'zone': incident.get('zone', 'Unknown')
        }

        try:
            if for_post:
                return self.post_format.format(**data)
            elif detail:
                return self.query_detail_format.format(**data)
            else:
                return self.query_summary_format.format(**data)
        except KeyError as e:
            self.logger.warning("VCSO: Format string references unknown field: %s", e)
            # Fallback format
            return f"{data['description']}: {data['location']} (P{data['priority']}, {data['entry_time']})"
