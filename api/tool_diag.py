"""CC 诊断日志 - 常驻启用，基于文件，用户不可见。

所有 CC 工具链事件追加写入 data/cc_diag.log。
最大 1000 行；超出后自动裁剪至 500 行。
设计为可随产品分发给终端用户，用户无感知。
"""
import os
import threading
import time

_DIAG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_DIAG_PATH = os.path.join(_DIAG_DIR, "cc_diag.log")
_MAX_LINES = 1000
_TRIM_TO = 500
_lock = threading.Lock()
_write_count = 0


def tool_diag(cc_id="", ca_sid="", event="", detail=""):
    """追加一行诊断日志。线程安全，自动裁剪。

    参数:
        cc_id:   CC 会话 ID（或前缀）
        ca_sid:  ChatApp 会话 ID（或前缀）
        event:   简短事件标签（如 EXEC_START, AR_QUEUE）
        detail:  自由格式的详细信息字符串
    """
    global _write_count
    _now = time.time()
    ts = time.strftime("%H:%M:%S", time.localtime(_now)) + f".{int(_now * 1000) % 1000:03d}"
    cc_short = str(cc_id)[:24] if cc_id else "-"
    ca_short = str(ca_sid)[:12] if ca_sid else "-"
    line = f"{ts} [{cc_short}] [{ca_short}] {event}: {detail}\n"
    try:
        with _lock:
            with open(_DIAG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
            _write_count += 1
            if _write_count >= 100:
                _write_count = 0
                _maybe_trim()
    except Exception:
        pass


def _maybe_trim():
    """如果文件超过 _MAX_LINES 行，仅保留最后 _TRIM_TO 行。"""
    try:
        with open(_DIAG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > _MAX_LINES:
            with open(_DIAG_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines[-_TRIM_TO:])
    except Exception:
        pass
