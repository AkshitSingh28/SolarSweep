"""Telemetry, the dashboard API, and the CLI."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from solarsweep.cli import main
from solarsweep.telemetry import RunHistory, RunRecorder
from solarsweep.web.app import create_app

# -- Telemetry -------------------------------------------------------------


def test_a_run_leaves_a_readable_jsonl_trace(robot, settings):
    robot.run_cycle("quick_pass")
    files = list(Path(settings.logging.telemetry_dir).glob("run-*.jsonl"))
    assert len(files) == 1

    records = [json.loads(line) for line in files[0].read_text().splitlines()]
    kinds = [r["kind"] for r in records]
    assert kinds[0] == "run_start"
    assert kinds[-1] == "run_end"
    assert "pass_complete" in kinds
    assert "referenced" in kinds
    # Every record is timestamped relative to the start of the run.
    assert all(isinstance(r["t"], (int, float)) for r in records)


def test_the_trace_records_why_a_run_stopped(settings):
    from conftest import make_robot

    bot, _ = make_robot(settings, fault="obstacle@500")
    try:
        bot.run_cycle("full_cycle")
    finally:
        bot.monitor.stop()

    path = next(Path(settings.logging.telemetry_dir).glob("run-*.jsonl"))
    end = json.loads(path.read_text().splitlines()[-1])
    assert end["outcome"] == "aborted"
    assert end["stop_reason"] == "obstacle"
    assert "obstacle" in end["detail"]


def test_history_flags_a_run_that_died_mid_cycle(tmp_path):
    """No run_end line means the process was killed. Silence would be worse
    than an ugly entry."""
    (tmp_path / "run-20260101T000000Z.jsonl").write_text(
        '{"t":0,"kind":"run_start","run":"x"}\n'
    )
    entries = RunHistory(tmp_path).load()
    assert entries[0]["outcome"] == "interrupted"


def test_history_is_empty_when_nothing_has_run(tmp_path):
    assert RunHistory(tmp_path / "nothing").load() == []


def test_a_write_racing_finish_does_not_kill_the_cycle(tmp_path):
    """An e-stop from the dashboard thread runs finish() and closes the handle.
    If the control thread is between event()'s guard and its write when that
    happens, it must drop the line rather than raise into the cycle.

    This reproduced as a flaky AttributeError under CI's timing before the
    guard moved inside the lock, so the interleaving is forced here rather
    than raced for.
    """
    recorder = RunRecorder(tmp_path, mode="full_cycle")
    real_lock = recorder._lock

    class LosesTheRace:
        """Closes the handle exactly where the other thread's finish() would."""

        def __enter__(self):
            real_lock.acquire()
            recorder._fh.close()
            recorder._fh = None
            return self

        def __exit__(self, *exc):
            real_lock.release()
            return False

    recorder._lock = LosesTheRace()
    recorder.event("tick", position_mm=900)  # must not raise

    recorder._lock = real_lock
    assert recorder.summary.events == 1


def test_telemetry_failure_does_not_stop_the_robot(tmp_path, robot, settings):
    """A read-only SD card must not prevent a cycle."""
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a directory")
    recorder = RunRecorder(blocked, "quick_pass")
    recorder.event("test", value=1)  # must not raise
    summary = recorder.finish("completed")
    assert summary.outcome == "completed"


def test_enum_and_dataclass_values_survive_serialisation(tmp_path):
    from solarsweep.safety import StopReason

    recorder = RunRecorder(tmp_path, "full_cycle")
    recorder.event("thing", reason=StopReason.STALL, nested={"a": [1, 2]})
    recorder.finish("aborted", stop_reason="stall")
    line = json.loads((tmp_path / f"run-{recorder.run_id}.jsonl").read_text()
                      .splitlines()[0])
    assert line["reason"] == "stall"
    assert line["nested"] == {"a": [1, 2]}


# -- Dashboard API ---------------------------------------------------------


@pytest.fixture
def client(robot, settings):
    app = create_app(robot, settings)
    app.config["TESTING"] = True
    return app.test_client()


def test_index_renders(client):
    """v1's dashboard called render_template('index.html') for a template that
    was never committed — the first request would have 500'd."""
    response = client.get("/")
    assert response.status_code == 200
    assert b"SolarSweep" in response.data


def test_status_endpoint_returns_the_full_snapshot(client):
    data = client.get("/api/status").get_json()
    for key in ("state", "position_mm", "inputs", "estop", "stall_protection"):
        assert key in data


def test_start_and_estop(client, robot):
    assert client.post("/api/start", json={"mode": "quick_pass"}).get_json()["ok"]
    assert client.post("/api/estop").get_json()["ok"]
    assert robot.latch.tripped


def test_starting_twice_is_a_conflict(client):
    client.post("/api/start", json={"mode": "full_cycle"})
    second = client.post("/api/start", json={"mode": "full_cycle"})
    assert second.status_code == 409


def test_unknown_mode_is_a_bad_request(client):
    assert client.post("/api/start", json={"mode": "nonsense"}).status_code == 400


def test_clear_reports_why_it_refused(client, robot, board):
    board.press_estop()
    robot.estop("test")
    body = client.post("/api/clear").get_json()
    assert body["ok"] is False
    assert "still asserted" in body["error"]


def test_mutating_endpoints_require_the_token_when_one_is_set(robot, settings):
    secured = replace(settings, web=replace(settings.web, auth_token="letmein"))
    app = create_app(robot, secured)
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.get("/api/status").status_code == 200  # reading is fine
    assert client.post("/api/estop").status_code == 401
    assert client.post("/api/estop", headers={"X-Auth-Token": "wrong"}).status_code == 401
    assert client.post("/api/estop", headers={"X-Auth-Token": "letmein"}).status_code == 200


def test_runs_endpoint_lists_history(client, robot):
    robot.run_cycle("quick_pass")
    runs = client.get("/api/runs").get_json()
    assert runs and runs[0]["outcome"] == "completed"


# -- CLI -------------------------------------------------------------------


def test_cli_run_returns_zero_on_a_clean_cycle(tmp_path, capsys):
    code = main(["-c", "config/settings.yaml", "--sim", "--time-scale", "0",
                 "run", "--mode", "quick_pass"])
    assert code == 0
    assert "COMPLETED" in capsys.readouterr().out


def test_cli_run_returns_nonzero_when_a_fault_stops_it(capsys):
    code = main(["-c", "config/settings.yaml", "--sim", "--time-scale", "0",
                 "run", "--fault", "obstacle@400"])
    assert code == 1
    assert "obstacle" in capsys.readouterr().out


def test_cli_rejects_fault_injection_without_sim(capsys):
    code = main(["-c", "config/settings.yaml", "run", "--fault", "rain"])
    assert code == 2
    assert "only applies with --sim" in capsys.readouterr().err


def test_cli_rejects_an_unknown_fault_name(capsys):
    with pytest.raises(ValueError, match="unknown fault"):
        main(["-c", "config/settings.yaml", "--sim", "run", "--fault", "banana"])


def test_cli_status_prints_json(capsys):
    assert main(["-c", "config/settings.yaml", "--sim", "status"]) == 0
    json.loads(capsys.readouterr().out)


def test_cli_selftest_passes_in_simulation(capsys):
    code = main(["-c", "config/settings.yaml", "--sim", "--time-scale", "0",
                 "selftest", "--motion", "--yes"])
    out = capsys.readouterr().out
    assert "All checks passed" in out
    assert code == 0


def test_cli_selftest_skips_motion_without_confirmation(capsys):
    main(["-c", "config/settings.yaml", "--sim", "selftest", "--motion"])
    assert "pass --yes to confirm" in capsys.readouterr().out


def test_cli_calibrate_prints_a_configurable_number(capsys):
    code = main(["-c", "config/settings.yaml", "--sim", "--time-scale", "0",
                 "calibrate", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    assert "duty_to_mmps:" in out


def test_cli_calibrate_refuses_on_hardware_without_confirmation(capsys):
    code = main(["-c", "config/settings.yaml", "calibrate"])
    assert code == 2
    assert "Clear the rail" in capsys.readouterr().out


def test_cli_reports_a_bad_config_instead_of_traceback(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("cleaning:\n  passes: 0\n")
    assert main(["-c", str(bad), "--sim", "status"]) == 2
    assert "configuration error" in capsys.readouterr().err
