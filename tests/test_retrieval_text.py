"""Tests for shared Korean-friendly search text preparation."""

from video_preprocess.retrieval import (
    character_ngrams,
    normalize_search_text,
    search_terms,
)


def test_normalization_folds_unicode_punctuation_case_and_whitespace() -> None:
    assert normalize_search_text("  ＡI·음성\n검출!!!  ") == "ai 음성 검출"


def test_character_ngrams_do_not_cross_word_boundaries() -> None:
    assert character_ngrams("음성 검출") == ("음성", "검출")
    words, grams = search_terms("파이프라인을")
    assert words == ("파이프라인을",)
    assert "파이" in grams
    assert "인을" in grams
