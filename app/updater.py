# app/updater.py
import subprocess
import time

#18#

# ----------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -----------

def _run(cmd: list[str]):
    """Выполняет системную команду и возвращает {ok, stdout, stderr}."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "returncode": proc.returncode,
        }
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": str(e)}


# ----------- ОПРЕДЕЛЕНИЕ ВЕРСИЙ -----------

def get_local_commit():
    """Возвращает локальный git HEAD."""
    result = _run(["git", "rev-parse", "HEAD"])
    return result["stdout"] if result["ok"] else None


def get_remote_commit():
    """
    Получает удалённую версию origin/main.
    ВАЖНО: repo должен быть настроен на origin: SSH или HTTPS.
    """
    result = _run(["git", "ls-remote", "origin", "HEAD"])
    if not result["ok"]:
        return None

    line = result["stdout"].strip()
    if not line:
        return None

    # формат: "<sha>\tHEAD"
    return line.split("\t")[0]


# ----------- CHECK UPDATE -----------

def check_for_update():
    local = get_local_commit()
    remote = get_remote_commit()

    return {
        "local": local,
        "remote": remote,
        "update_available": (local != remote and remote is not None)
    }


# ----------- APPLY UPDATE -----------

def log_debug(text: str):
    try:
        with open("/home/radxa/medicam-server/update_debug.log", "a") as f:
            f.write(text + "\n")
    except:
        pass


def apply_update():
    log_debug("===== APPLY UPDATE START =====")

    # 1. fetch
    fetch = _run(["git", "fetch"])
    log_debug(f"FETCH: {fetch}")
    if not fetch["ok"]:
        return {"ok": False, "step": "fetch", **fetch}

    # 2. reset
    reset = _run(["git", "reset", "--hard", "origin/main"])
    log_debug(f"RESET: {reset}")
    if not reset["ok"]:
        return {"ok": False, "step": "reset", **reset}

    # 3. Keep the systemd runtime directory and other server-unit drop-ins in
    # sync with the checked-out version before restarting the service.
    install_runtime = _run([
        "/bin/bash",
        "scripts/install_server_runtime.sh",
    ])
    log_debug(f"SERVER RUNTIME INSTALL: {install_runtime}")
    if not install_runtime["ok"]:
        return {
            "ok": False,
            "step": "server_runtime",
            **install_runtime,
        }

    # 4. Schedule restart after this HTTP response is returned. A synchronous
    # restart kills the current uvicorn worker before /update/apply can respond,
    # which makes successful updates look like HTTP 500 failures.
    restart_unit = f"medicam-restart-{int(time.time())}"
    log_debug(f"Scheduling restart via {restart_unit}...")
    restart = _run([
        "sudo",
        "/usr/bin/systemd-run",
        "--unit", restart_unit,
        "--on-active=1",
        "--collect",
        "/bin/systemctl",
        "restart",
        "medicam.service",
        "medicam-ble-manager.service",
    ])
    log_debug(f"RESTART RESULT: {restart}")

    ble_restart_unit = f"medicam-ble-restart-{int(time.time())}"
    ble_restart = _run([
        "sudo",
        "/usr/bin/systemd-run",
        "--unit", ble_restart_unit,
        "--on-active=1",
        "--collect",
        "/bin/systemctl",
        "try-restart",
        "medicam-ble.service",
    ])
    log_debug(f"BLE RESTART RESULT: {ble_restart}")

    log_debug("===== APPLY UPDATE END =====")

    if not restart["ok"]:
        return {"ok": False, "step": "restart", **restart}
    if not ble_restart["ok"]:
        return {"ok": False, "step": "ble_restart", **ble_restart}

    return {
        "ok": True,
        "step": "done",
        "local": get_local_commit(),
        "restart_unit": restart_unit,
        "ble_restart_unit": ble_restart_unit,
    }
