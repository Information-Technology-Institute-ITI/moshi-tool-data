# P1 Playback Correctness and Synchronization

Status: complete  
Implemented: 2026-09-09  
Contract: `studio.review/v1`

## Outcome

Segment **Play** and **Loop** now use one shared, sample-based playback controller. Canonical audio
is the only audible master. Video is a muted, control-free follower synchronized to the audio
clock.

## Range semantics

All ranges are half-open and use the 24 kHz source sample clock.

- A new Play or Loop command always replaces the previous range, seeks to its exact start, and
  starts playback—even when the same segment is requested repeatedly.
- `once` schedules WaveSurfer's stop boundary at the exact segment end. The time-update fallback
  pauses and snaps to the same end sample if the media clock crosses it.
- `loop` returns to the exact start sample at the end and immediately reschedules the end boundary.
- Pause preserves the active range. Resume continues within it and reschedules its end.
- Clearing a range cancels its scheduled boundary without pausing full-source playback.
- A seek outside the active half-open range clears it. A seek inside preserves it and reschedules
  the end boundary.
- A new command while audio is loading replaces any earlier pending command; only the newest starts
  when decoding finishes.
- Natural media end clears a once-range and restarts a loop-range.
- Playback rejection or decoding failure enters an explicit error state and clears the range.

## Audio/video synchronization

- WaveSurfer plays `canonical_audio` and owns play, pause, seek, range, and rate state.
- The video element is always muted, has no native controls, and cannot become a second audible
  transport.
- Video receives the audio playback rate.
- Drift at or below 120 ms is tolerated to avoid seek churn; greater drift is corrected to the
  canonical audio playhead.
- Frame-step controls seek by exactly `1 / video_frame_rate` seconds.

## Implementation

- [`frontend/src/playbackController.ts`](frontend/src/playbackController.ts) contains the media-
  independent state machine and transport adapters.
- [`frontend/src/components/WaveformEditor.tsx`](frontend/src/components/WaveformEditor.tsx) adapts
  WaveSurfer and the muted video element to that controller.
- [`frontend/src/App.tsx`](frontend/src/App.tsx) sends typed `once` and `loop` range requests from
  transcript segments.
- [`frontend/playback-smoke.html`](frontend/playback-smoke.html) and
  [`frontend/src/playbackBrowserSmoke.ts`](frontend/src/playbackBrowserSmoke.ts) exercise the real
  controller with browser-decoded deterministic WAV and MP4 fixtures.

## Verification

| Check | Result |
| --- | --- |
| Frontend tests | 186 passed across 11 files |
| Playback-controller unit tests | 9 passed |
| Waveform component tests | Play, Loop, muted video, exact boundary, and frame stepping covered |
| Review integration | Typed once/loop requests and repeated nonces covered |
| Production build | TypeScript and Vite passed |
| Real Chrome media smoke | Passed |

The browser smoke validates decoded WAV/MP4 media, audible-master Play, exact once-stop, loop return,
muted video, playback-rate propagation, and drift correction. Generated fixture binaries and the
Chrome screenshot remain under ignored local paths.

No database migration or annotation schema change was required for P1.
