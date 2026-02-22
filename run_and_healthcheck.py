from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def is_healthy(url: str, timeout: float = 2.0) -> bool:
    request = urllib.request.Request(url=url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return int(resp.status) < 500
    except urllib.error.URLError:
        return False
    except Exception:
        return False


def main() -> int:
    load_env_file(Path(__file__).resolve().parent / ".env")
    parser = argparse.ArgumentParser(description="Start Flask app in background and wait for health check.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"), help="Host to bind/check")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")), help="Port to bind/check")
    parser.add_argument("--timeout", type=float, default=25.0, help="Seconds to wait for health")
    parser.add_argument("--check-only", action="store_true", help="Only check current health, do not spawn")
    parser.add_argument("--open-browser", action="store_true", help="Open app URL in default browser when healthy")
    args = parser.parse_args()

    base_url = f"http://{args.host}:{args.port}"
    health_url = f"{base_url}/"

    if is_healthy(health_url):
        print(f"Already healthy: {health_url}")
        if args.open_browser:
            try:
                webbrowser.open(health_url)
            except Exception:
                pass
        return 0

    if args.check_only:
        print(f"Not healthy: {health_url}")
        return 1

    runtime_dir = Path("runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_file = runtime_dir / "server.log"
    pid_file = runtime_dir / "server.pid"

    env = os.environ.copy()
    env["HOST"] = args.host
    env["PORT"] = str(args.port)
    env["FLASK_DEBUG"] = "0"

    creation_flags = 0
    if os.name == "nt":
        creation_flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

    with log_file.open("ab") as log_stream:
        proc = subprocess.Popen(
            [sys.executable, "app.py"],
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=str(Path(__file__).resolve().parent),
            creationflags=creation_flags,
            close_fds=True,
        )

    deadline = time.time() + max(1.0, args.timeout)
    while time.time() < deadline:
        if proc.poll() is not None:
            print("Server process exited before becoming healthy.")
            print(f"See log: {log_file}")
            return 1
        if is_healthy(health_url):
            pid_file.write_text(str(proc.pid), encoding="utf-8")
            print(f"Server started. URL: {health_url}")
            print(f"PID: {proc.pid}")
            print(f"Log: {log_file}")
            if args.open_browser:
                try:
                    webbrowser.open(health_url)
                except Exception:
                    pass
            return 0
        time.sleep(0.4)

    try:
        proc.terminate()
    except Exception:
        pass
    print(f"Health check timed out after {args.timeout}s: {health_url}")
    print(f"See log: {log_file}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
