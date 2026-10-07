import configparser
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from modules.db_migrations import MigrationRunner
from modules.models import MeshMessage
from modules.one_byte_deny import (
    ACTION_DENY,
    ACTION_NORMAL,
    ACTION_SUPPRESS,
    DEFAULT_SENDER_COOLDOWN_MINUTES,
    OneByteDenyTracker,
    is_one_byte_path,
    load_settings,
)


class FileDB:
    def __init__(self, path: str):
        self.path = path

    def execute_query(self, query, params=()):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    class _Connection:
        def __init__(self, path: str):
            self.path = path

        def __enter__(self):
            self.conn = sqlite3.connect(self.path)
            return self.conn

        def __exit__(self, *_):
            self.conn.close()

    def connection(self):
        return self._Connection(self.path)


def config(**values):
    result = configparser.ConfigParser()
    result.add_section("Bot")
    for key, value in values.items():
        result.set("Bot", key, str(value))
    return result


def bot(config_parser, db_manager=None):
    return SimpleNamespace(
        config=config_parser,
        db_manager=db_manager,
        logger=logging.getLogger("test_one_byte_deny"),
    )


def message(sender="Alice", *, path_byte_length=1, packet_hash="ABCDEF0123456789", **kwargs):
    routing_info = kwargs.pop("routing_info", {"path_byte_length": path_byte_length})
    if packet_hash is not None:
        routing_info["packet_hash"] = packet_hash
    return MeshMessage(
        "ping",
        sender_id=sender,
        sender_pubkey=kwargs.pop("sender_pubkey", None),
        routing_info=routing_info,
        **kwargs,
    )


def make_tracker(tmp_path, **settings):
    db_path = tmp_path / "deny.db"
    with sqlite3.connect(db_path) as conn:
        MigrationRunner(conn, logging.getLogger("test_migrations")).run()
    db = FileDB(str(db_path))
    values = {
        "deny_1_byte": "true",
        "deny_hash_template": "Denied {packet_hash} to {sender}",
        "deny_1_byte_sender_cooldown_minutes": 60,
        "deny_1_byte_channel_cooldown_minutes": 5,
        **settings,
    }
    parser = config(**values)
    return OneByteDenyTracker(bot(parser, db)), db


def test_classifier_requires_explicit_integer_path_length():
    assert is_one_byte_path(message())
    assert not is_one_byte_path(message(path_byte_length=2))
    assert not is_one_byte_path(message(routing_info={"path_length": 1}))
    assert not is_one_byte_path(message(routing_info={"path_byte_length": "1"}))
    assert not is_one_byte_path(message(routing_info={"path_byte_length": True}))


def test_disabled_and_synthetic_messages_are_normal(tmp_path):
    disabled = OneByteDenyTracker(bot(config(deny_1_byte="false")))
    assert disabled.decide(message(), "ping").action == ACTION_NORMAL

    tracker, _ = make_tracker(tmp_path)
    synthetic = message(capture_sink=[])
    assert tracker.decide(synthetic, "ping").action == ACTION_NORMAL


def test_template_uses_packet_hash_and_existing_placeholders(tmp_path):
    tracker, _ = make_tracker(tmp_path)
    decision = tracker.decide(message(sender="Alice"), "ping")
    assert decision.action == ACTION_DENY
    assert decision.response == "Denied ABCDEF0123456789 to Alice"


def test_missing_packet_hash_renders_empty(tmp_path):
    tracker, _ = make_tracker(tmp_path)
    decision = tracker.decide(message(packet_hash=None), "ping")
    assert decision.response == "Denied  to Alice"


def test_sender_and_mesh_cooldowns_are_shared(tmp_path):
    tracker, _ = make_tracker(tmp_path)
    assert tracker.decide(message("Alice"), "ping").action == ACTION_DENY
    assert tracker.decide(message("Alice"), "help").action == ACTION_SUPPRESS
    assert tracker.decide(message("Bob"), "ping").action == ACTION_SUPPRESS


def test_zero_cooldowns_allow_expired_attempts(tmp_path):
    tracker, _ = make_tracker(
        tmp_path,
        deny_1_byte_sender_cooldown_minutes=0,
        deny_1_byte_channel_cooldown_minutes=0,
    )
    assert tracker.decide(message("Alice"), "ping").action == ACTION_DENY
    assert tracker.decide(message("Alice"), "ping").action == ACTION_DENY


def test_malformed_and_nonfinite_cooldowns_use_defaults():
    settings = load_settings(
        config(
            deny_1_byte_sender_cooldown_minutes="-1",
            deny_1_byte_channel_cooldown_minutes="nan",
        )
    )
    assert settings.sender_cooldown_minutes == DEFAULT_SENDER_COOLDOWN_MINUTES
    assert settings.mesh_cooldown_minutes == 5.0


def test_persistence_survives_tracker_instances(tmp_path):
    first, db = make_tracker(tmp_path)
    assert first.decide(message("Alice"), "ping").action == ACTION_DENY

    second = OneByteDenyTracker(
        bot(
            config(
                deny_1_byte="true",
                deny_1_byte_sender_cooldown_minutes=60,
                deny_1_byte_channel_cooldown_minutes=0,
            ),
            db,
        )
    )
    assert second.decide(message("Alice"), "ping").action == ACTION_SUPPRESS


def test_failed_database_write_still_consumes_cooldown(tmp_path):
    parser = config(
        deny_1_byte="true",
        deny_1_byte_sender_cooldown_minutes=60,
        deny_1_byte_channel_cooldown_minutes=0,
    )
    failing_db = SimpleNamespace(
        execute_query=lambda *args: [],
        connection=lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    tracker = OneByteDenyTracker(bot(parser, failing_db))
    assert tracker.decide(message("Alice"), "ping").action == ACTION_DENY
    assert tracker.decide(message("Alice"), "ping").action == ACTION_SUPPRESS


def test_concurrent_reservations_only_allow_one(tmp_path):
    tracker, _ = make_tracker(
        tmp_path,
        deny_1_byte_sender_cooldown_minutes=60,
        deny_1_byte_channel_cooldown_minutes=0,
    )

    def decide(index):
        return tracker.decide(message(f"sender-{index}"), "ping").action

    with ThreadPoolExecutor(max_workers=8) as pool:
        actions = list(pool.map(decide, range(8)))
    assert actions.count(ACTION_DENY) == 1
    assert actions.count(ACTION_SUPPRESS) == 7


def test_cached_decision_prevents_duplicate_reservation(tmp_path):
    tracker, db = make_tracker(tmp_path)
    msg = message()
    assert tracker.decide(msg, "ping").action == ACTION_DENY
    assert tracker.decide(msg, "ping").action == ACTION_DENY
    assert db.execute_query("SELECT COUNT(*) AS n FROM one_byte_deny_events")[0]["n"] == 1
