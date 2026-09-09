#!/usr/bin/env python3
"""Test script for VCSO Alert Service."""

import sys
import os

# Add parent directory to path to import the service
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from local.service_plugins.vcso_alert_service import VCSOTableParser, VCSOAlertService
import asyncio
from unittest.mock import Mock
import configparser


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


async def test_live_scrape():
    """Test live scraping from VCSO website."""
    print("\nAttempting live scrape from VCSO website...")

    mock_bot = Mock()
    config = configparser.ConfigParser()
    config.add_section('VCSO_Alert_Service')
    config.set('VCSO_Alert_Service', 'enabled', 'true')
    config.set('VCSO_Alert_Service', 'label', 'VCSO')
    config.set('VCSO_Alert_Service', 'min_priority', '3')

    mock_bot.config = config

    service = VCSOAlertService(mock_bot)

    try:
        incidents = service._scrape_incidents()
        print(f"✓ Successfully scraped {len(incidents)} incidents")

        if incidents:
            print("\nFirst incident:")
            inc = incidents[0]
            for key, value in inc.items():
                print(f"  {key}: {value}")
    except Exception as e:
        print(f"✗ Live scrape failed: {e}")
        print("  (This is expected if the website is unreachable or has changed)")


if __name__ == '__main__':
    print("Testing VCSO Alert Service\n")
    print("=" * 50)

    test_parser()
    print()

    asyncio.run(test_service())
    print()

    asyncio.run(test_live_scrape())

    print("\n" + "=" * 50)
    print("All tests completed!")
