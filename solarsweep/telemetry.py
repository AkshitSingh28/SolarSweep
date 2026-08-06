"""Per-run telemetry, one JSON object per line.

The prototype "could not be tested", and part of why testing hardware is hard
is that when something goes wrong on a roof you get one sentence from whoever
was watching. A cycle here leaves a machine-readable trace: what the sensors
said, when each pass started, how far the carriage actually got, and the exact
interlock that stopped it.

Format is JSONL so it can be tailed live, appended to safely, and read with
``jq`` or three lines of pandas. Files are per-run, named for the start time.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "value"):  # Enum
        return value.value
    return str(value)


@dataclass
class RunSummary:
    run_id: str
    mode: str
    started_at: str
    ended_at: str | None = None
    duration_s: float = 0.0
    passes_planned: int = 0
    passes_completed: int = 0
    distance_mm: float = 0.0
    water_pulses: int = 0
    dust_before: int | None = None
    dust_after: int | None = None
    outcome: str = "running"  # completed | aborted | faulted | estopped
    stop_reason: str | None = None
    detail: str = ""
    events: int = 0


class RunRecorder:
    """Writes one run's events, then a summary line.

    Thread-safe: the web dashboard reads status from another thread while the
    control loop writes.
    """

    def __init__(self, directory: str | os.PathLike[str], mode: str,
                 enabled: bool = True) -> None:
        self._enabled = enabled
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        started = datetime.now(timezone.utc)
        self.run_id = started.strftime("%Y%m%dT%H%M%SZ")
        self.summary = RunSummary(
            run_id=self.run_id, mode=mode, started_at=started.isoformat()
        )
        self._path: Path | None = None
        self._fh = None

        if not enabled:
            return
        try:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            self._path = directory / f"run-{self.run_id}.jsonl"
            self._fh = self._path.open("a", encoding="utf-8")
        except OSError as exc:
            # Losing telemetry must never stop the machine working.
            logger.warning("telemetry disabled: cannot write to %s (%s)", directory, exc)
            self._enabled = False

    @property
    def path(self) -> Path | None:
        return self._path

    def event(self, kind: str, **fields: Any) -> None:
        record = {
            "t": round(time.monotonic() - self._t0, 3),
            "run": self.run_id,
            "kind": kind,
            **{k: _jsonable(v) for k, v in fields.items()},
        }
        self.summary.events += 1
        if not self._enabled or self._fh is None:
            logger.debug("telemetry(%s): %s", kind, fields)
            return
        line = json.dumps(record, separators=(",", ":"))
        with self._lock:
            # Re-read under the lock. The check above is only a fast path: an
            # e-stop from the dashboard thread can run finish() and close the
            # handle while this thread is queued here, and a stale local would
            # then write to None. Losing telemetry must never stop the machine.
            fh = self._fh
            if fh is None:
                logger.debug("telemetry(%s) dropped: run already finished", kind)
                return
            try:
                fh.write(line + "\n")
                fh.flush()
            except OSError as exc:  # pragma: no cover
                logger.warning("telemetry write failed: %s", exc)

    def finish(self, outcome: str, stop_reason: str | None = None,
               detail: str = "") -> RunSummary:
        self.summary.outcome = outcome
        self.summary.stop_reason = stop_reason
        self.summary.detail = detail
        self.summary.ended_at = datetime.now(timezone.utc).isoformat()
        self.summary.duration_s = round(time.monotonic() - self._t0, 2)
        self.event("run_end", **_jsonable(self.summary))
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:  # pragma: no cover
                    pass
                self._fh = None
        return self.summary


@dataclass
class RunHistory:
    """The last N run summaries, for the dashboard and for `solarsweep runs`."""

    directory: Path
    limit: int = 25
    _cache: list = field(default_factory=list)

    def load(self) -> list[dict]:
        if not self.directory.exists():
            return []
        files = sorted(self.directory.glob("run-*.jsonl"), reverse=True)[: self.limit]
        out: list[dict] = []
        for path in files:
            summary = self._summary_of(path)
            if summary is not None:
                out.append(summary)
        return out

    @staticmethod
    def _summary_of(path: Path) -> dict | None:
        """The summary is the last ``run_end`` line; a run killed mid-flight
        will not have one, which is itself worth reporting."""
        last_end = None
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"run_end"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("kind") == "run_end":
                        last_end = record
        except OSError:
            return None
        if last_end is not None:
            return last_end
        return {"run_id": path.stem.removeprefix("run-"), "outcome": "interrupted",
                "detail": "no run_end record — the process died mid-cycle"}
