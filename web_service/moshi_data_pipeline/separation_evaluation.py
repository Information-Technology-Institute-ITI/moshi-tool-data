from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from moshi_data_pipeline.audio.io import read_audio


def _mono(path: Path) -> tuple[np.ndarray, int]:
    audio, sample_rate = read_audio(path)
    if audio.shape[1] != 1:
        audio = np.mean(audio, axis=1, keepdims=True, dtype=np.float32)
    return audio[:, 0].astype(np.float64), sample_rate


def _si_sdr(estimate: np.ndarray, reference: np.ndarray) -> float:
    estimate = estimate - np.mean(estimate)
    reference = reference - np.mean(reference)
    reference_energy = float(np.dot(reference, reference))
    if reference_energy <= 1e-12:
        return float("nan")
    target = reference * (float(np.dot(estimate, reference)) / reference_energy)
    noise = estimate - target
    return 10.0 * math.log10(
        max(float(np.dot(target, target)), 1e-12)
        / max(float(np.dot(noise, noise)), 1e-12)
    )


def _relative_error_db(estimate: np.ndarray, reference: np.ndarray) -> float:
    error = estimate - reference
    error_rms = math.sqrt(float(np.mean(error * error)))
    reference_rms = math.sqrt(float(np.mean(reference * reference)))
    return 20.0 * math.log10(max(error_rms, 1e-12) / max(reference_rms, 1e-12))


def _seam_metrics(
    first: np.ndarray,
    second: np.ndarray,
    sample_rate: int,
    seams_seconds: list[float],
) -> dict[str, float | int | None]:
    jumps: list[float] = []
    for seconds in seams_seconds:
        index = round(float(seconds) * sample_rate)
        if 1 <= index < len(first):
            jumps.append(
                max(
                    abs(float(first[index] - first[index - 1])),
                    abs(float(second[index] - second[index - 1])),
                )
            )
    normal = np.maximum(np.abs(np.diff(first)), np.abs(np.diff(second)))
    baseline = float(np.median(normal)) if len(normal) else 0.0
    peak = max(jumps, default=0.0)
    return {
        "seam_count": len(jumps),
        "maximum_seam_jump": peak if jumps else None,
        "maximum_seam_jump_relative_to_median": (
            peak / max(baseline, 1e-8) if jumps else None
        ),
    }


def evaluate_item(item: dict[str, Any], root: Path) -> dict[str, Any]:
    required = ("mixture", "estimated_a", "estimated_b")
    missing = [name for name in required if not item.get(name)]
    if missing:
        raise ValueError(f"Evaluation item is missing: {', '.join(missing)}")
    if bool(item.get("reference_a")) != bool(item.get("reference_b")):
        raise ValueError("reference_a and reference_b must be provided together")

    paths = {
        name: (root / str(item[name])).resolve()
        for name in (*required, "reference_a", "reference_b")
        if item.get(name)
    }
    loaded = {name: _mono(path) for name, path in paths.items()}
    sample_rates = {sample_rate for _, sample_rate in loaded.values()}
    if len(sample_rates) != 1:
        raise ValueError(f"Sample rates differ: {sorted(sample_rates)}")
    lengths = {len(audio) for audio, _ in loaded.values()}
    if len(lengths) != 1:
        raise ValueError(f"Audio lengths differ: {sorted(lengths)}")

    sample_rate = sample_rates.pop()
    mixture = loaded["mixture"][0]
    first = loaded["estimated_a"][0]
    second = loaded["estimated_b"][0]
    reconstruction_error_db = _relative_error_db(first + second, mixture)
    metrics: dict[str, Any] = {
        "duration_seconds": len(mixture) / sample_rate,
        "sample_rate": sample_rate,
        "samples": len(mixture),
        "mixture_reconstruction_error_db": reconstruction_error_db,
        "estimated_a_clipping_ratio": float(np.mean(np.abs(first) >= 0.999)),
        "estimated_b_clipping_ratio": float(np.mean(np.abs(second) >= 0.999)),
        **_seam_metrics(
            first,
            second,
            sample_rate,
            [float(value) for value in item.get("seams_seconds", [])],
        ),
    }

    if "reference_a" in loaded and "reference_b" in loaded:
        reference_a = loaded["reference_a"][0]
        reference_b = loaded["reference_b"][0]
        direct = (_si_sdr(first, reference_a) + _si_sdr(second, reference_b)) / 2.0
        crossed = (_si_sdr(first, reference_b) + _si_sdr(second, reference_a)) / 2.0
        best = max(direct, crossed)
        baseline = (
            _si_sdr(mixture, reference_a) + _si_sdr(mixture, reference_b)
        ) / 2.0
        metrics.update(
            {
                "best_permutation": "direct" if direct >= crossed else "crossed",
                "best_si_sdr_db": best,
                "mixture_si_sdr_db": baseline,
                "si_sdr_improvement_db": best - baseline,
            }
        )

    return {
        "id": str(item.get("id") or Path(str(item["mixture"])).stem),
        "status": "ok",
        "files": {name: str(path) for name, path in paths.items()},
        "metrics": metrics,
    }


def _manifest_items(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        values = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("items", []) if isinstance(payload, dict) else payload
    if not isinstance(values, list) or not values:
        raise ValueError("Evaluation manifest must contain at least one item")
    if any(not isinstance(value, dict) for value in values):
        raise ValueError("Every evaluation manifest item must be an object")
    return values


def _mean(results: list[dict[str, Any]], name: str) -> float | None:
    values = [
        float(result["metrics"][name])
        for result in results
        if result["status"] == "ok"
        and result["metrics"].get(name) is not None
        and math.isfinite(float(result["metrics"][name]))
    ]
    return float(np.mean(values)) if values else None


def evaluate_manifest(path: Path) -> dict[str, Any]:
    items = _manifest_items(path)
    results: list[dict[str, Any]] = []
    for item in items:
        try:
            results.append(evaluate_item(item, path.parent))
        except Exception as exc:
            results.append(
                {
                    "id": str(item.get("id") or item.get("mixture") or "unknown"),
                    "status": "failed",
                    "error": str(exc),
                }
            )
    successful = [result for result in results if result["status"] == "ok"]
    aggregate = {
        "item_count": len(results),
        "successful_item_count": len(successful),
        "failed_item_count": len(results) - len(successful),
        "success_rate": len(successful) / len(results),
        "mean_mixture_reconstruction_error_db": _mean(
            successful, "mixture_reconstruction_error_db"
        ),
        "mean_best_si_sdr_db": _mean(successful, "best_si_sdr_db"),
        "mean_si_sdr_improvement_db": _mean(successful, "si_sdr_improvement_db"),
        "maximum_seam_jump": max(
            (
                float(result["metrics"]["maximum_seam_jump"])
                for result in successful
                if result["metrics"].get("maximum_seam_jump") is not None
            ),
            default=None,
        ),
    }
    return {
        "schema_version": 1,
        "manifest": str(path.resolve()),
        "aggregate": aggregate,
        "items": results,
    }


def compare_evaluations(
    current: dict[str, Any],
    baseline: dict[str, Any],
    maximum_si_sdr_regression_db: float,
) -> list[str]:
    regressions: list[str] = []
    current_aggregate = current.get("aggregate", {})
    baseline_aggregate = baseline.get("aggregate", {})
    current_success = float(current_aggregate.get("success_rate", 0.0))
    baseline_success = float(baseline_aggregate.get("success_rate", 0.0))
    if current_success < baseline_success:
        regressions.append(
            f"success_rate decreased from {baseline_success:.3f} to {current_success:.3f}"
        )
    for metric in ("mean_best_si_sdr_db", "mean_si_sdr_improvement_db"):
        current_value = current_aggregate.get(metric)
        baseline_value = baseline_aggregate.get(metric)
        if current_value is None or baseline_value is None:
            continue
        if float(current_value) < float(baseline_value) - maximum_si_sdr_regression_db:
            regressions.append(
                f"{metric} decreased from {float(baseline_value):.3f} "
                f"to {float(current_value):.3f} dB"
            )
    return regressions
