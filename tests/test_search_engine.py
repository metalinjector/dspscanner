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
