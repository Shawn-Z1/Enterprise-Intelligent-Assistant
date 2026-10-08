import json
import sys
import time
from pathlib import Path

from app.config import get_settings


def is_healthy() -> bool:
    """无参数检查心跳新鲜度、依赖结果和存活进程；返回 worker 是否真实健康。"""
    settings = get_settings()
    try:
        state = json.loads(Path(settings.worker_state_path).read_text(encoding="utf-8"))
        return bool(state["ready"] and 0 <= time.time() - state["timestamp"] < settings.worker_stale_seconds
                    and Path(f'/proc/{state["pid"]}').exists())
    except (OSError, ValueError, KeyError):
        return False


if __name__ == "__main__":
    sys.exit(0 if is_healthy() else 1)
