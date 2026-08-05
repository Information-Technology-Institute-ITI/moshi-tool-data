import json

import numpy as np
import soundfile as sf

from moshi_data_pipeline.separation_evaluation import (
    compare_evaluations,
    evaluate_manifest,
)


def test_evaluation_is_permutation_aware_and_checks_reconstruction(tmp_path) -> None:
    sample_rate = 8000
    time = np.arange(sample_rate, dtype=np.float64) / sample_rate
    reference_a = (0.1 * np.sin(2 * np.pi * 220 * time)).astype(np.float32)
    reference_b = (0.08 * np.sin(2 * np.pi * 337 * time)).astype(np.float32)
    mixture = reference_a + reference_b
    for name, audio in {
        "mixture.wav": mixture,
        "estimate_a.wav": reference_b,
        "estimate_b.wav": reference_a,
        "reference_a.wav": reference_a,
        "reference_b.wav": reference_b,
    }.items():
        sf.write(tmp_path / name, audio, sample_rate, subtype="FLOAT")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "swapped-perfect",
                        "mixture": "mixture.wav",
                        "estimated_a": "estimate_a.wav",
                        "estimated_b": "estimate_b.wav",
                        "reference_a": "reference_a.wav",
                        "reference_b": "reference_b.wav",
                        "seams_seconds": [0.5],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = evaluate_manifest(manifest)

    assert result["aggregate"]["success_rate"] == 1.0
    metrics = result["items"][0]["metrics"]
    assert metrics["best_permutation"] == "crossed"
    assert metrics["best_si_sdr_db"] > 100.0
    assert metrics["mixture_reconstruction_error_db"] < -100.0


def test_evaluation_comparison_reports_si_sdr_regression() -> None:
    baseline = {
        "aggregate": {
            "success_rate": 1.0,
            "mean_best_si_sdr_db": 12.0,
            "mean_si_sdr_improvement_db": 8.0,
        }
    }
    current = {
        "aggregate": {
            "success_rate": 1.0,
            "mean_best_si_sdr_db": 10.0,
            "mean_si_sdr_improvement_db": 7.8,
        }
    }
    regressions = compare_evaluations(current, baseline, 0.5)
    assert len(regressions) == 1
    assert "mean_best_si_sdr_db" in regressions[0]
