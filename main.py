import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("combined-telegram-bots")

ROOT = Path(__file__).resolve().parent

APP_CONFIGS = {
    "spy": {
        "script": ROOT / "apps" / "spy" / "bot.py",
        "token_env": "SPY_BOT_TOKEN",
        "database_env": "SPY_DATABASE_URL",
        "local_port": "10001",
    },
    "bank": {
        "script": ROOT / "apps" / "bank" / "bot.py",
        "token_env": "BANK_BOT_TOKEN",
        "database_env": "BANK_DATABASE_URL",
        "local_port": "10002",
    },
}

processes: Dict[str, subprocess.Popen] = {}
stop_event = threading.Event()
lock = threading.Lock()
web_app = Flask(__name__)

def masked(value: Optional[str]) -> str:
    if not value:
        return "ausente"
    return "***" if len(value) <= 8 else f"{value[:4]}...{value[-4:]}"

def validate_configuration():
    missing = []
    for _, cfg in APP_CONFIGS.items():
        if not os.environ.get(cfg["token_env"], "").strip():
            missing.append(cfg["token_env"])
        if not os.environ.get(cfg["database_env"], "").strip():
            missing.append(cfg["database_env"])
        if not cfg["script"].exists():
            missing.append(str(cfg["script"]))
    if missing:
        raise RuntimeError("Configuração/arquivos ausentes: " + ", ".join(missing))

def child_environment(name: str) -> dict:
    cfg = APP_CONFIGS[name]
    env = os.environ.copy()
    env["BOT_TOKEN"] = os.environ[cfg["token_env"]].strip()
    env["DATABASE_URL"] = os.environ[cfg["database_env"]].strip()
    env["PORT"] = cfg["local_port"]
    env["PYTHONUNBUFFERED"] = "1"
    return env

def launch_child(name: str) -> subprocess.Popen:
    cfg = APP_CONFIGS[name]
    env = child_environment(name)
    logger.info(
        "Iniciando %s | script=%s | token=%s | banco=%s | porta=%s",
        name,
        cfg["script"],
        masked(env.get("BOT_TOKEN")),
        "configurado" if env.get("DATABASE_URL") else "ausente",
        env["PORT"],
    )
    return subprocess.Popen(
        ["python", "-u", str(cfg["script"])],
        cwd=str(cfg["script"].parent),
        env=env,
    )

def monitor_child(name: str):
    while not stop_event.is_set():
        try:
            process = launch_child(name)
            with lock:
                processes[name] = process
            exit_code = process.wait()
            with lock:
                processes.pop(name, None)
            if stop_event.is_set():
                break
            logger.error("%s encerrou com código %s. Reiniciando em 10s.", name, exit_code)
            stop_event.wait(10)
        except Exception:
            logger.exception("Falha ao iniciar/monitorar %s.", name)
            stop_event.wait(15)

def shutdown(signum=None, frame=None):
    if stop_event.is_set():
        return
    logger.info("Encerrando serviço combinado...")
    stop_event.set()
    with lock:
        current = list(processes.items())
    for name, process in current:
        if process.poll() is None:
            logger.info("Enviando SIGTERM para %s.", name)
            process.terminate()
    deadline = time.time() + 20
    for name, process in current:
        try:
            process.wait(timeout=max(0, deadline-time.time()))
        except subprocess.TimeoutExpired:
            logger.warning("Forçando encerramento de %s.", name)
            process.kill()

@web_app.get("/")
def health():
    result = {}
    with lock:
        for name in APP_CONFIGS:
            process = processes.get(name)
            result[name] = {
                "running": bool(process and process.poll() is None),
                "pid": process.pid if process and process.poll() is None else None,
                "exit_code": process.poll() if process else None,
            }
    all_running = all(item["running"] for item in result.values())
    return jsonify({
        "status": "online" if all_running else "degraded",
        "service": "telegram-bots-combined",
        "bots": result,
    }), 200 if all_running else 503

@web_app.get("/health")
def health_alias():
    return health()

def main():
    validate_configuration()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    for name in APP_CONFIGS:
        threading.Thread(
            target=monitor_child,
            args=(name,),
            daemon=True,
            name=f"monitor-{name}",
        ).start()

    port = int(os.environ.get("PORT", "10000"))
    logger.info("Servidor mestre disponível em 0.0.0.0:%s", port)

    try:
        web_app.run(
            host="0.0.0.0",
            port=port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    finally:
        shutdown()

if __name__ == "__main__":
    main()
