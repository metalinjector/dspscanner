"""Оркестратор сканирования документов."""
from __future__ import annotations

import multiprocessing
import os
import signal
import shutil
import subprocess
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as futures_wait
from dataclasses import replace
from datetime import datetime
from queue import Empty, Full, Queue
from threading import BoundedSemaphore, Event, Thread
from typing import Callable, Optional

from app.config import (
    DocReadMethod, ErrorEntry, FileEntry, ScanReport, ScanSettings, ScanStats, SearchResult,
)
from app.readers import extract_text
from app.readers.doc_reader import DocReader, _release_word_all
from app.scanning.file_finder import iter_files_safe
from app.scanning.search_engine import find_matches
from app.settings_store import load_settings

ProgressCallback = Callable[[int, int, str], None]
ResultCallback = Callable[[SearchResult], None]
ResultsCallback = Callable[[list[SearchResult]], None]
LogCallback = Callable[[str, str], None]
StatsCallback = Callable[[ScanStats], None]

_EXT_STAT_FIELD = {
    ".docx": "docx_count",
    ".doc": "doc_count",
    ".txt": "txt_count",
    ".pdf": "pdf_count",
}
_RISKY_EXTENSIONS = {".doc", ".pdf"}
# Используется и при формировании сообщения таймаута, и при его классификации
# в record_problem(), чтобы обе точки не могли разойтись при правке текста.
TIMEOUT_MARKER = "Таймаут чтения файла"
_ISOLATE_DOCX_ABOVE_BYTES = 16 * 1024 * 1024
_CPU_COUNT = max(1, os.cpu_count() or 1)
_RISKY_PROCESS_LIMIT = min(4, max(1, _CPU_COUNT // 2))
_RISKY_SEMAPHORE = BoundedSemaphore(_RISKY_PROCESS_LIMIT)
_OCR_LIMIT_PER_PROCESS = max(1, _CPU_COUNT // _RISKY_PROCESS_LIMIT)


def _isolated_process_target(send_connection, entry: FileEntry, settings: ScanSettings, ocr_limit: int) -> None:
    """Точка входа дочернего процесса для потенциально зависающих ридеров."""
    try:
        if os.name != "nt":
            try:
                os.setsid()
            except OSError:
                pass
        from app.readers.pdf_reader import configure_ocr_limit

        configure_ocr_limit(ocr_limit)
        payload = DocumentScanner._process_one_direct(entry, settings)
        send_connection.send(("ok", payload))
    except BaseException as exc:
        try:
            send_connection.send(("error", f"{type(exc).__name__}: {exc}"))
        except Exception:
            pass
    finally:
        try:
            send_connection.close()
        except Exception:
            pass


def _terminate_process_tree(process: multiprocessing.Process) -> None:
    """Завершает ридер вместе с запущенными им внешними программами.

    На POSIX дочерний ридер создаёт отдельную process group через ``setsid``.
    Важно послать сигнал группе *до* проверки, завершился ли её лидер: иначе
    LibreOffice/Tesseract могли пережить уже остановленный Python-процесс.
    """
    if process.pid is None:
        return
    pid = process.pid

    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            try:
                subprocess.run(
                    [taskkill, "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        if process.is_alive():
            try:
                process.terminate()
            except (OSError, AttributeError):
                pass
            process.join(timeout=1.0)
        if process.is_alive():
            try:
                process.kill()
            except (OSError, AttributeError):
                pass
        process.join(timeout=2.0)
        return

    process_group: int | None = None
    try:
        candidate = os.getpgid(pid)
        # Не посылаем сигнал чужой/родительской группе при редкой гонке до setsid().
        if candidate == pid:
            process_group = candidate
    except (OSError, ProcessLookupError):
        pass

    if process_group is not None:
        try:
            os.killpg(process_group, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    elif process.is_alive():
        try:
            process.terminate()
        except (OSError, AttributeError):
            pass

    process.join(timeout=1.0)

    # Группа может продолжать существовать после выхода её лидера, поэтому
    # SIGKILL отправляется независимо от process.is_alive().
    if process_group is not None:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    elif process.is_alive():
        try:
            process.kill()
        except (OSError, AttributeError):
            pass
    process.join(timeout=2.0)


def _run_isolated(
    entry: FileEntry,
    settings: ScanSettings,
    timeout: int,
    cancel_event: Event,
):
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_isolated_process_target,
        args=(send_connection, entry, settings, _OCR_LIMIT_PER_PROCESS),
        name=f"dsp-reader-{entry.extension.lstrip('.')}",
    )

    acquired = False
    try:
        while not acquired:
            if cancel_event.is_set():
                return [], None, "Операция отменена", False
            acquired = _RISKY_SEMAPHORE.acquire(timeout=0.1)

        if cancel_event.is_set():
            return [], None, "Операция отменена", False

        process.start()
        send_connection.close()
        deadline = time.monotonic() + max(1, timeout)
        while True:
            if receive_connection.poll(0.1):
                try:
                    status, payload = receive_connection.recv()
                except EOFError:
                    return [], None, "Изолированный процесс завершился без результата", False
                process.join(timeout=1.0)
                if process.is_alive():
                    _terminate_process_tree(process)
                if status == "ok":
                    return payload
                return [], None, str(payload), False

            if not process.is_alive():
                # После выхода процесса данные могут появиться в pipe чуть позже.
                if receive_connection.poll(0.2):
                    try:
                        status, payload = receive_connection.recv()
                        if status == "ok":
                            return payload
                        return [], None, str(payload), False
                    except EOFError:
                        pass
                return [], None, f"Процесс чтения аварийно завершён (код {process.exitcode})", False

            if cancel_event.is_set():
                _terminate_process_tree(process)
                return [], None, "Операция отменена", False

            if time.monotonic() >= deadline:
                _terminate_process_tree(process)
                return [], None, f"{TIMEOUT_MARKER} ({timeout} с) — процесс остановлен", False
    except Exception as exc:
        if process.is_alive():
            _terminate_process_tree(process)
        return [], None, f"Не удалось запустить изолированный процесс: {exc}", False
    finally:
        try:
            receive_connection.close()
        except Exception:
            pass
        try:
            send_connection.close()
        except Exception:
            pass
        if acquired:
            _RISKY_SEMAPHORE.release()


class DocumentScanner:
    """Выполняет полный цикл: поиск файлов → чтение → поиск слов → отчёт."""

    def run(
        self,
        settings: ScanSettings,
        cancel_event: Optional[Event] = None,
        on_progress: Optional[ProgressCallback] = None,
        on_result: Optional[ResultCallback] = None,
        on_results: Optional[ResultsCallback] = None,
        on_log: Optional[LogCallback] = None,
        on_stats: Optional[StatsCallback] = None,
    ) -> ScanReport:
        start = time.monotonic()
        app_settings = load_settings()
        stats = ScanStats()
        results: list[SearchResult] = []
        errors: list[ErrorEntry] = []
        unreadable_by_path: dict[str, ErrorEntry] = {}
        cancel_event = cancel_event or Event()

        def log(level: str, message: str, file_path: str = "") -> ErrorEntry | None:
            error_entry: ErrorEntry | None = None
            if level in {"WARNING", "ERROR"}:
                error_entry = ErrorEntry(
                    file_path=file_path,
                    level=level,
                    message=message,
                    timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                errors.append(error_entry)
            if on_log:
                on_log(level, message)
            return error_entry

        def mark_unreadable(level: str, message: str, file_path: str) -> None:
            """Фиксирует только окончательную невозможность прочитать файл.

            Для эвристического DOC запись выполняется после резервного прохода,
            поэтому успешно восстановленные внешней программой файлы сюда не
            попадают. Повторная запись того же пути заменяет предыдущую более
            свежей причиной.
            """
            error_entry = log(level, message, file_path)
            if error_entry is not None and file_path:
                unreadable_by_path[file_path] = error_entry

        validation_errors = settings.validate()
        if validation_errors:
            for message in validation_errors:
                log("ERROR", message)
            stats.errors = len(validation_errors)
            return ScanReport(
                errors=errors,
                stats=stats,
                elapsed_seconds=time.monotonic() - start,
                doc_read_method=settings.doc_read_method.value,
            )

        def on_skip(path: str, reason: str) -> None:
            if reason == "temp":
                stats.temp_files += 1
            stats.skipped += 1
            if reason in {"missing_or_inaccessible_root", "inaccessible_directory", "not_regular"}:
                log("WARNING", f"Путь пропущен ({reason}): {path}", path)

        discovery_size_limit = (
            max(settings.max_file_size_bytes, 2**63 - 1)
            if settings.filename_words else settings.max_file_size_bytes
        )
        max_workers = max(1, min(settings.max_workers or app_settings.max_workers, 128))
        discovered_count = 0
        discovery_done = False
        discovery_queue: Queue[tuple[str, object, object | None]] = Queue(
            maxsize=max(4, max_workers * 4)
        )

        def put_discovery_event(event: tuple[str, object, object | None]) -> bool:
            while not cancel_event.is_set():
                try:
                    discovery_queue.put(event, timeout=0.1)
                    return True
                except Full:
                    continue
            return False

        def discover_files() -> None:
            try:
                def queue_skip(path: str, reason: str) -> None:
                    put_discovery_event(("skip", path, reason))

                for entry in iter_files_safe(
                    settings.paths,
                    settings.file_types,
                    discovery_size_limit,
                    cancel_event=cancel_event,
                    include_empty=bool(settings.filename_words),
                    on_skip=queue_skip,
                ):
                    if not put_discovery_event(("entry", entry, None)):
                        return
            except BaseException as exc:
                put_discovery_event(
                    ("error", f"Ошибка обхода файлов: {type(exc).__name__}: {exc}", None)
                )
            finally:
                put_discovery_event(("done", None, None))

        discovery_thread = Thread(
            target=discover_files,
            name="dsp-discovery",
            daemon=True,
        )
        discovery_thread.start()
        if on_progress:
            on_progress(0, 0, "Поиск файлов и запуск обработки…")
        per_file_timeout = app_settings.per_file_timeout
        processed_count = 0
        pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dsp-file")
        cancelled_pool = False
        large_file_slots = BoundedSemaphore(max(1, min(4, max_workers)))
        huge_file_slot = BoundedSemaphore(1)
        heuristic_retry_candidates: list[tuple[FileEntry, str]] = []

        def read_entry(entry: FileEntry, active_settings: ScanSettings = settings):
            if cancel_event.is_set():
                return [], None, "Операция отменена", False
            isolate = bool(active_settings.words) and (
                entry.extension in _RISKY_EXTENSIONS
                or (entry.extension == ".docx" and entry.size >= _ISOLATE_DOCX_ABOVE_BYTES)
            )
            if isolate:
                return _run_isolated(entry, active_settings, per_file_timeout, cancel_event)
            return self._process_one_direct(entry, active_settings)

        def acquire_slot(semaphore: BoundedSemaphore) -> bool:
            while not cancel_event.is_set():
                if semaphore.acquire(timeout=0.1):
                    return True
            return False

        def process_entry(entry: FileEntry, active_settings: ScanSettings = settings):
            # Ограничиваем одновременное декодирование крупных документов:
            # строка Unicode и структуры парсера могут занимать в несколько
            # раз больше размера файла на диске. Ожидание слота отменяемо.
            slots: list[BoundedSemaphore] = []
            required = []
            if entry.size >= 100 * 1024 * 1024:
                required = [huge_file_slot, large_file_slots]
            elif entry.size >= 32 * 1024 * 1024:
                required = [large_file_slots]
            try:
                for semaphore in required:
                    if not acquire_slot(semaphore):
                        return [], None, "Операция отменена", False
                    slots.append(semaphore)
                return read_entry(entry, active_settings)
            finally:
                for semaphore in reversed(slots):
                    semaphore.release()

        def emit_results(file_results) -> None:
            if not file_results:
                return
            # Результаты одного файла передаются в GUI единым пакетом сразу
            # после завершения обработки этого файла. Это даёт потоковое
            # отображение без ожидания конца всего сканирования и при этом не
            # создаёт отдельный Qt-сигнал на каждое совпадение.
            file_results = list(file_results)
            results.extend(file_results)
            stats.found += len(file_results)
            if on_results:
                on_results(file_results)
            for result in file_results:
                if on_result:
                    on_result(result)

        def record_problem(entry: FileEntry, warning, error, locked) -> None:
            if locked:
                stats.locked += 1
                mark_unreadable(
                    "WARNING",
                    f"{entry.path.name}: файл заблокирован или недоступен для чтения",
                    str(entry.path),
                )
            elif error:
                if error != "Операция отменена":
                    stats.errors += 1
                    lowered = error.casefold()
                    if "повреж" in lowered or "некоррект" in lowered or "аварийно" in lowered:
                        stats.corrupted += 1
                    level = "WARNING" if TIMEOUT_MARKER in error else "ERROR"
                    mark_unreadable(level, f"{entry.path.name}: {error}", str(entry.path))
            if warning:
                log("WARNING", f"{entry.path.name}: {warning}", str(entry.path))

        def should_retry_after_heuristic(entry: FileEntry, error, locked: bool) -> bool:
            return bool(
                error
                and error != "Операция отменена"
                and not locked
                and settings.words
                and entry.extension == ".doc"
                and settings.doc_read_method == DocReadMethod.HEURISTIC
                and settings.use_external_programs_after_heuristic_failure
            )

        def handle_done(entry: FileEntry, file_results, warning, error, locked) -> None:
            nonlocal processed_count
            processed_count += 1
            stats.processed += 1
            field = _EXT_STAT_FIELD.get(entry.extension)
            if field:
                setattr(stats, field, getattr(stats, field) + 1)

            # Совпадения в имени доступны независимо от ошибки чтения и сразу
            # попадают в результаты. На втором проходе filename_words очищены,
            # поэтому дублирования не будет.
            emit_results(file_results)

            if should_retry_after_heuristic(entry, error, locked):
                heuristic_retry_candidates.append((entry, str(error)))
                if on_log:
                    on_log(
                        "INFO",
                        f"{entry.path.name}: быстрая эвристика не распознала файл; "
                        "он поставлен в очередь повторного чтения внешними программами",
                    )
            else:
                record_problem(entry, warning, error, locked)
            if on_progress:
                on_progress(processed_count, max(discovered_count, processed_count), entry.path.name)
            if on_stats:
                on_stats(stats)

        try:
            # Не создаём Future для каждого файла сразу: на деревьях с сотнями
            # тысяч файлов это заметно экономит память и делает отмену быстрой.
            future_map = {}
            max_in_flight = max_workers * 2

            def consume_discovery_event(*, wait: bool) -> bool:
                nonlocal discovered_count, discovery_done
                try:
                    event_type, payload, extra = discovery_queue.get(
                        timeout=0.05 if wait else 0
                    )
                except Empty:
                    return False

                if event_type == "entry":
                    entry = payload
                    if isinstance(entry, FileEntry):
                        discovered_count += 1
                        future_map[pool.submit(process_entry, entry)] = entry
                        if on_progress and (
                            discovered_count == 1 or discovered_count % 25 == 0
                        ):
                            on_progress(
                                processed_count,
                                discovered_count,
                                f"Найдено файлов: {discovered_count}",
                            )
                elif event_type == "skip":
                    on_skip(str(payload), str(extra or "unknown"))
                elif event_type == "error":
                    log("ERROR", str(payload))
                    stats.errors += 1
                elif event_type == "done":
                    discovery_done = True
                return True

            while future_map or not discovery_done:
                while (
                    len(future_map) < max_in_flight
                    and not discovery_done
                    and not cancel_event.is_set()
                ):
                    if not consume_discovery_event(wait=not future_map):
                        break

                if future_map:
                    done, _ = futures_wait(
                        set(future_map), timeout=0.10, return_when=FIRST_COMPLETED,
                    )
                    for future in done:
                        entry = future_map.pop(future)
                        try:
                            outcome = future.result()
                        except Exception as exc:
                            outcome = ([], None, f"Внутренняя ошибка: {exc}", False)
                        handle_done(entry, *outcome)
                elif not discovery_done:
                    consume_discovery_event(wait=True)

                if cancel_event.is_set():
                    for future in future_map:
                        future.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    cancelled_pool = True
                    break
        finally:
            if not cancelled_pool:
                pool.shutdown(wait=True)
            discovery_thread.join(timeout=1.0)
            try:
                _release_word_all()
            except Exception:
                pass

        # Второй проход выполняется строго после завершения всей быстрой
        # эвристики и только для legacy .doc, которые вернули ошибку. Файлы,
        # прочитанные успешно, повторно не открываются.
        if heuristic_retry_candidates and not cancel_event.is_set():
            retry_count = len(heuristic_retry_candidates)
            retry_total = discovered_count + retry_count
            retry_progress = discovered_count
            if on_progress:
                on_progress(
                    retry_progress,
                    retry_total,
                    f"Быстрая эвристика завершена. Повторный проход: {retry_count} файл(ов)",
                )

            if not DocReader.external_programs_may_be_available():
                if on_log:
                    on_log(
                        "WARNING",
                        "Повторный проход не запущен: LibreOffice не настроен, "
                        "а MS Word COM доступен только на Windows",
                    )
                for entry, initial_error in heuristic_retry_candidates:
                    retry_progress += 1
                    record_problem(
                        entry,
                        None,
                        f"Быстрая эвристика: {initial_error}; "
                        "повторный проход: доступные внешние программы не найдены",
                        False,
                    )
                    if on_progress:
                        on_progress(retry_progress, retry_total, f"Внешний проход недоступен: {entry.path.name}")
            else:
                retry_settings = replace(
                    settings,
                    doc_read_method=DocReadMethod.AUTO,
                    filename_words=[],
                    doc_external_only=True,
                )
                if on_log:
                    on_log(
                        "INFO",
                        f"Запущен повторный проход внешними программами для {retry_count} "
                        "нераспознанных .doc",
                    )

                retry_workers = max(1, min(max_workers, retry_count, _RISKY_PROCESS_LIMIT))
                retry_pool = ThreadPoolExecutor(
                    max_workers=retry_workers,
                    thread_name_prefix="dsp-doc-external",
                )
                retry_cancelled_pool = False
                try:
                    retry_iter = iter(heuristic_retry_candidates)
                    retry_future_map = {}
                    retry_max_in_flight = retry_workers * 2

                    def submit_more_retries() -> None:
                        while (
                            len(retry_future_map) < retry_max_in_flight
                            and not cancel_event.is_set()
                        ):
                            try:
                                entry, initial_error = next(retry_iter)
                            except StopIteration:
                                break
                            retry_future_map[
                                retry_pool.submit(process_entry, entry, retry_settings)
                            ] = (entry, initial_error)

                    submit_more_retries()
                    while retry_future_map:
                        done, _ = futures_wait(
                            set(retry_future_map), timeout=0.25, return_when=FIRST_COMPLETED,
                        )
                        for future in done:
                            entry, initial_error = retry_future_map.pop(future)
                            try:
                                file_results, warning, error, locked = future.result()
                            except Exception as exc:
                                file_results, warning, error, locked = (
                                    [], None, f"Внутренняя ошибка повторного прохода: {exc}", False,
                                )

                            emit_results(file_results)
                            if error == "Операция отменена":
                                pass
                            elif locked:
                                stats.locked += 1
                                mark_unreadable(
                                    "WARNING",
                                    f"{entry.path.name}: быстрая эвристика: {initial_error}; "
                                    "внешний проход: файл заблокирован",
                                    str(entry.path),
                                )
                            elif error:
                                record_problem(
                                    entry,
                                    warning,
                                    f"Быстрая эвристика: {initial_error}; внешние программы: {error}",
                                    False,
                                )
                            else:
                                if warning:
                                    log("WARNING", f"{entry.path.name}: {warning}", str(entry.path))
                                if on_log:
                                    on_log(
                                        "INFO",
                                        f"{entry.path.name}: успешно распознан внешней программой "
                                        "во втором проходе",
                                    )

                            retry_progress += 1
                            if on_progress:
                                on_progress(
                                    retry_progress,
                                    retry_total,
                                    f"Внешний повторный проход: {entry.path.name}",
                                )

                        if cancel_event.is_set():
                            for future in retry_future_map:
                                future.cancel()
                            retry_pool.shutdown(wait=False, cancel_futures=True)
                            retry_cancelled_pool = True
                            break
                        submit_more_retries()
                finally:
                    if not retry_cancelled_pool:
                        retry_pool.shutdown(wait=True)
                    try:
                        _release_word_all()
                    except Exception:
                        pass

        return ScanReport(
            results=results,
            errors=errors,
            unreadable_files=list(unreadable_by_path.values()),
            stats=stats,
            elapsed_seconds=time.monotonic() - start,
            doc_read_method=settings.doc_read_method.value,
            cancelled=cancel_event.is_set(),
        )

    @staticmethod
    def _process_one_direct(entry: FileEntry, settings: ScanSettings):
        modified = datetime.fromtimestamp(entry.modified).strftime("%Y-%m-%d %H:%M:%S")
        name_results: list[SearchResult] = []
        if settings.filename_words:
            name_matches = find_matches(
                entry.path.name,
                settings.filename_words,
                case_sensitive=settings.case_sensitive,
                whole_word=settings.whole_word,
                context_chars=settings.context_chars,
                max_matches_per_word=settings.max_matches_per_word,
            )
            name_results = [
                SearchResult(
                    file_name=entry.path.name,
                    full_path=str(entry.path),
                    word=match.word,
                    context=f"[Имя файла] {entry.path.name}",
                    file_type=entry.extension.lstrip("."),
                    modified=modified,
                    occurrence_index=index + 1,
                    matched_text=match.matched_text,
                    tooltip_context=entry.path.name,
                    detail_context=entry.path.name,
                )
                for index, match in enumerate(name_matches)
            ]

        # При пустом поле терминов для содержимого файл вообще не открывается.
        # Это существенно ускоряет сценарий поиска только по названиям и
        # исключает лишние ошибки ридеров/OCR.
        if not settings.words:
            return name_results, None, None, False
        if entry.size > settings.max_file_size_bytes:
            return (
                name_results,
                f"Содержимое пропущено: размер файла превышает {settings.max_file_size_mb:g} МБ",
                None,
                False,
            )
        if entry.size == 0:
            return name_results, None, None, False

        outcome = extract_text(entry.path, entry.extension, settings)
        if outcome.locked:
            return name_results, None, None, True
        if outcome.error:
            return name_results, None, outcome.error, False
        if not outcome.text:
            return name_results, outcome.warning, None, False

        matches = find_matches(
            outcome.text,
            settings.words,
            case_sensitive=settings.case_sensitive,
            whole_word=settings.whole_word,
            context_chars=settings.context_chars,
            max_matches_per_word=settings.max_matches_per_word,
        )
        content_results = [
            SearchResult(
                file_name=entry.path.name,
                full_path=str(entry.path),
                word=match.word,
                context=match.context,
                file_type=entry.extension.lstrip("."),
                modified=modified,
                occurrence_index=index + 1,
                matched_text=match.matched_text,
                tooltip_context=match.tooltip_context,
                detail_context=match.detail_context,
            )
            for index, match in enumerate(matches)
        ]
        return name_results + content_results, outcome.warning, None, False
