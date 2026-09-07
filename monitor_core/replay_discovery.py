"""低負載辨識目前真正持續寫入的 RRF。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ReplayFileSample:
    """一次目錄掃描取得的最小檔案資訊。"""

    path: Path
    size: int
    mtime_ns: int


@dataclass
class _ReplayObservation:
    size: int
    mtime_ns: int
    last_growth_at: float | None = None


class LiveReplayDiscovery:
    """只有觀察到檔案大小增加後，才把它視為即時錄影。

    新啟動時先記住現有歷史檔的大小；下一次掃描真的成長才會選取。
    已確認的活動檔可在短暫無資料時維持一段寬限，避免正常封包空檔造成
    反覆停止與重讀。
    """

    def __init__(self, *, active_grace_seconds: float = 20.0) -> None:
        self.active_grace_seconds = max(1.0, float(active_grace_seconds))
        self.directory: Path | None = None
        self.current_path: Path | None = None
        self._observations: dict[Path, _ReplayObservation] = {}

    def reset(self, *, keep_observations: bool = False) -> None:
        """重設目前選取；切換資料夾時也清除歷史觀察。"""

        self.current_path = None
        if not keep_observations:
            self.directory = None
            self._observations.clear()

    def observe(
        self,
        directory: Path,
        samples: Iterable[ReplayFileSample],
        *,
        now: float,
    ) -> Path | None:
        normalized_directory = Path(directory)
        if self.directory != normalized_directory:
            self.directory = normalized_directory
            self.current_path = None
            self._observations.clear()

        present: set[Path] = set()
        for sample in samples:
            path = Path(sample.path)
            present.add(path)
            previous = self._observations.get(path)
            if previous is None:
                self._observations[path] = _ReplayObservation(
                    size=max(0, int(sample.size)),
                    mtime_ns=max(0, int(sample.mtime_ns)),
                )
                continue

            size = max(0, int(sample.size))
            mtime_ns = max(0, int(sample.mtime_ns))
            # 只有檔案實際增加才證明正在錄影。若同名重錄造成截斷，先把
            # 縮小後的大小當成新基線，等下一次增加才選取；mtime 變動也
            # 不足以證明錄影，避免工具碰觸歷史檔造成誤判。
            if size > previous.size:
                previous.last_growth_at = float(now)
            elif size < previous.size:
                previous.last_growth_at = None
            previous.size = size
            previous.mtime_ns = mtime_ns

        for missing in set(self._observations) - present:
            self._observations.pop(missing, None)
        if self.current_path not in present:
            self.current_path = None

        active = [
            (path, observation)
            for path, observation in self._observations.items()
            if observation.last_growth_at is not None
            and float(now) - observation.last_growth_at <= self.active_grace_seconds
        ]
        if not active:
            self.current_path = None
            return None

        # 新錄影檔一旦真的開始成長就立即切換；不能因舊檔仍在寬限內而
        # 額外等待最多 20 秒。寬限只用來容忍同一活動檔的短暫寫入空檔。
        self.current_path = max(
            active,
            key=lambda item: (
                item[1].last_growth_at or 0.0,
                item[1].mtime_ns,
                str(item[0]).casefold(),
            ),
        )[0]
        return self.current_path
