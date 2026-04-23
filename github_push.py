import subprocess
import os
import shutil
import time
from threading import Lock

from utils import logger

_push_lock = Lock()
_last_push_ts = 0.0
_MIN_PUSH_INTERVAL_SEC = 30  # защита от спама коммитами/пушами
_last_ok_iso: str | None = None
_last_err: str | None = None


def get_publish_status() -> dict:
    """
    Статус публикации для отображения на сайте.
    """
    return {
        "last_ok": _last_ok_iso,
        "last_error": _last_err,
        "min_push_interval_sec": _MIN_PUSH_INTERVAL_SEC,
    }


def _git():
    path = shutil.which("git")
    if path:
        return path
    for p in [
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files (x86)\Git\bin\git.exe",
    ]:
        if os.path.exists(p):
            return p
    return "git"


def push_data_json(path: str = "docs/data.json"):
    global _last_push_ts, _last_ok_iso, _last_err
    if not os.path.exists(path):
        logger.warn("GitHub", "Файл не найден")
        _last_err = "file not found"
        return
    try:
        # Дебаунс: GitHub Pages/Netlify всё равно не обновятся быстрее,
        # а частые коммиты тормозят трекер и захламляют историю.
        with _push_lock:
            now = time.time()
            if now - _last_push_ts < _MIN_PUSH_INTERVAL_SEC:
                return
            _last_push_ts = now

        git = _git()

        # Пушим и data.json и index.html
        files = [path]
        if os.path.exists("docs/index.html"):
            files.append("docs/index.html")

        subprocess.run([git, "add"] + files, check=True, capture_output=True)

        result = subprocess.run(
            [git, "commit", "-m", "update data"],
            capture_output=True, text=True
        )

        out = (result.stdout or "") + "\n" + (result.stderr or "")
        if "nothing to commit" in out.lower():
            return
        if result.returncode != 0:
            _last_err = (out.strip() or "git commit failed")[:240]
            logger.warn("GitHub", f"git commit failed: {(_last_err or '')[:120]}")
            return

        subprocess.run([git, "push"], check=True, capture_output=True)
        _last_ok_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _last_err = None
        logger.ok("GitHub", "Сайт обновлён")

    except subprocess.CalledProcessError as e:
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr or e)
        _last_err = err[:240]
        logger.err("GitHub", f"Ошибка: {err[:120]}")
    except Exception as e:
        _last_err = str(e)[:240]
        logger.err("GitHub", f"{e}")