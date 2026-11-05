"""Đường dẫn khi chạy source hoặc file .exe PyInstaller."""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Kho, cookie, accounts.txt — cùng thư mục với MuseManager.exe."""
    if is_frozen():
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base


DATA_DIR = data_dir()
BUNDLE_DIR = bundle_dir()
