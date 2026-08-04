from __future__ import annotations

import re
import unicodedata

from moshi_data_pipeline.config import NormalizationConfig

ARABIC_DIACRITICS = re.compile(
    "[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e8\u06ea-\u06ed]"
)
ALEF_VARIANTS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})


def normalize_word(word: str, config: NormalizationConfig) -> str:
    value = unicodedata.normalize("NFKC", word)
    if config.remove_diacritics:
        value = ARABIC_DIACRITICS.sub("", value)
    if config.normalize_alef:
        value = value.translate(ALEF_VARIANTS)
    if config.normalize_alef_maqsura:
        value = value.replace("ى", "ي")
    if not config.keep_punctuation:
        value = "".join(
            character
            for character in value
            if not unicodedata.category(character).startswith(("P", "S"))
        )
    return " ".join(value.split()).strip()


def normalize_text(text: str, config: NormalizationConfig) -> str:
    return " ".join(
        normalized for token in text.split() if (normalized := normalize_word(token, config))
    )
