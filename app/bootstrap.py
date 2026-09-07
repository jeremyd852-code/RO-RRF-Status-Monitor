"""不依賴 Tk 的啟動階段量測。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class StartupTimeline:
    started_at: float = field(default_factory=time.perf_counter)
    marks: list[tuple[str, float]] = field(default_factory=list)

    def mark(self, name: str) -> None:
        self.marks.append((str(name), time.perf_counter()))

    @property
    def elapsed_seconds(self) -> float:
        end = self.marks[-1][1] if self.marks else time.perf_counter()
        return max(0.0, end - self.started_at)

    def stage_durations(self) -> tuple[tuple[str, float], ...]:
        previous = self.started_at
        result: list[tuple[str, float]] = []
        for name, timestamp in self.marks:
            result.append((name, max(0.0, timestamp - previous)))
            previous = timestamp
        return tuple(result)

    def slow_summary(self, *, threshold_seconds: float = 0.8) -> str:
        if self.elapsed_seconds <= max(0.0, float(threshold_seconds)):
            return ""
        details = "、".join(
            f"{name} {duration:.2f}s"
            for name, duration in self.stage_durations()
        )
        return f"啟動耗時 {self.elapsed_seconds:.2f}s（{details}）"
