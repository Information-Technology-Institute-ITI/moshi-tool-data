from moshi_data_pipeline.config import NormalizationConfig
from moshi_data_pipeline.transcription.normalization import normalize_text, normalize_word


def test_conservative_egyptian_arabic_normalization() -> None:
    config = NormalizationConfig()
    assert normalize_word("إزّيك", config) == "ازيك"
    assert normalize_word("ازيك", config) == "ازيك"
    assert normalize_word("آه", config) == "اه"
    assert normalize_word("أيوه", config) == "ايوه"
    for dialect_word in ("عايز", "عاوز", "يعني"):
        assert normalize_word(dialect_word, config) == dialect_word
    for english in ("okay", "AI"):
        assert normalize_word(english, config) == english
    assert normalize_text("machine learning", config) == "machine learning"


def test_alef_maqsura_is_opt_in_and_ta_marbuta_is_unchanged() -> None:
    default = NormalizationConfig()
    opted_in = NormalizationConfig(normalize_alef_maqsura=True)
    assert normalize_word("على", default) == "على"
    assert normalize_word("على", opted_in) == "علي"
    assert normalize_word("مدرسة", default) == "مدرسة"
