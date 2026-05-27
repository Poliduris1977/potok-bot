# app/knowledge/loader.py
# Загружает базу знаний из markdown-файлов в app/knowledge/data/
# Пока файлы — заглушки. Заполнятся после брифа с клиентом.

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "data"

_FILES = {
    "company":    "company.md",
    "products":   "products.md",
    "objections": "objections.md",
    "workflow":   "workflow.md",
}


@lru_cache(maxsize=1)
def get_knowledge() -> dict[str, str]:
    """Загрузить все файлы базы знаний. Кэшируется до перезапуска."""
    result: dict[str, str] = {}
    for key, filename in _FILES.items():
        path = _DATA_DIR / filename
        if path.exists():
            result[key] = path.read_text(encoding="utf-8").strip()
            logger.info("База знаний загружена: %s (%d символов)", filename, len(result[key]))
        else:
            result[key] = ""
            logger.warning("Файл базы знаний не найден: %s", path)
    return result
