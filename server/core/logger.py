"""공유 로거 — 모든 에이전트가 동일한 logger 인스턴스를 사용한다."""

import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_RUNTIME_DIR = _ROOT / "runtime"
_LOG_FILE = _RUNTIME_DIR / "agent.log"


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("migration_agent")
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        # Windows 환경 UTF-8 인코딩 보정
        try:
            import io
            sys.stdout = io.TextIOWrapper(
                sys.stdout.detach(), encoding="utf-8", line_buffering=True
            )
        except Exception:
            pass
        formatter = logging.Formatter("%(asctime)s - [%(name)s] [%(levelname)s] - %(message)s")
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        try:
            _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception:
            pass
        logger.propagate = False
    return logger


logger = _setup_logger()
