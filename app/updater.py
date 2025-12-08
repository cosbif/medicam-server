# app/updater.py
import subprocess
import os
from pathlib import Path

#8#

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
            "stderr": proc.stderr.strip()
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

def apply_update():
    """
    git fetch → git reset → restart service
    """
    # 1. fetch
    fetch = _run(["git", "fetch"])
    if not fetch["ok"]:
        return {"ok": False, "step": "fetch", **fetch}

    # 2. reset
    reset = _run(["git", "reset", "--hard", "origin/main"])
    if not reset["ok"]:
        return {"ok": False, "step": "reset", **reset}

    # 3. restart systemd service
    # !!! НАЗВАНИЕ СЕРВИСА УКАЖИ СВОЁ !!!
    service_name = "medicam.service"

    restart = _run(["sudo", "-E", "/bin/systemctl", "restart", service_name])

    if not restart["ok"]:
        return {"ok": False, "step": "restart", **restart}

    return {
        "ok": True,
        "step": "done",
        "local": get_local_commit()
    }