"""Текстовый поисковый движок."""
from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, List

from app.config import MAX_MATCHES_PER_WORD_PER_FILE

# Дочерний логгер "dspscanner": пишет в тот же файл журнала и вкладку "Журнал"
# GUI, что и остальное приложение (см. app/logging_utils.py).
_logger = logging.getLogger("dspscanner.search_engine")


@dataclass
class Match:
    word: str
    matched_text: str
    context: str
    position: int
    tooltip_context: str = ""
    detail_context: str = ""


@dataclass(frozen=True)
class _AhoAutomaton:
    transitions: tuple[dict[str, int], ...]
    failures: tuple[int, ...]
    outputs: tuple[tuple[int, ...], ...]
    lengths: tuple[int, ...]


_WILDCARD_GAP = r"[\w\-]{0,30}"
_WHITESPACE_RE = re.compile(r"\s+")
_AHO_MIN_TERMS = 32
_MIN_TOOLTIP_WORDS_EACH_SIDE = 30
_DETAIL_CONTEXT_MULTIPLIER = 1.5
_TOOLTIP_SCAN_CHARS_MIN = 8_000
_TOOLTIP_SCAN_CHARS_MAX = 100_000
_NONSPACE_RE = re.compile(r"\S+")


@lru_cache(maxsize=1024)
def _build_pattern(word: str, case_sensitive: bool, whole_word: bool) -> re.Pattern:
    term = re.sub(r"\*+", "*", word.strip())
    if not term.replace("*", ""):
        # Нельзя компилировать пустой шаблон: он совпадает в каждой позиции.
        raise ValueError("Термин состоит только из wildcard-символов")

    escaped_parts = []
    for part in term.split("*"):
        escaped = re.escape(part.strip())
        escaped = escaped.replace(r"\ ", r"\s+").replace(" ", r"\s+")
        escaped_parts.append(escaped)

    pattern = _WILDCARD_GAP.join(escaped_parts)
    if not case_sensitive:
        pattern = re.sub(r"[еёЕЁ]", "[её]", pattern)
    if whole_word:
        pattern = rf"\b{pattern}\b"

    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(pattern, flags)


def _unique_words(words: Iterable[str], case_sensitive: bool) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw_word in words:
        word = str(raw_word).strip()
        if not word or not word.replace("*", ""):
            continue
        key = word if case_sensitive else word.casefold().replace("ё", "е")
        if key in seen:
            continue
        seen.add(key)
        unique.append(word)
    return unique


def _literal_key(word: str, case_sensitive: bool, whole_word: bool) -> str | None:
    """Возвращает безопасный для Aho–Corasick литерал или None.

    Фразы с пробелами остаются в regex-ветке, поскольку исходная семантика
    допускает любое количество/вид пробельных символов (``\\s+``).
    """
    if "*" in word or any(char.isspace() for char in word):
        return None
    key = word if case_sensitive else word.casefold().replace("ё", "е")
    if len(key) != len(word):
        return None
    if whole_word and (not _is_word_char(word[0]) or not _is_word_char(word[-1])):
        return None
    return key


def _is_word_char(char: str) -> bool:
    return char == "_" or char.isalnum()


@lru_cache(maxsize=64)
def _build_aho(patterns: tuple[str, ...]) -> _AhoAutomaton:
    transitions: list[dict[str, int]] = [{}]
    failures = [0]
    outputs: list[list[int]] = [[]]

    for pattern_index, pattern in enumerate(patterns):
        state = 0
        for char in pattern:
            next_state = transitions[state].get(char)
            if next_state is None:
                next_state = len(transitions)
                transitions[state][char] = next_state
                transitions.append({})
                failures.append(0)
                outputs.append([])
            state = next_state
        outputs[state].append(pattern_index)

    queue = deque(transitions[0].values())
    while queue:
        state = queue.popleft()
        for char, next_state in transitions[state].items():
            queue.append(next_state)
            fallback = failures[state]
            while fallback and char not in transitions[fallback]:
                fallback = failures[fallback]
            failures[next_state] = transitions[fallback].get(char, 0)
            outputs[next_state].extend(outputs[failures[next_state]])

    return _AhoAutomaton(
        transitions=tuple(transitions),
        failures=tuple(failures),
        outputs=tuple(tuple(items) for items in outputs),
        lengths=tuple(len(pattern) for pattern in patterns),
    )


def _find_aho_positions(
    text: str,
    patterns: tuple[str, ...],
    case_sensitive: bool,
    whole_word: bool,
    max_matches_per_word: int,
) -> list[list[tuple[int, int]]]:
    normalized = text if case_sensitive else text.casefold().replace("ё", "е")
    # Некоторые Unicode-символы (например, ß) меняют длину при casefold().
    # В таком случае позиции уже не совпадают с исходной строкой — используем regex.
    if len(normalized) != len(text):
        _logger.debug(
            "Aho-Corasick: casefold() изменил длину текста (%d -> %d), "
            "откат на regex-поиск для %d термина(ов)",
            len(text), len(normalized), len(patterns),
        )
        return []

    automaton = _build_aho(patterns)
    found: list[list[tuple[int, int]]] = [[] for _ in patterns]
    saturated = 0
    state = 0

    for end, char in enumerate(normalized):
        while state and char not in automaton.transitions[state]:
            state = automaton.failures[state]
        state = automaton.transitions[state].get(char, 0)
        for pattern_index in automaton.outputs[state]:
            bucket = found[pattern_index]
            if len(bucket) >= max_matches_per_word:
                continue
            start = end - automaton.lengths[pattern_index] + 1
            # Совпадения одного паттерна поступают в порядке возрастания end
            # (длина фиксирована, значит и start не убывает), поэтому отсекаем
            # пересекающиеся вхождения сравнением с последним принятым. Это
            # повторяет not-overlapping семантику re.finditer regex-ветки:
            # без отсечения счёт совпадений файла зависел бы от того, набрался
            # ли порог _AHO_MIN_TERMS.
            if bucket and start < bucket[-1][1]:
                continue
            if whole_word:
                if start > 0 and _is_word_char(text[start - 1]):
                    continue
                if end + 1 < len(text) and _is_word_char(text[end + 1]):
                    continue
            bucket.append((start, end + 1))
            if len(bucket) == max_matches_per_word:
                saturated += 1
        if saturated == len(patterns):
            break
    return found


def _expanded_context_pair(
    text: str,
    start_pos: int,
    end_pos: int,
    context_chars: int,
) -> tuple[str, str]:
    """Возвращает hover- и detail-контексты за один разбор локального окна.

    Подсказка содержит не менее 30 слов с каждой стороны либо значение поля
    «Контекст», если оно больше. Детальный фрагмент на 50% шире. Один общий
    поиск токенов предотвращает двойную нагрузку на больших документах.
    """
    hover_words = max(_MIN_TOOLTIP_WORDS_EACH_SIDE, int(context_chars))
    detail_words = max(hover_words + 1, int(round(hover_words * _DETAIL_CONTEXT_MULTIPLIER)))

    containing_start = start_pos
    while containing_start > 0 and not text[containing_start - 1].isspace():
        containing_start -= 1
    containing_end = end_pos
    while containing_end < len(text) and not text[containing_end].isspace():
        containing_end += 1

    scan_chars = min(
        _TOOLTIP_SCAN_CHARS_MAX,
        max(_TOOLTIP_SCAN_CHARS_MIN, detail_words * 80),
    )
    left_scan_start = max(0, containing_start - scan_chars)
    right_scan_end = min(len(text), containing_end + scan_chars)
    left_tokens = list(_NONSPACE_RE.finditer(text[left_scan_start:containing_start]))
    right_tokens = list(_NONSPACE_RE.finditer(text[containing_end:right_scan_end]))

    char_start = max(0, start_pos - context_chars)
    char_end = min(len(text), end_pos + context_chars)

    def build(words_each_side: int) -> str:
        if len(left_tokens) >= words_each_side:
            word_start = left_scan_start + left_tokens[-words_each_side].start()
        else:
            word_start = left_scan_start
        if len(right_tokens) >= words_each_side:
            word_end = containing_end + right_tokens[words_each_side - 1].end()
        else:
            word_end = right_scan_end
        start = min(char_start, word_start)
        end = max(char_end, word_end)
        snippet = _WHITESPACE_RE.sub(" ", text[start:end]).strip()
        return f"{'…' if start > 0 else ''}{snippet}{'…' if end < len(text) else ''}"

    return build(hover_words), build(detail_words)


def _to_match(text: str, word: str, start_pos: int, end_pos: int, context_chars: int) -> Match:
    tooltip_context, detail_context = _expanded_context_pair(
        text, start_pos, end_pos, context_chars
    )
    # Короткий контекст для колонки «Контекст» — центрирован на совпадении,
    # чтобы найденное слово всегда было в поле зрения пользователя.
    char_start = max(0, start_pos - context_chars)
    char_end = min(len(text), end_pos + context_chars)
    short_context = _WHITESPACE_RE.sub(" ", text[char_start:char_end]).strip()
    if char_start > 0:
        short_context = "…" + short_context
    if char_end < len(text):
        short_context = short_context + "…"
    return Match(
        word=word,
        matched_text=text[start_pos:end_pos],
        context=short_context,
        position=start_pos,
        tooltip_context=tooltip_context,
        detail_context=detail_context,
    )


def find_matches(
    text: str,
    words: Iterable[str],
    case_sensitive: bool = False,
    whole_word: bool = False,
    context_chars: int = 150,
    max_matches_per_word: int = MAX_MATCHES_PER_WORD_PER_FILE,
) -> List[Match]:
    if not text:
        return []

    context_chars = max(0, int(context_chars))
    max_matches_per_word = max(1, int(max_matches_per_word))
    unique_words = _unique_words(words, case_sensitive)
    if not unique_words:
        return []

    by_word: dict[str, list[Match]] = {word: [] for word in unique_words}
    literal_words: list[str] = []
    literal_patterns: list[str] = []
    for word in unique_words:
        key = _literal_key(word, case_sensitive, whole_word)
        if key is not None:
            literal_words.append(word)
            literal_patterns.append(key)

    use_aho = len(literal_words) >= _AHO_MIN_TERMS
    if use_aho:
        positions = _find_aho_positions(
            text,
            tuple(literal_patterns),
            case_sensitive,
            whole_word,
            max_matches_per_word,
        )
        # Пустой список означает, что casefold изменил длину строки.
        if positions:
            for word, word_positions in zip(literal_words, positions):
                by_word[word] = [
                    _to_match(text, word, start, end, context_chars)
                    for start, end in word_positions
                ]
        else:
            use_aho = False

    aho_words = set(literal_words) if use_aho else set()
    for word in unique_words:
        if word in aho_words:
            continue
        try:
            pattern = _build_pattern(word, case_sensitive, whole_word)
        except (ValueError, re.error):
            continue
        bucket = by_word[word]
        for count, match in enumerate(pattern.finditer(text)):
            if count >= max_matches_per_word:
                break
            bucket.append(_to_match(text, word, match.start(), match.end(), context_chars))

    # Сохраняем прежний порядок: сначала все совпадения первого термина,
    # затем второго и т.д., независимо от выбранного алгоритма.
    return [match for word in unique_words for match in by_word[word]]
