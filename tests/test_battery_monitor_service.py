"""Tests for the local battery monitor service."""

from configparser import ConfigParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import asyncio
import importlib.util
import sys
import types
from pathlib import Path


if "local_services" not in sys.modules:
    package = types.ModuleType("local_services")
    package.__path__ = [str(Path(__file__).parents[1] / "local" / "service_plugins")]
    sys.modules["local_services"] = package
base_spec = importlib.util.spec_from_file_location(
    "local_services.base_service",
    Path(__file__).parents[1] / "modules" / "service_plugins" / "base_service.py",
)
base_module = importlib.util.module_from_spec(base_spec)
sys.modules["local_services.base_service"] = base_module
assert base_spec.loader is not None
base_spec.loader.exec_module(base_module)
service_spec = importlib.util.spec_from_file_location(
    "local_services.battery_monitor",
    Path(__file__).parents[1] / "local" / "service_plugins" / "battery_monitor.py",
)
service_module = importlib.util.module_from_spec(service_spec)
sys.modules["local_services.battery_monitor"] = service_module
assert service_spec.loader is not None
service_spec.loader.exec_module(service_module)
BatteryMonitorService = service_module.BatteryMonitorService


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def config():
    cfg = ConfigParser()
    cfg.add_section("Battery_Monitor_Service")
    cfg.set(
        "Battery_Monitor_Service",
        "nodes",
        "AA" * 32 + "," + "aa" * 32 + ",bad",
    )
    cfg.set("Battery_Monitor_Service", "check_interval", "60")
    return cfg


def make_bot(config):
    return SimpleNamespace(
        config=config,
        logger=Mock(),
        connected=True,
        meshcore=None,
        db_manager=SimpleNamespace(store_battery_observation=Mock()),
    )


def test_loads_and_deduplicates_full_public_keys(config):
    service = BatteryMonitorService(make_bot(config))
    assert service.nodes == ["aa" * 32]
    assert service.check_interval == 60


def test_checks_remote_node_and_stores_nullable_percentage(config):
    key = "aa" * 32
    commands = SimpleNamespace(
        req_status_sync=AsyncMock(return_value={"bat": 3725, "nb_recv": 10, "nb_sent": 20}),
        req_telemetry_sync=AsyncMock(return_value=[]),
    )
    meshcore = SimpleNamespace(commands=commands, contacts={key: {"public_key": key}})
    bot = make_bot(config)
    bot.meshcore = meshcore
    service = BatteryMonitorService(bot)

    run(service._check_nodes())

    commands.req_status_sync.assert_awaited_once_with(meshcore.contacts[key], timeout=30.0)
    bot.db_manager.store_battery_observation.assert_called_once()
    params = bot.db_manager.store_battery_observation.call_args.args
    assert params[0] == key
    assert params[2] is None
    assert params[3] == pytest.approx(3.725)
    assert params[4:] == (10, 20, None)


def test_extracts_explicit_percentage(config):
    key = "aa" * 32
    commands = SimpleNamespace(
        req_status_sync=AsyncMock(return_value={"bat": 4000}),
        req_telemetry_sync=AsyncMock(
            return_value=[{"type": "percentage", "value": 81}]
        ),
    )
    bot = make_bot(config)
    bot.meshcore = SimpleNamespace(commands=commands, contacts={key: {"public_key": key}})
    service = BatteryMonitorService(bot)

    run(service._check_nodes())

    params = bot.db_manager.store_battery_observation.call_args.args
    assert params[2] == pytest.approx(81)
    assert params[3] == pytest.approx(4.0)


def test_one_node_failure_does_not_abort_following_nodes(config):
    key = "aa" * 32
    second = "bb" * 32
    config.set("Battery_Monitor_Service", "nodes", f"{key},{second}")
    commands = SimpleNamespace(
        req_status_sync=AsyncMock(
            side_effect=[RuntimeError("timeout"), {"bat": 3800}]
        ),
        req_telemetry_sync=AsyncMock(return_value=[]),
    )
    bot = make_bot(config)
    bot.meshcore = SimpleNamespace(
        commands=commands,
        contacts={key: {"public_key": key}, second: {"public_key": second}},
    )
    service = BatteryMonitorService(bot)

    run(service._check_nodes())

    assert commands.req_status_sync.await_count == 2
    bot.db_manager.store_battery_observation.assert_called_once()


def test_stores_counter_deltas_through_database_manager(config):
    key = "aa" * 32
    commands = SimpleNamespace(
        req_status_sync=AsyncMock(
            side_effect=[
                {"bat": 4000, "nb_recv": 100, "nb_sent": 200},
                {"bat": 4010, "nb_recv": 107, "nb_sent": 203},
            ]
        ),
        req_telemetry_sync=AsyncMock(return_value=[]),
    )
    bot = make_bot(config)
    bot.meshcore = SimpleNamespace(commands=commands, contacts={key: {}})
    service = BatteryMonitorService(bot)

    run(service._check_nodes())
    run(service._check_nodes())

    assert bot.db_manager.store_battery_observation.call_count == 2
    second = bot.db_manager.store_battery_observation.call_args_list[1].args
    assert second[4:] == (107, 203, None)


def test_start_stop_cancels_poll_task(config):
    bot = make_bot(config)
    bot.meshcore = SimpleNamespace(commands=SimpleNamespace(), contacts={})
    service = BatteryMonitorService(bot)
    service._check_nodes = AsyncMock()

    run(service.start())
    assert service.is_running()
    run(service.stop())
    assert not service.is_running()
    assert service._poll_task is None
