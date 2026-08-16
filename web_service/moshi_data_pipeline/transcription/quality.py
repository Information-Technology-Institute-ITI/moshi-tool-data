from __future__ import annotations

import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from math import isfinite
from typing import Any

_TOKEN_RE = re.compile(r"[\w\u0600-\u06ff]+", re.UNICODE)
_LATIN_RE = re.compile(r"[A-Za-z]")
HIGH_RISK_TRANSCRIPT_FLAGS = frozenset(
    {
        "abnormally_high_word_rate",
        "decode_disagreement",
        "low_average_log_probability",
        "overlapping_speech",
        "repeated_ngram",
        "suspicious_character_sequence",
    }
)
_FLAG_WEIGHTS = {
    "repeated_ngram": 6,
    "suspicious_character_sequence": 6,
    "abnormally_high_word_rate": 4,
    "abnormally_low_word_rate": 3,
    "low_average_log_probability": 2,
}


def normalized_tokens(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(text)]


def maximum_ngram_occurrences(tokens: list[str], size: int = 3) -> int:
    if size <= 0 or len(tokens) < size:
        return 0
    counts = Counter(tuple(tokens[index : index + size]) for index in range(len(tokens) - size + 1))
    return max(counts.values(), default=0)


def normalized_disagreement(first: str, second: str) -> float:
    left = " ".join(normalized_tokens(first))
    right = " ".join(normalized_tokens(second))
    if not left and not right:
        return 0.0
    return 1.0 - SequenceMatcher(None, left, right).ratio()


def _has_suspicious_character_sequence(text: str, tokens: list[str]) -> bool:
    if any(unicodedata.category(character) in {"Cf", "Cs"} for character in text):
        return True
    previous_prefix = ""
    run = 0
    for token in tokens:
        if len(token) <= 3 and token:
            prefix = token[0]
            run = run + 1 if prefix == previous_prefix else 1
            previous_prefix = prefix
            if run >= 8:
                return True
        else:
            previous_prefix = ""
            run = 0
    return False


def analyze_segment(segment: dict[str, Any], config: Any) -> list[str]:
    text = str(segment.get("text", ""))
    tokens = normalized_tokens(text)
    duration = max(0.0, float(segment.get("end", 0.0)) - float(segment.get("start", 0.0)))
    word_rate = len(tokens) / duration if duration else float("inf")
    flags: list[str] = []
    if maximum_ngram_occurrences(tokens, 3) > config.max_repeated_ngram_occurrences:
        flags.append("repeated_ngram")
    if _has_suspicious_character_sequence(text, tokens):
        flags.append("suspicious_character_sequence")
    average_log_probability = segment.get("avg_logprob")
    if (
        average_log_probability is not None
        and float(average_log_probability) < config.suspect_avg_logprob
    ):
        flags.append("low_average_log_probability")
    if tokens and word_rate < config.min_words_per_second:
        flags.append("abnormally_low_word_rate")
    if word_rate > config.max_words_per_second:
        flags.append("abnormally_high_word_rate")
    return flags


def quality_rank(flags: list[str], average_log_probability: Any) -> tuple[int, int, float]:
    try:
        probability = float(average_log_probability)
    except (TypeError, ValueError):
        probability = float("-inf")
    probability_penalty = -probability if isfinite(probability) else float("inf")
    return (
        sum(_FLAG_WEIGHTS.get(flag, 1) for flag in set(flags)),
        len(set(flags)),
        probability_penalty,
    )


def latin_script_word(word: str) -> bool:
    return bool(_LATIN_RE.search(word))


def quality_summary(segments: list[dict[str, Any]]) -> dict[str, Any]:
    suspicious = [
        {
            "index": index,
            "start": float(segment.get("start", 0.0)),
            "end": float(segment.get("end", 0.0)),
            "flags": list(segment.get("quality_flags", [])),
            "decode_disagreement": float(segment.get("decode_disagreement", 0.0)),
        }
        for index, segment in enumerate(segments)
        if segment.get("quality_flags")
    ]
    return {
        "suspicious_segment_count": len(suspicious),
        "unresolved_hallucination_count": sum(
            bool(
                {"repeated_ngram", "suspicious_character_sequence"}
                & set(item["flags"])
            )
            for item in suspicious
        ),
        "suspicious_segments": suspicious,
    }
