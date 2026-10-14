"""Windows desktop launcher; only the owned child process may be stopped.

The GUI uses the standard library. A missing web runtime is installed in a
separate virtual environment. Child stdin is a lifetime pipe: closing the GUI
or losing the parent asks uvicorn to shut down, without a network stop endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
APP_ID = "dive-memory-workbench"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class LaunchError(Exception):
    """A failure that can be shown without exposing environment credentials."""


class InstanceLock:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+b")
        if not path.stat().st_size:
            self.handle.write(b"0")
            self.handle.flush()

    def acquire(self) -> bool:
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def close(self):
        # Closing releases the OS lock, including after abnormal process exit.
        # Keep the file itself to avoid two processes locking different inodes.
        self.handle.close()


def child_environment(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.update(
        PYTHONPATH=str(root / "src"),
        PYTHONUTF8="1",
        PYTHONNOUSERSITE="1",
        DIVE_MODE="evidence_only",
    )
    return env


def read_state(path: Path) -> dict | None:
    try:
        if path.stat().st_size > 8192:
            return None
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return None
        port, identity = state.get("port"), state.get("instance")
        if type(port) is not int or not 1 <= port <= 65535:
            return None
        if not isinstance(identity, str) or len(identity) != 32:
            return None
        int(identity, 16)
        return state
    except (OSError, ValueError, TypeError):
        return None


def local_url(state: dict) -> str:
    # Never open an arbitrary URL obtained from a state file.
    return f"http://127.0.0.1:{state['port']}/"


def running_instance(path: Path, expected: str | None = None) -> dict | None:
    state = read_state(path)
    if not state or (expected and state["instance"] != expected):
        return None
    try:
        # Do not send local requests to an inherited HTTP proxy.
        opener = build_opener(ProxyHandler({}))
        with opener.open(local_url(state) + "launcher/identity", timeout=0.5) as r:
            identity = json.loads(r.read(4096))
        if identity == {"app": APP_ID, "instance": state["instance"], "ready": True}:
            return state
    except (OSError, ValueError, URLError):
        pass
    return None


def remove_state(path: Path, identity: str):
    state = read_state(path)
    if state and state["instance"] == identity:
        path.unlink(missing_ok=True)


def console_python(path: Path) -> Path:
    return path.with_name("python.exe") if path.name.lower() == "pythonw.exe" else path


def runtime_available(python: Path, root: Path) -> bool:
    if not python.is_file():
        return False
    try:
        result = subprocess.run(
            [
                str(python),
                "-c",
                "import sys, fastapi, uvicorn; assert sys.version_info >= (3,11)",
            ],
            cwd=root,
            env=child_environment(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=NO_WINDOW,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Launcher:
    def __init__(
        self,
        root: Path = ROOT,
        *,
        data_dir: Path | None = None,
        db: Path | None = None,
        preferred_port: int = 8765,
    ):
        self.root = root.resolve()
        self.data_dir = data_dir or self.root / ".workbench"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = db or self.root / "memory.db"
        self.state_file = self.data_dir / "running.json"
        self.log_file = self.data_dir / "启动日志.txt"
        self.preferred_port = preferred_port
        self.identity = uuid.uuid4().hex
        self.cancel = threading.Event()
        self.process: subprocess.Popen | None = None
        self.messages: queue.Queue = queue.Queue()

    def status(self, message: str):
        self.messages.put(("status", message))

    def command(self, command: list[str]):
        with self.log_file.open("ab") as log:
            process = subprocess.Popen(
                command,
                cwd=self.root,
                env=child_environment(self.root),
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
            started = time.monotonic()
            while process.poll() is None:
                if self.cancel.wait(0.15) or time.monotonic() - started > 600:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise LaunchError("环境准备已停止。可以稍后重试。")
            if process.returncode:
                raise LaunchError(
                    "运行环境准备失败。请检查网络后重试；详细原因已写入启动日志。"
                )

    def prepare_runtime(self, candidates: list[Path] | None = None) -> Path:
        self.status("正在检查运行环境…")
        managed = self.root / ".venv-workbench"
        relative = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        if candidates is None:
            candidates = [
                managed / relative,
                self.root / ".venv" / relative,
                console_python(Path(sys.executable)),
            ]
        for python in candidates:
            if self.cancel.is_set():
                raise LaunchError("启动已取消。")
            if runtime_available(python, self.root):
                return python
        self.status("首次使用：正在准备独立运行环境，请稍候…")
        self.command(
            [str(console_python(Path(sys.executable))), "-m", "venv", str(managed)]
        )
        python = managed / relative
        self.status("首次使用：正在下载运行依赖，需要网络；下次无需重复安装。")
        self.command(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--retries",
                "1",
                "--timeout",
                "20",
                "-r",
                str(self.root / "requirements/runtime.lock"),
            ]
        )
        if not runtime_available(python, self.root):
            raise LaunchError("运行环境未就绪，请查看启动日志后重试。")
        return python

    def start(self, *, timeout: float = 40) -> dict:
        if self.process and self.process.poll() is None:
            existing = running_instance(self.state_file, self.identity)
            if existing:
                return existing
            raise LaunchError("服务仍在运行，请先停止后重试。")
        python = self.prepare_runtime()
        if self.cancel.is_set():
            raise LaunchError("启动已取消。")
        self.identity = uuid.uuid4().hex
        self.status("正在启动本机工作台…")
        with self.log_file.open("ab") as log:
            log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 ---\n".encode())
            log.flush()
            self.process = subprocess.Popen(
                [
                    str(python),
                    str(Path(__file__).resolve()),
                    "--serve",
                    "--state",
                    str(self.state_file),
                    "--db",
                    str(self.db),
                    "--instance",
                    self.identity,
                    "--port",
                    str(self.preferred_port),
                ],
                cwd=self.root,
                env=child_environment(self.root),
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
                creationflags=NO_WINDOW,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                self.stop()
                raise LaunchError("启动已取消。")
            if self.process.poll() is not None:
                remove_state(self.state_file, self.identity)
                raise LaunchError("工作台未能启动。请点击“查看日志”检查原因，再重试。")
            state = running_instance(self.state_file, self.identity)
            if state:
                return state
            self.cancel.wait(0.1)
        self.stop()
        raise LaunchError("等待工作台启动超时。请查看日志后重试。")

    def stop(self):
        self.cancel.set()
        if self.process:
            if self.process.stdin:
                # EOF also reaches the child when the parent exits unexpectedly.
                self.process.stdin.close()
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
        remove_state(self.state_file, self.identity)


def serve(state_file: Path, db: Path, identity: str, port: int) -> int:
    import uvicorn

    from dive_memory.application.settings import application_from_environment
    from dive_memory.web import create_workbench

    os.environ["DIVE_MODE"] = "evidence_only"
    runtime = application_from_environment(str(db))
    app = create_workbench(runtime)

    @app.get("/launcher/identity")
    def identity_response():
        return {
            "app": APP_ID,
            "instance": identity,
            "ready": runtime.readiness()["ready"],
        }

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        selected = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, access_log=False, timeout_graceful_shutdown=8)
        )

        def watch_parent():
            try:
                sys.stdin.buffer.read(1)
            finally:
                server.should_exit = True

        threading.Thread(target=watch_parent, daemon=True).start()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_file.with_suffix(f".{identity}.tmp")
        temporary.write_text(
            json.dumps({"port": selected, "instance": identity}), encoding="utf-8"
        )
        temporary.replace(state_file)
        server.run(sockets=[sock])
        return 0
    finally:
        sock.close()
        runtime.close()
        remove_state(state_file, identity)


def open_local(path: Path):
    os.startfile(str(path))


def show_window(launcher: Launcher):
    import tkinter as tk
    from tkinter import messagebox, ttk

    window = tk.Tk()
    window.title("续研 · 启动工作台")
    window.geometry("600x420")
    window.minsize(560, 400)
    window.configure(bg="#f7f6f1")
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 9))
    style.configure("Primary.TButton", background="#176b5b", foreground="white")
    style.configure("Horizontal.TProgressbar", background="#176b5b")
    body = tk.Frame(window, bg="#f7f6f1", padx=28, pady=24)
    body.pack(fill="both", expand=True)
    tk.Label(
        body,
        text="续研",
        font=("Microsoft YaHei UI", 24, "bold"),
        fg="#183443",
        bg="#f7f6f1",
    ).pack(anchor="w")
    tk.Label(
        body,
        text="把研究接着做下去。",
        font=("Microsoft YaHei UI", 11),
        fg="#587074",
        bg="#f7f6f1",
    ).pack(anchor="w", pady=(3, 22))
    status = tk.StringVar(value="正在准备启动…")
    tk.Label(
        body,
        textvariable=status,
        wraplength=530,
        justify="left",
        anchor="w",
        font=("Microsoft YaHei UI", 11),
        fg="#183443",
        bg="#f7f6f1",
        height=3,
    ).pack(fill="x")
    progress = ttk.Progressbar(body, mode="indeterminate")
    progress.pack(fill="x", pady=(6, 10))
    address = tk.StringVar(value="本地模式 · 无需模型密钥")
    ttk.Entry(
        body,
        textvariable=address,
        state="readonly",
        font=("Microsoft YaHei UI", 10),
    ).pack(fill="x")
    controls = tk.Frame(body, bg="#f7f6f1")
    controls.pack(fill="x", pady=(20, 15))
    active = {"thread": None, "state": None, "closing": False}

    def job(action, result):
        try:
            value = action()
            launcher.messages.put((result, value))
        except (LaunchError, OSError, subprocess.SubprocessError) as exc:
            launcher.messages.put(("error", str(exc)))

    def begin():
        launcher.cancel.clear()
        launch_button.configure(state="disabled")
        stop_button.configure(state="disabled")
        progress.configure(mode="indeterminate", value=0)
        progress.start(12)
        thread = threading.Thread(
            target=job, args=(launcher.start, "ready"), daemon=True
        )
        active["thread"] = thread
        thread.start()

    def open_browser():
        if active["state"]:
            if not running_instance(launcher.state_file, launcher.identity):
                status.set("暂时无法连接工作台，请停止服务后重试。")
            elif not webbrowser.open(local_url(active["state"])):
                status.set("浏览器未自动打开，请复制下方地址到浏览器。")
            else:
                status.set(
                    "工作台已就绪，浏览器已打开。"
                    if not os.getenv("DIVE_WORKBENCH_TOKEN")
                    else "工作台已就绪。请在网页的本地访问设置中填写你的访问令牌。"
                )
        else:
            begin()

    def stop():
        launcher.cancel.set()
        stop_button.configure(state="disabled")
        launch_button.configure(state="disabled")
        status.set("正在停止服务，已保存记录会保留…")
        thread = active["thread"]
        if not active["state"] and thread and thread.is_alive():
            return  # The preparation/start loop observes cancellation.
        active["thread"] = threading.Thread(
            target=job, args=(launcher.stop, "stopped"), daemon=True
        )
        active["thread"].start()

    def close():
        active["closing"] = True
        stop()

    def inspect(path):
        try:
            open_local(path)
        except OSError:
            messagebox.showinfo(
                "续研", f"请在资源管理器中打开：\n{path}", parent=window
            )

    launch_button = ttk.Button(
        controls, text="打开工作台", style="Primary.TButton", command=open_browser
    )
    launch_button.pack(side="left")
    stop_button = ttk.Button(controls, text="停止服务", command=stop)
    stop_button.pack(side="left", padx=8)
    ttk.Button(
        controls, text="查看日志", command=lambda: inspect(launcher.log_file)
    ).pack(side="left")
    data_button = ttk.Button(
        body, text="打开数据所在文件夹", command=lambda: inspect(launcher.db.parent)
    )
    data_button.pack(anchor="w")
    tk.Label(
        body,
        text="关闭此窗口会停止服务，记录保存在项目文件夹的 memory.db。",
        wraplength=530,
        justify="left",
        fg="#587074",
        bg="#f7f6f1",
        font=("Microsoft YaHei UI", 9),
    ).pack(anchor="w", pady=(12, 0))

    def poll():
        while not launcher.messages.empty():
            kind, value = launcher.messages.get_nowait()
            if kind == "status":
                status.set(value)
                continue
            progress.stop()
            progress.configure(mode="determinate", value=100 if kind == "ready" else 0)
            if active["closing"]:
                window.destroy()
                return
            if kind == "ready":
                active["state"] = value
                status.set("工作台已就绪，正在打开浏览器。")
                address.set(local_url(value))
                launch_button.configure(text="打开工作台", state="normal")
                stop_button.configure(state="normal")
                open_browser()
            else:
                active["state"] = None
                address.set("本地模式 · 无需模型密钥")
                status.set(
                    "服务已停止，已保存记录保留。" if kind == "stopped" else value
                )
                launch_button.configure(text="重新启动", state="normal")
                stop_button.configure(state="disabled")
        if active["state"] and launcher.process and launcher.process.poll() is not None:
            active["state"] = None
            status.set("服务已退出。请查看日志，或点击重新启动。")
            launch_button.configure(text="重新启动", state="normal")
            stop_button.configure(state="disabled")
        window.after(150, poll)

    window.protocol("WM_DELETE_WINDOW", close)
    begin()
    window.after(150, poll)
    window.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--instance")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.serve:
        return serve(args.state, args.db, args.instance, args.port)
    import tkinter as tk
    from tkinter import messagebox

    launcher = Launcher()
    lock = InstanceLock(launcher.data_dir / "launcher.lock")
    try:
        if not lock.acquire():
            existing = running_instance(launcher.state_file)
            if existing:
                webbrowser.open(local_url(existing))
            else:
                root = tk.Tk()
                root.withdraw()
                messagebox.showinfo(
                    "续研", "启动窗口已经打开，正在准备或停止服务。请回到该窗口继续。"
                )
                root.destroy()
            return 0
        show_window(launcher)
        return 0
    finally:
        launcher.stop()
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
