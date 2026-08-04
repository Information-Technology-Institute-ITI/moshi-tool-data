from moshi_data_pipeline.studio.domain import (
    SAMPLE_RATE,
    ActivityRegion,
    AnnotationDocument,
    ClipPlanRequest,
    ExclusionRegion,
)
from moshi_data_pipeline.studio.planning import (
    derived_overlaps,
    derived_silences,
    propose_clip_plan,
    validate_annotation,
)


def _sample(value: float) -> int:
    return round(value * SAMPLE_RATE)


def test_annotation_derives_overlap_silence_and_conversation_clips() -> None:
    annotation = AnnotationDocument(
        source_id="source_" + "a" * 32,
        version=3,
        assistant_speaker="A",
        activities=[
            ActivityRegion(speaker="A", start_sample=_sample(0), end_sample=_sample(25)),
            ActivityRegion(speaker="B", start_sample=_sample(20), end_sample=_sample(50)),
            ActivityRegion(speaker="A", start_sample=_sample(50), end_sample=_sample(80)),
            ActivityRegion(speaker="B", start_sample=_sample(80), end_sample=_sample(120)),
        ],
        exclusions=[
            ExclusionRegion(
                kind="noise",
                start_sample=_sample(60),
                end_sample=_sample(61),
            )
        ],
    )
    assert derived_overlaps(annotation.activities) == [(_sample(20), _sample(25))]
    assert derived_silences(annotation.activities, _sample(125)) == [
        (_sample(120), _sample(125))
    ]
    assert validate_annotation(annotation, _sample(125)) == []

    plan = propose_clip_plan(
        annotation.source_id,
        annotation,
        _sample(125),
        ClipPlanRequest(mode="count", count=2),
    )
    assert plan.feasible
    assert len(plan.clips) == 2
    assert all(20 <= clip.metrics["duration_seconds"] <= 100 for clip in plan.clips)
    assert all(clip.metrics["speaker_exchanges"] >= 1 for clip in plan.clips)


def test_infeasible_count_returns_closest_valid_proposal_and_explanation() -> None:
    annotation = AnnotationDocument(
        source_id="source_" + "b" * 32,
        version=1,
        assistant_speaker="A",
        activities=[
            ActivityRegion(speaker="A", start_sample=0, end_sample=_sample(35)),
            ActivityRegion(speaker="B", start_sample=_sample(35), end_sample=_sample(70)),
        ],
    )
    plan = propose_clip_plan(
        annotation.source_id,
        annotation,
        _sample(70),
        ClipPlanRequest(mode="count", count=5),
    )
    assert plan.feasible
    assert "closest valid 1-clip plan" in plan.message
    assert len(plan.clips) == 1


def test_manual_boundary_inside_active_speech_is_blocked() -> None:
    annotation = AnnotationDocument(
        source_id="source_" + "c" * 32,
        version=1,
        assistant_speaker="A",
        activities=[
            ActivityRegion(speaker="A", start_sample=0, end_sample=_sample(30)),
            ActivityRegion(speaker="B", start_sample=_sample(30), end_sample=_sample(60)),
            ActivityRegion(speaker="A", start_sample=_sample(60), end_sample=_sample(90)),
        ],
    )
    plan = propose_clip_plan(
        annotation.source_id,
        annotation,
        _sample(90),
        ClipPlanRequest(
            mode="manual",
            boundaries_samples=[0, _sample(25), _sample(90)],
        ),
    )
    assert not plan.feasible
    assert "end_boundary_cuts_active_audio" in plan.clips[0].reasons


def test_aligned_word_pauses_split_long_turns_and_clamp_source_end() -> None:
    duration = 304
    activities = []
    speakers = ("A", "B", "A", "B", "A", "B", "A", "B")
    for index, speaker in enumerate(speakers):
        start = index * 38
        activities.append(
            ActivityRegion(
                speaker=speaker,
                start_sample=_sample(start),
                end_sample=_sample(start + 38),
            )
        )
    aligned_words = [
        {
            "word": f"word-{index}",
            "start": float(start),
            "end": float(start + 0.8),
            "speaker": speakers[min(start // 38, len(speakers) - 1)],
            "score": 0.9,
        }
        for index, start in enumerate(range(0, duration, 2))
    ]
    # Alignment models may extend the final word by a few milliseconds. The
    # source endpoint must remain a safe boundary after clamping.
    aligned_words[-1]["start"] = 303.4
    aligned_words[-1]["end"] = 304.05
    annotation = AnnotationDocument(
        source_id="source_" + "d" * 32,
        version=1,
        assistant_speaker="A",
        activities=activities,
        aligned_words=aligned_words,
    )

    plan = propose_clip_plan(
        annotation.source_id,
        annotation,
        _sample(duration),
        ClipPlanRequest(mode="count", count=4),
    )

    assert plan.feasible
    assert len(plan.clips) == 4
    assert all(20 <= clip.metrics["duration_seconds"] <= 100 for clip in plan.clips)
    assert all(clip.status == "valid" for clip in plan.clips)
    assert plan.clips[-1].end_sample == _sample(duration)


def test_impossible_short_count_uses_valid_four_clip_fallback() -> None:
    duration = 304
    activities = [
        ActivityRegion(
            speaker="A" if index % 2 == 0 else "B",
            start_sample=_sample(index * 38),
            end_sample=_sample((index + 1) * 38),
        )
        for index in range(8)
    ]
    aligned_words = [
        {
            "word": f"word-{start}",
            "start": float(start),
            "end": float(start + 0.8),
            "speaker": "A" if (start // 38) % 2 == 0 else "B",
            "score": 0.9,
        }
        for start in range(0, duration, 2)
    ]
    annotation = AnnotationDocument(
        source_id="source_" + "e" * 32,
        version=1,
        assistant_speaker="A",
        activities=activities,
        aligned_words=aligned_words,
    )

    plan = propose_clip_plan(
        annotation.source_id,
        annotation,
        _sample(duration),
        ClipPlanRequest(mode="count", count=2),
    )

    assert plan.feasible
    assert len(plan.clips) == 4
    assert "closest valid 4-clip plan" in plan.message
