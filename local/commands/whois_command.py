#!/usr/bin/env python3
"""
Whois command for the MeshCore Bot
Looks up callsign and displays basic user info
"""

import re
import time
import requests
import xml.dom.minidom

from modules.models import MeshMessage
from modules.commands.base_command import BaseCommand


class WhoisError(RuntimeError):
    pass

class WhoisCommand(BaseCommand):
    """Handles whois command"""

    # Plugin metadata
    name = "whois"
    keywords = ['whois']
    description = "Lookup callsign and display owner information"
    category = "basic"

    # Documentation
    short_description = "Display callsign information"
    usage = "whois [callsign]"
    examples = [
        "whois wt0f"
    ]
    parameters = [
        {"name": "callsign", "description": "Callsign to lookup"}
    ]

    settings_schema = [
        {
            "key": "hamqth_username",
            "label": "HamQTH Username",
            "type": "str",
            "default": "",
            "help": "Username used to authenticate to HamQTH"
        },
        {
            "key": "hamqth_password",
            "label": "HamQTH Password",
            "type": "str",
            "default": "",
            "help": "Password used to authenticate to HamQTH"
        },
    ]

    def __init__(self, bot):
        """Initialize the whois command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.enabled = self.get_config_value("Whois_Command", "enabled", fallback=True, value_type="bool")
        self.username = self.get_config_value("Whois_Command", "hamqth_username", fallback="", value_type="str")
        self.password = self.get_config_value("Whois_Command", "hamqth_password", fallback="", value_type="str")
        self.format = self.get_config_value("Whois_Command", "format",
                                        fallback="{callsign} is {adr_name} from {qth}, {country}, {continent}",
                                        value_type="str")
        self._session_start = 0
        self._session_id = None
        self._auth_count = 0

        if self.username == "" or self.password == "":
            # disable if username or password is empty
            self.enabled = False
            self.logger.info("Whois_Command disabled due to empty username or password")

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.enabled:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        """Get help text for the contact command.

        Returns:
            str: Help text string.
        """
        return self.translate('commands.whois.help')

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Override to handle whois-specific matching.

        Args:
            message: The received message.

        Returns:
            bool: True if message is a whois command, False otherwise.
        """
        content_lower = self.cleanup_message_for_matching(message)

        if content_lower == 'whois':
            return True

        # Check for whosi with parameter
        if content_lower.startswith("whois "):
            words = content_lower.split()
            if len(words) >= 2 and words[0] == "whois":
                return True  # Match any whois command, validation in execute()

        return False

    def build_response(self, record: dict[str]) -> str:
        """Format a lookup response from self.format

        Args:
            record: dictonary of keywords and values

        Returns:
            str: formatted string
        """
        def keyword_expansion(match, data: dict[str]) -> str:
            # Extract the matched text string
            kw = match.group(1)
            # Return the new string to replace the match
            return data[kw] or ""

        response = re.sub(r'\{\s*(\S+)\s*\}',
                          lambda m, d=record: keyword_expansion(m, d),
                          self.format)
        return response

    def hamqth_authenticate(self) -> str:
        """Authenticate to HamQTH if needed

        Returns:
            str: session ID to use.
        """
        now = time.time()
        # last session ID was created within (the last hour - 10 secs)
        if now - self._session_start < 3590:
            self._auth_count = 0
            return self._session_id

        # check to make sure we are not in a loop of failed auths
        if self._auth_count > 2:
            # reset auth count so we can try again on next lookup
            self._auth_count = 0
            raise WhoisError("Too many failed authentications")

        auth_params = {
            "u": self.username,
            "p": self.password
        }
        auth_resp = requests.get("https://www.hamqth.com/xml.php", auth_params)
        # keep track of auths so we do not get into a loop on errors
        self._auth_count += 1

        if auth_resp.status_code != 200:
            raise WhoisError("HamQTH server error: status code = {auth_resp.status_code}")

        # locate the session ID
        dom = xml.dom.minidom.parseString(auth_resp.content)

        # check for error
        error = dom.getElementsByTagName("error")
        if error:
            error_text = error[0].firstChild.data
            if error_text =="Session does not exist or expired":
                # Need to force reauthentication
                self._session_start = 0
                return self.hamqth_authenticate()

            # some other error that we can not handle
            raise WhoisError(f"HamQTH authentication error: {error_text}")

        self._session_start = now
        session = dom.getElementsByTagName("session_id")
        self.logger.debug(f"HamQTH session id: {session[0].firstChild.data}")
        return session[0].firstChild.data

    def hamqth_query(self, callsign: str) -> dict:
        """Query HamQTH for callsign record

        Args:
            callsign: Callsign to lookup

        Returns:
            dict: record data
        """
        query_params = {
            "id": self._session_id,
            "callsign": callsign,
            "prg": "meshcore-bot/whois"
        }
        query_resp = requests.get("https://www.hamqth.com/xml.php", query_params)

        if query_resp.status_code != 200:
            raise WhoisError("HamQTH server error: status code = {auth_resp.status_code}")

        # locate the session ID
        dom = xml.dom.minidom.parseString(query_resp.content)

        # check for error
        error = dom.getElementsByTagName("error")
        if error:
            error_text = error[0].firstChild.data
            if error_text =="Callsign not found":
                return { "error": "Callsign not found" }

            # Unknown error
            raise WhoisError(f"HamQTH query error: {error_text}")

        def xml_to_dict(element: xml.dom.minidom.Node) -> dict:
            data = {}
            if element.hasChildNodes():
                for child in element.childNodes:
                    if  child.nodeType == child.ELEMENT_NODE:
                        if child.firstChild and child.firstChild.data:
                            data[child.nodeName] = child.firstChild.data
                        else:
                            data[child.nodeName] = ""
            return data

        record_data = dom.getElementsByTagName("search")[0]
        record = xml_to_dict(record_data)

        return record

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the whois command.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if executed successfully, False otherwise.
        """
        content = message.content.strip()

        # Handle command-style messages
        if content.startswith('!'):
            content = content[1:].strip()

        # Default to d6 if no specification
        if content.lower() == "whois":
            return await self.send_response(message,
                                            self.translate("commands.whois.noparam"))

        # Parse dice specification
        callsign = content[6:].strip()  # Get everything after "whois "

        self._session_id = self.hamqth_authenticate()
        record = self.hamqth_query(callsign)
        self.logger.debug(f"HamQTH {record=}")

        response = self.build_response(record)
        return await self.send_response(message, response)
