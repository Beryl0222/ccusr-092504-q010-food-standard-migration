"""追加式事件日志：内存列表 + 可选 JSONL 持久化，支持重放恢复。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


class EventLog:
    """事件只追加不改写；指定路径时逐行落盘，重启后整段重放。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._events: list[dict[str, Any]] = []
        if self._path and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._events.append(json.loads(line))

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def append(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
