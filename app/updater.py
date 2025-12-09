# app/updater.py
import subprocess
import os
from pathlib import Path

#17#

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

    # 3. restart via systemd-run
    log_debug("Attempting restart...")
    restart = _run(["sudo", "/bin/systemctl", "start", "restart-medicam.service"])
    log_debug(f"RESTART RESULT: {restart}")

    # Extra: run journalctl for restart job
    journal = _run(["journalctl", "-u", "medicam-restart", "--no-pager", "--since", "5 minutes ago"])
    log_debug(f"JOURNAL OUTPUT: {journal}")

    log_debug("===== APPLY UPDATE END =====")

    if not restart["ok"]:
        return {"ok": False, "step": "restart", **restart}

    return {
        "ok": True,
        "step": "done",
        "local": get_local_commit()
    }