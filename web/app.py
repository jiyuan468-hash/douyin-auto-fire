"""Web control panel for douyin-auto-fire."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

from flask import Flask, jsonify, render_template, request, Response

app = Flask(__name__, template_folder="templates", static_folder="static")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("web_ui")
_last_result: dict = {}
_task_pid: Optional[int] = None
_task_thread: Optional[threading.Thread] = None
_task_lock = threading.Lock()


def _read_config() -> dict:
    cfg_path = PROJECT_ROOT / "config.json"
    if cfg_path.exists():
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    return {}


def _write_config(cfg: dict) -> None:
    cfg_path = PROJECT_ROOT / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_result() -> dict:
    result_path = PROJECT_ROOT / "artifacts" / "result.json"
    if result_path.exists():
        return json.loads(result_path.read_text(encoding="utf-8"))
    return {}


def _read_log_lines(n: int = 200) -> list[str]:
    log_path = PROJECT_ROOT / "artifacts" / "run.log"
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-n:]


def _run_task(dry_run: bool) -> dict:
    global _last_result
    cmd = [sys.executable, "run.py", "--dry-run" if dry_run else ""]
    cmd = [c for c in cmd if c]
    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        output = proc.stdout + proc.stderr
        result = _read_result()
        _last_result = result
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": output, "result": result}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "任务超时（>5分钟）"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.route("/")
def index():
    cfg = _read_config()
    result = _read_result()
    log_lines = _read_log_lines(100)
    has_storage = (PROJECT_ROOT / "storage-state.json").exists()
    has_cookie = (PROJECT_ROOT / "chrome-cookies.json").exists()
    return render_template(
        "index.html",
        config=cfg,
        result=result,
        log_lines=log_lines,
        has_storage=has_storage,
        has_cookie=has_cookie,
    )


@app.route("/api/config", methods=["GET"])
def api_config():
    return jsonify(_read_config())


@app.route("/api/config", methods=["POST"])
def api_update_config():
    cfg = request.get_json()
    if not isinstance(cfg, dict):
        return jsonify({"error": "invalid"}), 400
    _write_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/run", methods=["POST"])
def api_run():
    dry_run = request.json.get("dry_run", True)
    with _task_lock:
        global _task_thread, _task_pid
        if _task_thread and _task_thread.is_alive():
            return jsonify({"error": "任务正在运行中，请等待完成"}), 409
        res = _run_task(dry_run)
        _last_result = res.get("result", {})
        return jsonify(res)


@app.route("/api/result")
def api_result():
    return jsonify(_read_result())


@app.route("/api/log")
def api_log():
    n = request.args.get("n", 200, type=int)
    return jsonify(_read_log_lines(n))


@app.route("/api/login-status")
def api_login_status():
    storage = PROJECT_ROOT / "storage-state.json"
    cookie = PROJECT_ROOT / "chrome-cookies.json"
    status = {
        "has_storage": storage.exists(),
        "has_cookie": cookie.exists(),
    }
    if storage.exists():
        try:
            state = json.loads(storage.read_text(encoding="utf-8"))
            status["cookie_count"] = len(state.get("cookies", []))
        except Exception:
            status["cookie_count"] = 0
    return jsonify(status)


@app.route("/api/run-login", methods=["POST"])
def api_run_login():
    with _task_lock:
        if _task_thread and _task_thread.is_alive():
            return jsonify({"error": "任务正在运行中"}), 409
        def _run():
            cmd = [sys.executable, "scripts/login.py"]
            env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
            try:
                proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env,
                                       capture_output=True, text=True, encoding="utf-8",
                                       errors="replace", timeout=120)
                return {"ok": proc.returncode == 0, "output": proc.stdout + proc.stderr}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        return jsonify(_run())


@app.route("/stream-log")
def stream_log():
    def _gen():
        log_path = PROJECT_ROOT / "artifacts" / "run.log"
        if not log_path.exists():
            yield json.dumps({"lines": []}) + "\n"
            return
        while True:
            lines = _read_log_lines(100)
            yield json.dumps({"lines": lines}) + "\n"
            __import__("time").sleep(3)
    return Response(_gen(), mimetype="text/event-stream")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="127.0.0.1", port=5000, debug=False)
