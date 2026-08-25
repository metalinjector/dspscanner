"""Поисковый движок: границы слов, wildcard, ё-свёртка, две ветки поиска."""
from __future__ import annotations

import pytest

from app.scanning.search_engine import _AHO_MIN_TERMS, find_matches
from app.scanning.term_parser import parse_terms

PUNCT_TEXT = "Приказ № 42 и язык C++, а также (проект) документа. тел. 123"


@pytest.mark.parametrize("term", ["№ 42", "C++", "(проект)", "тел."])
def test_whole_word_finds_terms_bounded_by_punctuation(term):
    """\\b есть только на стыке \\w и \\W — такие термины раньше терялись."""
    matches = find_matches(PUNCT_TEXT, [term], whole_word=True)
    assert [m.matched_text for m in matches] == [term]


def test_whole_word_still_requires_absence_of_word_chars():
    assert find_matches("xC++x", ["C++"], whole_word=True) == []
    assert find_matches("язык C++.", ["C++"], whole_word=True)


def test_whole_word_keeps_plain_word_semantics():
    text = "договор Договор поддоговор договоры"
    whole = [m.matched_text for m in find_matches(text, ["договор"], whole_word=True)]
    partial = [m.matched_text for m in find_matches(text, ["договор"], whole_word=False)]
    assert whole == ["договор", "Договор"]
    assert len(partial) == 4


def test_substring_match_is_default():
    assert [m.matched_text for m in find_matches("беспилотник", ["пилот"])] == ["пилот"]


def test_yo_is_folded_when_case_insensitive():
    found = {m.matched_text for m in find_matches("ёжик Ежик ЁЖИК", ["ежик"])}
    assert found == {"ёжик", "Ежик", "ЁЖИК"}


def test_case_sensitive_search_keeps_register():
    assert [m.matched_text for m in find_matches("Договор договор", ["Договор"], case_sensitive=True)] == [
        "Договор"
    ]


def test_wildcard_expands_within_word():
    found = [m.matched_text for m in find_matches("договор договорной вагон", ["дог*"])]
    assert found == ["договор", "договорной"]


def test_phrase_tolerates_any_whitespace():
    assert find_matches("служебная\n  записка", ["служебная записка"])


def test_terms_made_only_of_wildcards_are_ignored():
    assert find_matches("любой текст", ["***"]) == []


def test_max_matches_per_word_is_honoured():
    text = "договор " * 100
    assert len(find_matches(text, ["договор"], max_matches_per_word=7)) == 7


def _normalize(matches):
    return sorted((m.word, m.matched_text, m.position) for m in matches)


@pytest.mark.parametrize("case_sensitive", [False, True])
@pytest.mark.parametrize("whole_word", [False, True])
def test_regex_and_aho_branches_agree(case_sensitive, whole_word):
    """Свыше _AHO_MIN_TERMS литералов включается Aho-Corasick.

    Обе ветки обязаны давать идентичный результат, иначе выдача менялась бы
    от одного лишь количества терминов в списке.
    """
    text = ("Служебная записка о договоре. ДОГОВОР №12/ё, ежевика, Ежик; "
            "приказ, приказной, ПРИКАЗ. " * 20)
    base = ["договор", "ежик", "приказ", "записка"]
    filler = [f"термин{i}" for i in range(_AHO_MIN_TERMS + 8)]

    few = _normalize(find_matches(text, base, case_sensitive=case_sensitive, whole_word=whole_word))
    many = _normalize(find_matches(text, base + filler, case_sensitive=case_sensitive, whole_word=whole_word))
    assert few == [item for item in many if item[0] in base]


def test_parse_terms_keeps_phrases_and_drops_duplicates():
    assert parse_terms("служебная записка, договор\nДОГОВОР; ёжик,ежик") == [
        "служебная записка",
        "договор",
        "ёжик",
    ]


def test_context_contains_the_match():
    text = "начало " * 50 + "СЕКРЕТ" + " конец" * 50
    match = find_matches(text, ["СЕКРЕТ"], context_chars=40)[0]
    assert "СЕКРЕТ" in match.context
    assert "СЕКРЕТ" in match.tooltip_context
    assert len(match.detail_context) >= len(match.tooltip_context)


def test_tooltip_context_keeps_paragraph_breaks():
    """Пустая строка документа должна остаться пустой строкой в подсказке.

    Полная нормализация пробелов склеивала абзацы: фрагменты из разных мест
    страницы выглядели одним предложением. Колонка «Контекст» при этом
    остаётся однострочной — там перенос сломал бы строку таблицы.
    """
    from app.scanning.search_engine import find_matches

    text = (
        "Для служебного пользования, беспилотники.\n\n\n"
        "Лекарственные поражения печени является одной из наиболее частых причин."
    )
    match = find_matches(text, ["беспилотники"], context_chars=50)[0]

    assert "беспилотники.\n\nЛекарственные" in match.tooltip_context
    assert "\n\n" in match.detail_context
    # Три и более переводов подряд сводятся к одной пустой строке.
    assert "\n\n\n" not in match.tooltip_context
    # Короткий контекст таблицы остаётся в одну строку.
    assert "\n" not in match.context


def test_tooltip_context_collapses_spaces_inside_a_line():
    """Внутри абзаца лишние пробелы и переносы строк по-прежнему схлопываются."""
    from app.scanning.search_engine import find_matches

    text = "начало   строки\tсекрет   и\r\nпродолжение того же абзаца"
    match = find_matches(text, ["секрет"], context_chars=80)[0]

    assert "   " not in match.tooltip_context
    assert "\t" not in match.tooltip_context
    assert "\r" not in match.tooltip_context
    # Одиночный перенос сохраняется как перенос, а не как пустая строка.
    assert "и\nпродолжение" in match.tooltip_context


def test_context_window_matches_the_full_scan_implementation():
    """Адаптивное окно обязано давать ровно тот же контекст, что и полный разбор.

    Раньше для каждого совпадения токенизировалось фиксированное окно в
    _TOOLTIP_SCAN_CHARS_MIN символов с каждой стороны. Теперь окно растёт по
    необходимости — результат должен совпадать посимвольно, иначе экономия
    куплена ценой другого текста в подсказке.
    """
    import random
    import re

    from app.scanning import search_engine as se

    def reference(text, start_pos, end_pos, context_chars):
        """Прежняя реализация: разбирает окно целиком."""
        hover = max(se._MIN_TOOLTIP_WORDS_EACH_SIDE, int(context_chars))
        detail = max(hover + 1, int(round(hover * se._DETAIL_CONTEXT_MULTIPLIER)))
        containing_start = start_pos
        while containing_start > 0 and not text[containing_start - 1].isspace():
            containing_start -= 1
        containing_end = end_pos
        while containing_end < len(text) and not text[containing_end].isspace():
            containing_end += 1
        scan = min(
            se._TOOLTIP_SCAN_CHARS_MAX,
            max(se._TOOLTIP_SCAN_CHARS_MIN, detail * 80),
        )
        left_start = max(0, containing_start - scan)
        right_end = min(len(text), containing_end + scan)
        left = list(re.finditer(r"\S+", text[left_start:containing_start]))
        right = list(re.finditer(r"\S+", text[containing_end:right_end]))
        char_start = max(0, start_pos - context_chars)
        char_end = min(len(text), end_pos + context_chars)

        def build(count):
            word_start = (
                left_start + left[-count].start() if len(left) >= count else left_start
            )
            word_end = (
                containing_end + right[count - 1].end()
                if len(right) >= count
                else right_end
            )
            start = min(char_start, word_start)
            end = max(char_end, word_end)
            snippet = se._normalize_block(text[start:end])
            head = "…" if start > 0 else ""
            tail = "…" if end < len(text) else ""
            return f"{head}{snippet}{tail}"

        return build(hover), build(detail)

    random.seed(20260825)
    words = ["слово", "договор", "текст", "№42", "оченьдлинноеслово" * 3]
    separators = [" ", "  ", "\n", "\n\n", "\t", "\r\n"]

    for _ in range(150):
        text = "".join(
            random.choice(words) + random.choice(separators)
            for _ in range(random.choice([5, 40, 300]))
        )
        start = random.randrange(0, max(1, len(text) - 1))
        end = min(len(text), start + random.randrange(1, 10))
        context_chars = random.choice([0, 40, 150, 400])
        assert se._expanded_context_pair(text, start, end, context_chars) == reference(
            text, start, end, context_chars
        )


def test_context_window_handles_text_without_spaces():
    """Текст без пробелов не должен зацикливать расширение окна.

    Окно растёт, пока не наберётся нужное число слов; в base64-подобном
    документе слов нет вовсе, и выходом служит только жёсткая граница.
    """
    from app.scanning.search_engine import _expanded_context_pair

    blob = "A" * 40_000
    hover, detail = _expanded_context_pair(blob, 20_000, 20_005, 40)

    assert hover and detail
    assert len(detail) >= len(hover)


def test_expanded_context_does_not_tokenize_the_whole_window():
    """Контекст разбирает столько текста, сколько нужно, а не фиксированное окно.

    Это и делало сканирование медленным: на каждое совпадение токенизировалось
    по _TOOLTIP_SCAN_CHARS_MIN символов с каждой стороны (8000 + 8000), хотя
    для 60 слов хватает нескольких сотен. Проверка считает реально
    просканированные символы, а не время: результат не зависит от нагрузки
    машины и одинаков в CI.
    """
    from app.scanning import search_engine as se

    scanned = []
    original = se._NONSPACE_RE.finditer

    def counting_finditer(text, *args):
        if args:
            start, end = args[0], args[1]
        else:
            start, end = 0, len(text)
        scanned.append(end - start)
        return original(text, *args)

    class _Proxy:
        """Подменяет только finditer, остальное делегирует настоящему шаблону."""

        def __init__(self, pattern):
            self._pattern = pattern

        def finditer(self, text, *args):
            return counting_finditer(text, *args)

        def __getattr__(self, name):
            return getattr(self._pattern, name)

    real = se._NONSPACE_RE
    se._NONSPACE_RE = _Proxy(real)
    try:
        text = "договор поставка отчёт комплектующие анализ страница пункт " * 2_000
        middle = len(text) // 2
        se._expanded_context_pair(text, middle, middle + 7, 40)
    finally:
        se._NONSPACE_RE = real

    total = sum(scanned)
    # Прежняя реализация разбирала бы 2 * _TOOLTIP_SCAN_CHARS_MIN символов.
    old_cost = 2 * se._TOOLTIP_SCAN_CHARS_MIN
    assert total < old_cost / 4, (
        f"разобрано {total} символов при старой стоимости {old_cost} — "
        "окно контекста снова читает лишнее"
    )
