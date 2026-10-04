#!/usr/bin/env python3
"""Test script for VCSO Alert Service."""

import asyncio
import configparser
import os
import ssl
import sys
from unittest.mock import Mock

# Add repository root to path to import the local service package.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from local.service_plugins.vcso_alert_service import VCSOAlertService, VCSOTableParser


def test_parser():
    """Test HTML parser with sample data."""
    html_sample = """
    <table class="ListView" id="ActiveCallsTbl">
        <tr>
            <th>Call Number</th>
            <th>Description</th>
            <th>Priority</th>
            <th>Location</th>
            <th>Entry Time</th>
            <th>Zone</th>
        </tr>
        <tr class="row">
            <td><span id="ctl00_ContentPlaceHolder1_ActiveCallsListView_ctrl0_CallNoLabel">262520075</span></td>
            <td><span id="ctl00_ContentPlaceHolder1_ActiveCallsListView_ctrl0_CallDescLabel">Extra Patrol</span></td>
            <td><span id="ctl00_ContentPlaceHolder1_ActiveCallsListView_ctrl0_PriorityLabel">4</span></td>
            <td><span id="ctl00_ContentPlaceHolder1_ActiveCallsListView_ctrl0_LocationLabel">Ormond Beach</span></td>
            <td><span id="ctl00_ContentPlaceHolder1_ActiveCallsListView_ctrl0_TimeEnteredLabel">1:23 AM</span></td>
            <td><span id="ctl00_ContentPlaceHolder1_ActiveCallsListView_ctrl0_Zone">D1NA</span></td>
        </tr>
        <tr class="alt">
            <td><span id="ctl00_ContentPlaceHolder1_ActiveCallsListView_ctrl1_CallNoLabel">262520051</span></td>
            <td><span id="ctl00_ContentPlaceHolder1_ActiveCallsListView_ctrl1_CallDescLabel">Suspicious Vehicle</span></td>
            <td><span id="ctl00_ContentPlaceHolder1_ActiveCallsListView_ctrl1_PriorityLabel">3</span></td>
            <td><span id="ctl00_ContentPlaceHolder1_ActiveCallsListView_ctrl1_LocationLabel">Orange City</span></td>
            <td><span id="ctl00_ContentPlaceHolder1_ActiveCallsListView_ctrl1_TimeEnteredLabel">12:53 AM</span></td>
            <td><span id="ctl00_ContentPlaceHolder1_ActiveCallsListView_ctrl1_Zone">64C</span></td>
        </tr>
    </table>
    """

    parser = VCSOTableParser()
    parser.feed(html_sample)

    print(f"Parsed {len(parser.incidents)} incidents:")
    for inc in parser.incidents:
        print(f"  Call: {inc.get('call_number')}")
        print(f"  Desc: {inc.get('description')}")
        print(f"  Priority: {inc.get('priority')}")
        print(f"  Location: {inc.get('location')}")
        print(f"  Zone: {inc.get('zone')}")
        print(f"  Time: {inc.get('entry_time')}")
        print()

    assert len(parser.incidents) == 2
    assert parser.incidents[0]['call_number'] == '262520075'
    assert parser.incidents[1]['priority'] == '3'
    print("✓ Parser test passed")


async def test_service():
    """Test service initialization and formatting."""
    # Create mock bot with config
    mock_bot = Mock()
    config = configparser.ConfigParser()
    config.add_section('VCSO_Alert_Service')
    config.set('VCSO_Alert_Service', 'enabled', 'true')
    config.set('VCSO_Alert_Service', 'label', 'VCSO')
    config.set('VCSO_Alert_Service', 'min_priority', '3')
    config.set('VCSO_Alert_Service', 'post_format', '{location} - {description} (Zone: {zone})')
    config.set('VCSO_Alert_Service', 'query_summary_format', '{description}: {location} (P{priority}, {entry_time})')

    mock_bot.config = config
    mock_bot.logger = Mock()

    # Create service
    service = VCSOAlertService(mock_bot)

    # Test formatting
    test_incident = {
        'call_number': '262520075',
        'description': 'Extra Patrol',
        'priority': '4',
        'location': 'Ormond Beach',
        'entry_time': '1:23 AM',
        'zone': 'D1NA'
    }

    summary = service._format_incident(test_incident, detail=False, for_post=False)
    print(f"Summary format: {summary}")
    assert 'Extra Patrol' in summary
    assert 'Ormond Beach' in summary
    print("✓ Summary formatting passed")

    post = service._format_incident(test_incident, detail=False, for_post=True)
    print(f"Post format: {post}")
    assert 'Ormond Beach' in post
    assert 'D1NA' in post
    print("✓ Post formatting passed")

    # Test capabilities
    caps = service.get_capabilities()
    assert 'city' in caps
    print("✓ Capabilities test passed")

    # Test query parsing
    query_type, location, _, _ = service.parse_query("daytona")
    assert query_type == 'city'
    assert location == 'daytona'
    print("✓ Query parsing passed")


async def test_scrape_with_aiohttp(monkeypatch):
    """Test async scraping without contacting the live VCSO site."""
    mock_bot = Mock()
    config = configparser.ConfigParser()
    config.add_section('VCSO_Alert_Service')
    config.set('VCSO_Alert_Service', 'enabled', 'true')
    mock_bot.config = config
    mock_bot.logger = Mock()
    mock_bot.bot_root = '/tmp'
    service = VCSOAlertService(mock_bot)

    html = '<table id="ActiveCallsTbl"><tr class="row"><td><span id="CallNoLabel">123456</span></td></tr></table>'

    class FakeResponse:
        def __init__(self):
            self.status_checked = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def raise_for_status(self):
            self.status_checked = True

        async def text(self):
            return html

    class FakeSession:
        closed = False

        def get(self, url):
            assert url == service.url
            return FakeResponse()

    service._http_session = FakeSession()
    incidents = await service._scrape_incidents()

    assert incidents == [{'call_number': '123456'}]


async def test_ssl_context_loads_additional_ca_file(tmp_path):
    """Configured local CA files are loaded into the verified default context."""
    mock_bot = Mock()
    config = configparser.ConfigParser()
    config.add_section('VCSO_Alert_Service')
    config.set('VCSO_Alert_Service', 'additional_ca_file', 'ca.pem')
    mock_bot.config = config
    mock_bot.logger = Mock()
    mock_bot.bot_root = str(tmp_path)
    ca_file = tmp_path / 'ca.pem'
    ca_file.write_text('placeholder')
    service = VCSOAlertService(mock_bot)

    class FakeContext:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED

        def __init__(self):
            self.loaded = None

        def load_verify_locations(self, **kwargs):
            self.loaded = kwargs

    context = FakeContext()
    original_create = ssl.create_default_context
    try:
        ssl.create_default_context = lambda: context
        assert service._build_ssl_context() is context
    finally:
        ssl.create_default_context = original_create

    assert context.loaded == {'cafile': str(ca_file), 'capath': None}
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED


    """The service closes its aiohttp session when stopped."""
    mock_bot = Mock()
    config = configparser.ConfigParser()
    config.add_section('VCSO_Alert_Service')
    config.set('VCSO_Alert_Service', 'enabled', 'true')
    mock_bot.config = config
    mock_bot.logger = Mock()
    mock_bot.bot_root = '/tmp'
    service = VCSOAlertService(mock_bot)

    class FakeSession:
        closed = False

        async def close(self):
            self.closed = True

    session = FakeSession()
    service._http_session = session
    await service.stop()

    assert session.closed
    assert service._http_session is None


if __name__ == '__main__':
    print("Testing VCSO Alert Service\n")
    print("=" * 50)

    test_parser()
    print()

    asyncio.run(test_service())
    print()

    print("\n" + "=" * 50)
    print("All tests completed!")
