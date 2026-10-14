"""Process-local metrics, independent of HTTP and storage implementations."""

from collections import Counter, defaultdict
from collections.abc import Callable
from threading import Lock
from time import monotonic


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _metric_line(name: str, labels: dict[str, str], value: float | int) -> str:
    suffix = ",".join(f'{key}="{_label(text)}"' for key, text in labels.items())
    return f"{name}{{{suffix}}} {value}" if labels else f"{name} {value}"


class RequestMetrics:
    """Copy counters under a lock before rendering in an HTTP worker thread.

    Callers supply matched route templates, never raw paths or user identifiers.
    Uptime uses a monotonic clock so wall-clock corrections cannot make it negative.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._lock = Lock()
        self._requests: Counter[tuple[str, str]] = Counter()
        self._counts: Counter[str] = Counter()
        self._durations: dict[str, float] = defaultdict(float)

    def record(self, route: str, status: int, elapsed: float) -> None:
        with self._lock:
            self._requests[(route, str(status))] += 1
            self._counts[route] += 1
            self._durations[route] += elapsed

    def render(self, *, ready: bool, database_ready: bool, mode: str) -> str:
        with self._lock:
            requests = self._requests.copy()
            counts = self._counts.copy()
            durations = self._durations.copy()
        lines = [
            "# HELP dive_http_requests_total HTTP responses by route template and status.",
            "# TYPE dive_http_requests_total counter",
        ]
        for (path, status), total in sorted(requests.items()):
            lines.append(
                _metric_line(
                    "dive_http_requests_total", {"path": path, "status": status}, total
                )
            )
        lines += [
            "# HELP dive_http_request_duration_seconds Handling duration by route template.",
            "# TYPE dive_http_request_duration_seconds summary",
        ]
        for path, duration in sorted(durations.items()):
            lines.extend(
                (
                    _metric_line(
                        "dive_http_request_duration_seconds_sum",
                        {"path": path},
                        round(duration, 6),
                    ),
                    _metric_line(
                        "dive_http_request_duration_seconds_count",
                        {"path": path},
                        counts[path],
                    ),
                )
            )
        lines += [
            "# HELP dive_readiness Component readiness (1 = ready).",
            "# TYPE dive_readiness gauge",
            _metric_line("dive_readiness", {"component": "overall"}, int(ready)),
            _metric_line(
                "dive_readiness", {"component": "database"}, int(database_ready)
            ),
            "# HELP dive_process_uptime_seconds Seconds since this process started serving.",
            "# TYPE dive_process_uptime_seconds gauge",
            _metric_line(
                "dive_process_uptime_seconds",
                {},
                round(self._clock() - self._started, 3),
            ),
            "# HELP dive_build_info Build and composition metadata.",
            "# TYPE dive_build_info gauge",
            _metric_line("dive_build_info", {"version": "0.4.0", "mode": mode}, 1),
        ]
        return "\n".join(lines) + "\n"
