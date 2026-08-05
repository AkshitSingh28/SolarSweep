"""Dashboard and JSON API.

Differences from v1's dashboard, which could not have run:

* Its template (``index.html``) did not exist, so ``/`` raised
  ``TemplateNotFound`` on the first request.
* It depended on flask-socketio + eventlet, which is a monkey-patching
  minefield on modern Python and slow to install on a Pi Zero. Live updates
  here are server-sent events over plain Flask — no extra dependency, and it
  reconnects on its own.
* ``/api/stop`` called ``emergency_stop`` with no authentication and no way to
  clear the resulting state.

Mutating endpoints require ``SOLARSWEEP_WEB_TOKEN`` whenever the server is
bound to anything other than loopback; the config layer refuses to start
otherwise.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from ..config import Settings
from ..control import CycleRefused, SolarSweepRobot
from ..telemetry import RunHistory

logger = logging.getLogger(__name__)


class RobotService:
    """Owns the "is a cycle running?" question so the API cannot start two."""

    def __init__(self, robot: SolarSweepRobot) -> None:
        self.robot = robot
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, mode: str) -> tuple[bool, str]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, "a cycle is already running"
            thread = threading.Thread(
                target=self._run, args=(mode,), name="solarsweep-cycle", daemon=True
            )
            self._thread = thread
        thread.start()
        return True, f"started {mode}"

    def _run(self, mode: str) -> None:
        try:
            self.robot.run_cycle(mode)
        except CycleRefused as exc:
            logger.info("cycle refused: %s", exc)
        except Exception:  # pragma: no cover
            logger.exception("cycle crashed")


def create_app(robot: SolarSweepRobot, settings: Settings) -> Flask:
    app = Flask(__name__, template_folder="templates")
    service = RobotService(robot)
    token = settings.web.auth_token

    def require_token(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not token:
                return view(*args, **kwargs)
            supplied = request.headers.get("X-Auth-Token", "")
            # Constant-time compare: this endpoint can move a machine.
            if not hmac.compare_digest(supplied, token):
                return jsonify({"ok": False, "error": "bad or missing token"}), 401
            return view(*args, **kwargs)

        return wrapper

    def snapshot() -> dict:
        return {
            **robot.status,
            "busy": service.busy,
            "auth_required": bool(token),
        }

    @app.route("/")
    def index():
        return render_template("index.html", name=settings.name)

    @app.get("/api/status")
    def api_status():
        return jsonify(snapshot())

    @app.get("/api/runs")
    def api_runs():
        history = RunHistory(Path(settings.logging.telemetry_dir), limit=25).load()
        return jsonify(history)

    @app.get("/api/stream")
    def api_stream():
        """Server-sent events. One status object per second."""

        def generate():
            while True:
                yield f"data: {json.dumps(snapshot())}\n\n"
                time.sleep(1.0)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/start")
    @require_token
    def api_start():
        mode = (request.get_json(silent=True) or {}).get("mode", "full_cycle")
        if mode not in ("full_cycle", "quick_pass"):
            return jsonify({"ok": False, "error": f"unknown mode {mode!r}"}), 400
        ok, message = service.start(mode)
        return jsonify({"ok": ok, "message": message}), (200 if ok else 409)

    @app.post("/api/pause")
    @require_token
    def api_pause():
        robot.pause()
        return jsonify({"ok": True, "state": robot.machine.state.value})

    @app.post("/api/resume")
    @require_token
    def api_resume():
        robot.resume()
        return jsonify({"ok": True, "state": robot.machine.state.value})

    @app.post("/api/estop")
    @require_token
    def api_estop():
        robot.estop("dashboard")
        return jsonify({"ok": True, "state": robot.machine.state.value})

    @app.post("/api/clear")
    @require_token
    def api_clear():
        cleared = robot.clear_fault()
        return jsonify({
            "ok": cleared,
            "state": robot.machine.state.value,
            "error": None if cleared else "the e-stop input is still asserted",
        })

    return app


def serve(robot: SolarSweepRobot, settings: Settings, with_scheduler: bool = True) -> None:
    from ..scheduling import CleaningScheduler

    scheduler = CleaningScheduler(robot, settings)
    if with_scheduler:
        scheduler.start()

    app = create_app(robot, settings)
    logger.info(
        "dashboard on http://%s:%d  (auth %s)",
        settings.web.host, settings.web.port,
        "required" if settings.web.auth_token else "not required — loopback only",
    )
    try:
        # threaded=True so the SSE stream does not block the control endpoints.
        app.run(
            host=settings.web.host,
            port=settings.web.port,
            debug=False,
            threaded=True,
            use_reloader=False,
        )
    finally:
        scheduler.shutdown()
