"""Desktop launcher: one process, one fixed port, one browser tab.

A frozen build starts from a shortcut, so the working directory is not the
repository and nothing may be assumed about it. Everything that a user has to
know when startup fails is printed to the console window *and* written to a
rotating log inside the data directory, because that console disappears the
moment the window is closed.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import uvicorn

from .main import create_app
from .resources import default_data_dir, frontend_dist, log_dir

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}/#chat"
HEALTH_URL = f"http://{HOST}:{PORT}/api/v1/healthz"
LOG_NAME = "app.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3
PROBE_TIMEOUT = 0.6

logger = logging.getLogger("wot.desktop")


def setup_logging(directory: Path) -> Path:
    """Log to ``<data>/logs/app.log`` and to the console, without duplicating handlers."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / LOG_NAME
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        if getattr(handler, "_wot_owned", False):
            root.removeHandler(handler)
            handler.close()
    file_handler = RotatingFileHandler(
        target, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler._wot_owned = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream._wot_owned = True  # type: ignore[attr-defined]
        root.addHandler(stream)
    return target


def app_answers(timeout: float = PROBE_TIMEOUT) -> bool:
    """True when a WeOnceTalked server already owns the port."""
    try:
        response = httpx.get(HEALTH_URL, timeout=timeout, trust_env=False)
    except (httpx.HTTPError, OSError):
        return False
    if response.status_code != 200:
        return False
    try:
        return response.json().get("status") == "ok"
    except ValueError:
        return False


def port_is_free() -> bool:
    """Bind without ``SO_REUSEADDR``: on Windows that flag would hide a busy port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((HOST, PORT))
        except OSError:
            return False
    return True


def browser_allowed() -> bool:
    return os.getenv("WOT_NO_BROWSER", "").strip().lower() not in {"1", "true", "yes", "on"}


def open_browser_when_ready(stop: threading.Event, timeout: float = 30.0) -> None:
    """Wait for the lifespan to finish, then hand the URL to the default browser."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not stop.is_set():
        if app_answers():
            webbrowser.open(URL)
            return
        stop.wait(0.25)
    logger.warning("服务未在 %s 秒内就绪，请手动打开 %s", int(timeout), URL)


def run() -> int:
    directory = default_data_dir()
    try:
        log_path = setup_logging(log_dir(directory))
    except OSError as exc:
        print(f"无法写入数据目录 {directory}：{exc}", flush=True)
        return 4
    logger.info("数据目录 %s", directory)
    logger.info("日志文件 %s", log_path)
    if frontend_dist() is None:
        logger.warning("未找到前端构建产物 frontend/dist，只能访问接口，请先执行 npm run build")

    if app_answers():
        logger.info("检测到已在运行的实例，打开 %s", URL)
        print(f"我们曾聊过 已在运行：{URL}", flush=True)
        if browser_allowed():
            webbrowser.open(URL)
        return 0

    if not port_is_free():
        message = (
            f"端口 {PORT} 已被其他程序占用（不是本应用）。\n"
            f"请关闭占用该端口的程序后重新启动，或排查：netstat -ano | findstr :{PORT}"
        )
        logger.error(message)
        print(message, flush=True)
        return 2

    try:
        app = create_app(directory)
    except Exception:
        logger.exception("初始化失败")
        print("初始化失败，详见日志文件。", flush=True)
        return 5

    stop = threading.Event()
    if browser_allowed():
        threading.Thread(
            target=open_browser_when_ready, args=(stop,), name="wot-open-browser", daemon=True
        ).start()
    else:
        logger.info("WOT_NO_BROWSER 已设置，不自动打开浏览器")

    print(f"我们曾聊过 正在启动：{URL}", flush=True)
    print(f"数据目录：{directory}", flush=True)
    print("关闭此窗口即停止服务。", flush=True)
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_config=None, access_log=False)
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出")
    except SystemExit as exc:
        logger.error("服务未能启动（退出码 %s），可能是端口被占用", exc.code)
        print(f"服务未能启动（退出码 {exc.code}），详见日志文件。", flush=True)
        return 3
    except Exception:
        logger.exception("服务异常退出")
        print("服务异常退出，详见日志文件。", flush=True)
        return 1
    finally:
        stop.set()
    logger.info("服务已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
