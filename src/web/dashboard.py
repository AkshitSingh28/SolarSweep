"""
Web dashboard for Solar Cleaner Robot.
Provides real-time status, manual controls, and cleaning history.
Built with Flask + Socket.IO for live updates.
"""

import threading
import logging
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit

logger = logging.getLogger(__name__)


class Dashboard:
    def __init__(self, robot, settings, port: int = 5000):
        self.robot = robot
        self.settings = settings
        self.port = port
        self.app = Flask(__name__, template_folder="templates")
        self.app.config["SECRET_KEY"] = "solar-cleaner-secret"
        self.socketio = SocketIO(self.app, cors_allowed_origins="*")
        self._register_routes()
        self._register_socketio()

    def _register_routes(self):
        app = self.app

        @app.route("/")
        def index():
            return render_template("index.html")

        @app.route("/api/status")
        def status():
            return jsonify(self.robot.status)

        @app.route("/api/start", methods=["POST"])
        def start():
            mode = request.json.get("mode", "full_cycle")
            t = threading.Thread(target=self._run_mode, args=(mode,), daemon=True)
            t.start()
            return jsonify({"ok": True, "mode": mode})

        @app.route("/api/stop", methods=["POST"])
        def stop():
            self.robot.emergency_stop()
            return jsonify({"ok": True})

        @app.route("/api/pause", methods=["POST"])
        def pause():
            self.robot.pause()
            return jsonify({"ok": True})

        @app.route("/api/resume", methods=["POST"])
        def resume():
            self.robot.resume()
            return jsonify({"ok": True})

        @app.route("/api/settings", methods=["GET"])
        def get_settings():
            return jsonify({
                "panel_length_mm": self.settings.robot.panel_length_mm,
                "cleaning_passes": self.settings.cleaning.passes,
                "brush_speed_pct": self.settings.cleaning.brush_speed_pct,
                "rain_disable": self.settings.rain_disable,
                "dust_threshold": self.settings.dust_threshold,
            })

    def _register_socketio(self):
        @self.socketio.on("connect")
        def on_connect():
            logger.info("Web client connected.")
            emit("status", self.robot.status)

        @self.socketio.on("request_status")
        def on_status_request():
            emit("status", self.robot.status)

    def _run_mode(self, mode: str):
        try:
            if mode == "full_cycle":
                self.robot.run_full_cycle()
            elif mode == "quick_pass":
                self.robot.run_quick_pass()
        except Exception as e:
            logger.error(f"Error in web-triggered run: {e}")

    def run(self):
        logger.info(f"Dashboard running at http://{self.settings.web_host}:{self.port}")
        self.socketio.run(
            self.app,
            host=self.settings.web_host,
            port=self.port,
            debug=False,
        )
