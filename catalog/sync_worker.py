"""短期資料校對子程序的生命週期管理。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from contextlib import ExitStack
from pathlib import Path
from typing import Callable, Mapping, Sequence

from catalog.schema import read_json_object, write_json_atomically


class CatalogWorkerCancelled(RuntimeError):
    pass


def commit_catalog_cache_files(
    replacements: Sequence[tuple[Path, Path]],
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """先驗證整批快取；提交失敗或取消時還原原檔與備份的字節。"""

    def check_cancelled() -> None:
        if should_cancel is not None and should_cancel():
            raise CatalogWorkerCancelled("資料校對已取消")

    # 每一份新檔與還原檔都放在正式檔所在磁碟，避免跨磁碟替換失敗。
    with ExitStack() as cleanup:
        prepared: list[tuple[Path, Path, Path | None]] = []

        def prepare(source: Path, destination: Path) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            work = Path(cleanup.enter_context(tempfile.TemporaryDirectory(
                prefix=".ro-rrf-commit-", dir=str(destination.parent)
            )))
            staged = work / "new.json"
            shutil.copy2(source, staged)
            previous = work / "previous.json" if destination.is_file() else None
            if previous is not None:
                shutil.copy2(destination, previous)
            prepared.append((staged, destination, previous))

        for source, destination in replacements:
            check_cancelled()
            source, destination = Path(source), Path(destination)
            read_json_object(source)
            if destination.is_file() and source.read_bytes() == destination.read_bytes():
                # 沿用有效快取時不輪替 .bak，保留真正的上一代資料。
                continue
            prepare(source, destination)
            if destination.is_file():
                prepare(destination, destination.with_name(destination.name + ".bak"))

        applied: list[tuple[Path, Path | None]] = []
        try:
            for staged, destination, previous in prepared:
                check_cancelled()
                os.replace(staged, destination)
                applied.append((destination, previous))
            check_cancelled()
        except BaseException:
            for destination, previous in reversed(applied):
                if previous is None:
                    destination.unlink(missing_ok=True)
                else:
                    os.replace(previous, destination)
            raise


@dataclass(frozen=True)
class CatalogWorkerProgress:
    value: float
    message: str


class CatalogWorkerClient:
    """保證同時只有一個校對子程序，並提供可測試的取消入口。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: subprocess.Popen[bytes] | None = None
        self._busy = False
        self._cancel_requested = threading.Event()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._busy

    def cancel(self) -> None:
        self._cancel_requested.set()
        with self._lock:
            process = self._active
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def run(
        self,
        *,
        request: Mapping[str, object],
        command_builder: Callable[[Path, Path, Path], Sequence[str]],
        on_progress: Callable[[CatalogWorkerProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        poll_seconds: float = 0.1,
    ) -> dict[str, object]:
        with self._lock:
            if self._busy:
                raise RuntimeError("已有資料校對工作正在執行")
            self._busy = True
            self._cancel_requested.clear()
        try:
            return self._run_worker(
                request=request,
                command_builder=command_builder,
                on_progress=on_progress,
                should_cancel=should_cancel,
                poll_seconds=poll_seconds,
            )
        finally:
            with self._lock:
                self._busy = False
                self._cancel_requested.clear()

    def _run_worker(
        self,
        *,
        request: Mapping[str, object],
        command_builder: Callable[[Path, Path, Path], Sequence[str]],
        on_progress: Callable[[CatalogWorkerProgress], None] | None,
        should_cancel: Callable[[], bool] | None,
        poll_seconds: float,
    ) -> dict[str, object]:

        with tempfile.TemporaryDirectory(prefix="ro-rrf-catalog-") as temp_dir:
            temp_root = Path(temp_dir)
            request_path = temp_root / "request.json"
            result_path = temp_root / "result.json"
            progress_path = temp_root / "progress.json"
            worker_request = dict(request)
            cache_replacements: list[tuple[Path, Path]] = []
            if worker_request.get("mode") == "full":
                # 子程序只改暫存副本；終止子程序永遠不會中斷正式快取提交。
                for key in ("status_cache_path", "client_cache_path"):
                    destination = Path(str(worker_request[key]))
                    staged = temp_root / (key + ".json")
                    if destination.is_file():
                        shutil.copy2(destination, staged)
                    worker_request[key] = str(staged)
                    cache_replacements.append((staged, destination))
            write_json_atomically(request_path, worker_request)
            command = [str(value) for value in command_builder(
                request_path,
                result_path,
                progress_path,
            )]
            if not command:
                raise ValueError("校對子程序命令為空")
            if self._cancel_requested.is_set() or (should_cancel is not None and should_cancel()):
                raise CatalogWorkerCancelled("資料校對已取消")
            creationflags = (
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if os.name == "nt"
                else 0
            )
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            with self._lock:
                self._active = process
            last_progress: tuple[float, str] | None = None
            try:
                while process.poll() is None:
                    cancelled = self._cancel_requested.is_set() or (
                        should_cancel is not None and should_cancel()
                    )
                    if cancelled:
                        self._stop_process(process)
                        raise CatalogWorkerCancelled("資料校對已取消")
                    if progress_path.is_file():
                        try:
                            progress_payload = read_json_object(progress_path)
                            progress = CatalogWorkerProgress(
                                max(0.0, min(100.0, float(progress_payload.get("value", 0.0)))),
                                str(progress_payload.get("message", "")),
                            )
                            signature = (progress.value, progress.message)
                            if signature != last_progress:
                                last_progress = signature
                                if on_progress is not None:
                                    on_progress(progress)
                        except (OSError, TypeError, ValueError, json.JSONDecodeError):
                            pass
                    time.sleep(max(0.02, float(poll_seconds)))

                return_code = process.wait()
                if self._cancel_requested.is_set() or (should_cancel is not None and should_cancel()):
                    raise CatalogWorkerCancelled("資料校對已取消")
                if not result_path.is_file():
                    raise RuntimeError(f"校對程序沒有回傳結果（{return_code}）")
                payload = read_json_object(result_path)
                if not payload.get("ok"):
                    raise RuntimeError(
                        str(payload.get("error", f"校對程序結束（{return_code}）"))
                    )
                if return_code != 0:
                    raise RuntimeError(f"校對程序未正常結束（{return_code}）")
                if cache_replacements:
                    commit_catalog_cache_files(
                        cache_replacements,
                        should_cancel=lambda: self._cancel_requested.is_set() or (
                            should_cancel is not None and should_cancel()
                        ),
                    )
                return payload
            finally:
                if process.poll() is None:
                    self._stop_process(process)
                with self._lock:
                    if self._active is process:
                        self._active = None

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=1.0)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
