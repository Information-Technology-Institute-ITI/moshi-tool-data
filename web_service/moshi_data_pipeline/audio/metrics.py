from __future__ import annotations

import math

import numpy as np


def safe_rms(signal: np.ndarray) -> float:
    if signal.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(signal.astype(np.float64)))))


def peak(signal: np.ndarray) -> float:
    return float(np.max(np.abs(signal))) if signal.size else 0.0


def clipping_ratio(signal: np.ndarray, threshold: float = 32767 / 32768) -> float:
    return float(np.mean(np.abs(signal) >= threshold)) if signal.size else 0.0


def silence_ratio(signal: np.ndarray, threshold_db: float = -50.0) -> float:
    if signal.size == 0:
        return 1.0
    threshold = 10 ** (threshold_db / 20.0)
    return float(np.mean(np.abs(signal) < threshold))


def dbfs(value: float) -> float:
    return 20.0 * math.log10(value) if value > 0 else float("-inf")


def masked_energy(signal: np.ndarray, mask: np.ndarray) -> float:
    if not mask.any():
        return 0.0
    selected = signal[mask]
    return float(np.mean(np.square(selected.astype(np.float64))))


def mixture_reconstruction_error_db(
    reference: np.ndarray,
    stereo: np.ndarray,
    mask: np.ndarray,
) -> float | None:
    if stereo.ndim != 2 or stereo.shape[1] != 2:
        raise ValueError("Expected stereo audio shaped [samples, 2]")
    if reference.shape != mask.shape or len(stereo) != len(reference):
        raise ValueError("Reference, stereo, and reconstruction mask lengths must match")
    if not mask.any():
        return None
    selected_reference = reference[mask].astype(np.float64)
    selected_estimate = np.sum(stereo[mask], axis=1, dtype=np.float64)
    reference_rms = safe_rms(selected_reference)
    error_rms = safe_rms(selected_estimate - selected_reference)
    return 20.0 * math.log10(max(error_rms, 1e-12) / max(reference_rms, 1e-12))


def channel_metrics(stereo: np.ndarray, silence_threshold_db: float = -50.0) -> dict[str, float]:
    if stereo.ndim != 2 or stereo.shape[1] != 2:
        raise ValueError("Expected stereo audio shaped [samples, 2]")
    metrics: dict[str, float] = {}
    for index, name in enumerate(("left", "right")):
        signal = stereo[:, index]
        rms = safe_rms(signal)
        metrics[f"{name}_rms"] = rms
        metrics[f"{name}_rms_dbfs"] = dbfs(rms)
        metrics[f"{name}_peak"] = peak(signal)
        metrics[f"{name}_clipping_ratio"] = clipping_ratio(signal)
        metrics[f"{name}_silence_ratio"] = silence_ratio(signal, silence_threshold_db)
    return metrics


def leakage_indicators(
    stereo: np.ndarray, assistant_mask: np.ndarray, user_mask: np.ndarray
) -> dict[str, float]:
    left, right = stereo[:, 0], stereo[:, 1]
    left_expected = masked_energy(left, assistant_mask)
    right_in_assistant = masked_energy(right, assistant_mask)
    right_expected = masked_energy(right, user_mask)
    left_in_user = masked_energy(left, user_mask)
    epsilon = 1e-12
    values = {
        "right_leakage_in_assistant_intervals": right_in_assistant / (left_expected + epsilon),
        "left_leakage_in_user_intervals": left_in_user / (right_expected + epsilon),
        "left_expected_energy": left_expected,
        "right_expected_energy": right_expected,
        "right_energy_in_assistant_intervals": right_in_assistant,
        "left_energy_in_user_intervals": left_in_user,
    }
    values["possible_swapped_channels"] = float(
        right_in_assistant > left_expected and left_in_user > right_expected
    )
    return values
