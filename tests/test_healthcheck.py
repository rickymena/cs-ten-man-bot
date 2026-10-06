import json

import pytest

import healthcheck

NOW = 1_800_000_000.0


@pytest.fixture
def beat(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setenv("HEARTBEAT_PATH", str(path))

    def write(**fields):
        payload = {"last_tick_unix": NOW - 20, "gateway_ready": True,
                   "queue": {"backlog": 0, "current_job_started_unix": None}}
        payload.update(fields)
        path.write_text(json.dumps(payload))
    return write


def test_healthy(beat):
    beat()
    assert healthcheck.check(NOW)[0]


def test_missing_heartbeat_is_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setenv("HEARTBEAT_PATH", str(tmp_path / "nope.json"))
    assert not healthcheck.check(NOW)[0]


def test_stalled_loop_is_unhealthy(beat):
    beat(last_tick_unix=NOW - 600)
    healthy, reason = healthcheck.check(NOW)
    assert not healthy and "stalled" in reason


def test_short_disconnect_is_fine_long_one_is_not(beat):
    beat(gateway_ready=False, gateway_down_since_unix=NOW - 60)
    assert healthcheck.check(NOW)[0]
    beat(gateway_ready=False, gateway_down_since_unix=NOW - 3600)
    assert not healthcheck.check(NOW)[0]


def test_stuck_database_job_is_unhealthy(beat):
    beat(queue={"backlog": 40, "current_job_started_unix": NOW - 300})
    healthy, reason = healthcheck.check(NOW)
    assert not healthy and "stuck" in reason
