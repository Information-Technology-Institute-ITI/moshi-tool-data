import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { createPortal } from "react-dom";
import WaveSurfer from "wavesurfer.js";
import RegionsPlugin, { type Region } from "wavesurfer.js/dist/plugins/regions.esm.js";
import type { ActivityRegion, Annotation, ExclusionRegion, Speaker } from "../types";
import { sampleId, seconds } from "../api";
import type { PlaybackRange } from "../productContracts";
import {
  PLAYBACK_SAMPLE_RATE,
  PlaybackController,
  type PlaybackSnapshot,
} from "../playbackController";

const SAMPLE_RATE = PLAYBACK_SAMPLE_RATE;
const COLORS = {
  A: "rgba(88, 214, 190, .30)",
  B: "rgba(255, 184, 108, .30)",
  exclusion: "rgba(255, 96, 120, .30)",
};

/**
 * A range the parent asks the player to move to. `nonce` lets the same range be
 * requested twice in a row (clicking the same transcript entry again).
 */
export type FocusRange = PlaybackRange & {
  nonce: number;
};

type Props = {
  audioUrl: string;
  videoUrl?: string | null;
  annotation: Annotation;
  durationSamples: number;
  frameRate: number;
  onChange: (annotation: Annotation) => void;
  /** Seek, and optionally loop, the given original-audio range. */
  focusRange?: FocusRange | null;
  /** Reports the playhead so the parent can follow along in the transcript. */
  onTimeChange?: (sample: number) => void;
  /**
   * Fired when the user clicks an A/B activity region on the timeline, with the
   * point they clicked. One region often spans several transcript segments, so
   * the position is what tells the parent which one they meant.
   */
  onRegionClick?: (regionId: string, atSample: number) => void;
  /**
   * Asks the parent to remove a speaker rectangle. Removing one can also take a
   * transcript segment with it, so the parent confirms first.
   */
  onRegionDelete?: (regionId: string) => void;
  /** Optional rail target for annotation and exclusion tools. */
  toolTarget?: HTMLElement | null;
  readOnly?: boolean;
};

export type WaveformEditorHandle = {
  playPlayback: () => void;
  pausePlayback: () => void;
  togglePlayback: () => void;
  seekBySeconds: (seconds: number) => void;
  beginSelection: () => void;
  finishActivity: (speaker: Speaker) => void;
};

const WaveformEditor = forwardRef<WaveformEditorHandle, Props>(function WaveformEditor({
  audioUrl,
  videoUrl,
  annotation,
  durationSamples,
  frameRate,
  onChange,
  focusRange,
  onTimeChange,
  onRegionClick,
  onRegionDelete,
  toolTarget,
  readOnly = false,
}: Props, ref) {
  const container = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const wave = useRef<WaveSurfer | null>(null);
  const playbackController = useRef<PlaybackController | null>(null);
  const waveReady = useRef(false);
  const regions = useRef<RegionsPlugin | null>(null);
  const annotationRef = useRef(annotation);
  // Held in refs because the WaveSurfer instance is created once per audioUrl
  // and its listeners would otherwise capture the first render's callbacks.
  const onTimeRef = useRef(onTimeChange);
  const onRegionClickRef = useRef(onRegionClick);
  const readOnlyRef = useRef(readOnly);
  const lastFocusNonce = useRef<number | null>(null);
  const selectionStart = useRef<number | null>(null);
  // State, not just the ref, because the regions are drawn from an effect that
  // has to re-run the moment the audio is decoded.
  const [ready, setReady] = useState(false);
  const [zoom, setZoom] = useState(35);
  const [anchor, setAnchor] = useState<number | null>(null);
  const [playback, setPlayback] = useState<PlaybackSnapshot>({
    status: "loading",
    current_sample: 0,
    active_range: null,
    rate: 1,
    audition_mode: "mixed",
    error: null,
  });

  annotationRef.current = annotation;
  onTimeRef.current = onTimeChange;
  onRegionClickRef.current = onRegionClick;
  readOnlyRef.current = readOnly;

  useEffect(() => {
    if (!container.current) return;
    const regionPlugin = RegionsPlugin.create();
    const instance = WaveSurfer.create({
      container: container.current,
      url: audioUrl,
      height: 72,
      waveColor: "#324052",
      progressColor: "#e7f6f2",
      cursorColor: "#f4d35e",
      normalize: true,
      minPxPerSec: zoom,
      plugins: [regionPlugin],
    });
    const controller = new PlaybackController(
      {
        play: (stopAt) => instance.play(undefined, stopAt),
        pause: () => instance.pause(),
        setTime: (time) => instance.setTime(time),
        getCurrentTime: () => instance.getCurrentTime(),
        getDuration: () => instance.getDuration(),
        setPlaybackRate: (value) => instance.setPlaybackRate(value),
      },
      durationSamples,
    );
    playbackController.current = controller;
    controller.setVideo(video.current);
    const unsubscribe = controller.subscribe((state) => {
      setPlayback(state);
      onTimeRef.current?.(state.current_sample);
    });
    wave.current = instance;
    waveReady.current = false;
    setReady(false);
    regions.current = regionPlugin;
    instance.on("ready", () => {
      waveReady.current = true;
      instance.zoom(zoom);
      setReady(true);
      void controller.markReady();
    });
    instance.on("timeupdate", (time) => controller.handleTimeUpdate(time));
    instance.on("interaction", (time) => controller.handleInteraction(time));
    instance.on("play", () => controller.handleAudioPlay());
    instance.on("pause", () => controller.handleAudioPause());
    instance.on("finish", () => controller.handleAudioEnded());
    instance.on("error", (error) => controller.markError(error));
    const wrapper = instance.getWrapper();
    const prepareWaveformSeek = (event: MouseEvent) => {
      const rect = wrapper.getBoundingClientRect();
      const ratio = rect.width > 0 ? (event.clientX - rect.left) / rect.width : 0;
      controller.prepareExternalSeek(
        Math.round(Math.max(0, Math.min(1, ratio)) * instance.getDuration() * SAMPLE_RATE),
      );
    };
    wrapper.addEventListener("click", prepareWaveformSeek, { capture: true });
    regionPlugin.on("region-updated", (region) => updateRegion(region));
    regionPlugin.on("region-clicked", (region, event) => {
      // Read the pointer rather than the player: the click seeks by bubbling to
      // the waveform wrapper, which has not happened yet at this point.
      const wrapper = instance.getWrapper();
      const rect = wrapper.getBoundingClientRect();
      const ratio = rect.width > 0 ? (event.clientX - rect.left) / rect.width : 0;
      const bounded = Math.max(0, Math.min(1, ratio));
      onRegionClickRef.current?.(
        region.id,
        Math.round(bounded * instance.getDuration() * SAMPLE_RATE),
      );
    });
    return () => {
      unsubscribe();
      wrapper.removeEventListener("click", prepareWaveformSeek, { capture: true });
      controller.destroy();
      instance.destroy();
      wave.current = null;
      playbackController.current = null;
      waveReady.current = false;
      regions.current = null;
      setReady(false);
    };
  }, [audioUrl, durationSamples]);

  useEffect(() => {
    if (waveReady.current) wave.current?.zoom(zoom);
  }, [zoom]);

  // Selecting a transcript entry moves the playhead to its start and, when
  // asked, loops its original-audio range. This never alters stored data.
  useEffect(() => {
    if (!focusRange || focusRange.nonce === lastFocusNonce.current) return;
    const controller = playbackController.current;
    if (!controller) return;
    lastFocusNonce.current = focusRange.nonce;
    void controller.playRange(focusRange);
  }, [focusRange]);

  // Waits for the decoded audio. Regions added before then have no duration to
  // position against, and the plugin only re-checks whether to draw them on the
  // next scroll, zoom or resize — which is why the speaker colours used to
  // appear only after clicking the waveform or dragging a region.
  useEffect(() => {
    const plugin = regions.current;
    if (!plugin || !ready) return;
    plugin.clearRegions();
    const add = (
      id: string,
      startSample: number,
      endSample: number,
      color: string,
      content: string,
    ) =>
      plugin.addRegion({
        id,
        start: startSample / SAMPLE_RATE,
        end: endSample / SAMPLE_RATE,
        color,
        content,
        drag: !readOnly,
        resize: !readOnly,
        minLength: 0.05,
      });
    annotation.activities.forEach((item) =>
      add(item.id, item.start_sample, item.end_sample, COLORS[item.speaker], item.speaker),
    );
    annotation.exclusions.forEach((item) =>
      add(
        item.id,
        item.start_sample,
        item.end_sample,
        COLORS.exclusion,
        `Exclude · ${item.kind}`,
      ),
    );
  }, [annotation.activities, annotation.exclusions, readOnly, ready]);

  function updateRegion(region: Region) {
    if (readOnlyRef.current) return;
    const start = Math.max(0, Math.round(region.start * SAMPLE_RATE));
    const end = Math.min(durationSamples, Math.round(region.end * SAMPLE_RATE));
    const currentAnnotation = annotationRef.current;
    const activity = currentAnnotation.activities.find((item) => item.id === region.id);
    if (activity) {
      onChange({
        ...currentAnnotation,
        activities: currentAnnotation.activities.map((item) =>
          item.id === region.id
            ? { ...item, start_sample: start, end_sample: end, origin: "manual" }
            : item,
        ),
      });
      return;
    }
    const exclusion = currentAnnotation.exclusions.find((item) => item.id === region.id);
    if (exclusion) {
      onChange({
        ...currentAnnotation,
        exclusions: currentAnnotation.exclusions.map((item) =>
          item.id === region.id ? { ...item, start_sample: start, end_sample: end } : item,
        ),
      });
    }
  }

  function beginSelection() {
    const time = playback.current_sample / SAMPLE_RATE;
    selectionStart.current = time;
    setAnchor(time);
  }

  function selectionBounds(): [number, number] | null {
    if (selectionStart.current === null) return null;
    const end = playback.current_sample / SAMPLE_RATE;
    const first = Math.min(selectionStart.current, end);
    const last = Math.max(selectionStart.current, end);
    if (last - first < 0.05) return null;
    selectionStart.current = null;
    setAnchor(null);
    return [Math.round(first * SAMPLE_RATE), Math.round(last * SAMPLE_RATE)];
  }

  function finishActivity(speaker: Speaker) {
    const bounds = selectionBounds();
    if (!bounds) return;
    const region: ActivityRegion = {
      id: sampleId("activity"),
      speaker,
      start_sample: bounds[0],
      end_sample: bounds[1],
      origin: "manual",
      confidence: null,
    };
    onChange({ ...annotationRef.current, activities: [...annotationRef.current.activities, region] });
  }

  function removeSelected(id: string) {
    onChange({
      ...annotationRef.current,
      activities: annotationRef.current.activities.filter((item) => item.id !== id),
      exclusions: annotationRef.current.exclusions.filter((item) => item.id !== id),
    });
  }

  function updateExclusion(id: string, patch: Partial<ExclusionRegion>) {
    onChange({
      ...annotationRef.current,
      exclusions: annotationRef.current.exclusions.map((item) =>
        item.id === id ? { ...item, ...patch } : item,
      ),
    });
  }

  function seekRelative(delta: number) {
    playbackController.current?.seekBySeconds(delta);
  }

  useImperativeHandle(ref, () => ({
    playPlayback: () => void playbackController.current?.play(),
    pausePlayback: () => playbackController.current?.pause(),
    togglePlayback: () => void playbackController.current?.toggle(),
    seekBySeconds: (delta) => seekRelative(delta),
    beginSelection,
    finishActivity,
  }));

  const current = playback.current_sample / SAMPLE_RATE;
  const loopRange = playback.active_range?.behavior === "loop"
    ? [
        playback.active_range.start_sample / SAMPLE_RATE,
        playback.active_range.end_sample / SAMPLE_RATE,
      ]
    : null;

  const playheadPosition = `${Math.max(
    0,
    Math.min(100, durationSamples > 0 ? ((current * SAMPLE_RATE) / durationSamples) * 100 : 0),
  )}%`;

  const railTools = !readOnly && (
    <div className="timeline-tools">
      <div className="annotation-actions">
        <button type="button" onClick={beginSelection} title="Mark where this speaker starts talking">
          1. Mark range start
        </button>
        <button type="button" className="speaker-a" onClick={() => finishActivity("A")} title="End the range here and assign it to speaker A">
          2. End as Speaker A
        </button>
        <button type="button" className="speaker-b" onClick={() => finishActivity("B")} title="End the range here and assign it to speaker B">
          2. End as Speaker B
        </button>
        <p className="timeline-tools-help">
          Use this only when a speaker lane is missing or wrong: seek to where speech starts,
          mark the start, seek to where it ends, then choose A or B. It changes the speaker
          timeline—not the transcript text.
        </p>
        <span className="shortcut-note">Shortcut: [ then A or B</span>
      </div>
      {!!annotation.exclusions.length && (
        <div className="exclusion-editor">
          {annotation.exclusions.map((item) => (
            <div key={item.id}>
              <span>{seconds(item.start_sample)}–{seconds(item.end_sample)}s</span>
              <select value={item.kind} onChange={(event) => updateExclusion(item.id, {
                kind: event.target.value as ExclusionRegion["kind"],
              })}>
                <option value="music">Music</option>
                <option value="advertisement">Advertisement</option>
                <option value="noise">Noise</option>
                <option value="third_speaker">Third speaker</option>
                <option value="unusable">Unusable</option>
              </select>
              <input value={item.note} placeholder="Optional note" onChange={(event) => updateExclusion(item.id, { note: event.target.value })} />
              <button type="button" className="danger-soft" onClick={() => removeSelected(item.id)}>Remove</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );

  return (
    <div className="editor-stack">
      {videoUrl && (
        <video
          className="source-video"
          ref={video}
          src={videoUrl}
          muted
          playsInline
          aria-label="Muted synchronized source video"
          onLoadedMetadata={() => playbackController.current?.setVideo(video.current)}
        />
      )}
      <div className="wave-shell">
        <div ref={container} aria-label="Editable source waveform" />
        {anchor !== null && (
          <div className="selection-hint">
            Range starts at {anchor.toFixed(2)}s — seek to its end, then choose Speaker A or B
          </div>
        )}
      </div>
      <div className="transport">
        <button onClick={() => seekRelative(-1)}>−1s</button>
        <button onClick={() => seekRelative(-1 / frameRate)}>− frame</button>
        <button
          className="primary"
          disabled={playback.status === "loading" || playback.status === "playing"}
          onClick={() => void playbackController.current?.play()}
        >
          Play
        </button>
        <button
          className="pause-control"
          disabled={playback.status !== "playing"}
          onClick={() => playbackController.current?.pause()}
        >
          Pause
        </button>
        <button onClick={() => seekRelative(1 / frameRate)}>+ frame</button>
        <button onClick={() => seekRelative(1)}>+1s</button>
        <span className="time-readout">{current.toFixed(2)}s</span>
        <label>
          Speed
          <select
            value={playback.rate}
            onChange={(event) => playbackController.current?.setRate(Number(event.target.value))}
          >
            <option value={0.75}>0.75×</option>
            <option value={1}>1×</option>
            <option value={1.25}>1.25×</option>
            <option value={1.5}>1.5×</option>
          </select>
        </label>
        <label className="zoom-control">
          Zoom
          <input
            type="range"
            min="10"
            max="160"
            value={zoom}
            onChange={(event) => setZoom(Number(event.target.value))}
          />
        </label>
        {loopRange && (
          <button onClick={() => playbackController.current?.clearRange()}>Clear loop</button>
        )}
      </div>
      {playback.error && <div className="inline-error" role="alert">{playback.error}</div>}
      {toolTarget && createPortal(railTools, toolTarget)}
      {!readOnly && !toolTarget && (
        <div className="annotation-actions">
          <button onClick={beginSelection}>1. Mark range start</button>
          <button className="speaker-a" onClick={() => finishActivity("A")}>2. End as Speaker A</button>
          <button className="speaker-b" onClick={() => finishActivity("B")}>2. End as Speaker B</button>
          <span className="shortcut-note">Shortcut: [ then A or B</span>
        </div>
      )}
      <div className="lane-grid">
        {(["A", "B"] as Speaker[]).map((speaker) => (
          <div className="lane-row" key={speaker}>
            <strong className={`lane-label speaker-${speaker.toLowerCase()}`}>Speaker {speaker}</strong>
            <div className="lane-track">
              {annotation.activities
                .filter((item) => item.speaker === speaker)
                .map((item) => (
                  <button
                    key={item.id}
                    className={`lane-region speaker-${speaker.toLowerCase()}`}
                    style={{
                      left: `${(item.start_sample / durationSamples) * 100}%`,
                      width: `${((item.end_sample - item.start_sample) / durationSamples) * 100}%`,
                    }}
                    onClick={(event) => {
                      // The lane is a miniature of the whole source, so the click
                      // position maps straight onto a sample. Seeking there keeps
                      // the player, the playhead and the transcript in step.
                      const rect = event.currentTarget.parentElement!.getBoundingClientRect();
                      const ratio = rect.width > 0
                        ? (event.clientX - rect.left) / rect.width
                        : 0;
                      const at = Math.round(
                        Math.max(0, Math.min(1, ratio)) * durationSamples,
                      );
                      playbackController.current?.seekToSample(at);
                      onRegionClick?.(item.id, at);
                    }}
                    onDoubleClick={() => !readOnly && onRegionDelete?.(item.id)}
                    title={
                      `${seconds(item.start_sample)}–${seconds(item.end_sample)}s`
                      + (readOnly ? "" : " · double-click to remove")
                    }
                  >
                    {speaker}
                  </button>
                ))}
              <span
                className="lane-playhead"
                style={{ left: playheadPosition }}
                aria-hidden="true"
              />
            </div>
          </div>
        ))}
        <div className="lane-row">
          <strong className="lane-label excluded">Excluded</strong>
          <div className="lane-track">
            {annotation.exclusions.map((item) => (
              <button
                key={item.id}
                className="lane-region excluded"
                style={{
                  left: `${(item.start_sample / durationSamples) * 100}%`,
                  width: `${((item.end_sample - item.start_sample) / durationSamples) * 100}%`,
                }}
                onDoubleClick={() => removeSelected(item.id)}
                title={`${item.kind} · double-click to remove`}
              >
                ×
              </button>
            ))}
            <span
              className="lane-playhead"
              style={{ left: playheadPosition }}
              aria-hidden="true"
            />
          </div>
        </div>
      </div>
      {!toolTarget && !!annotation.exclusions.length && (
        <div className="exclusion-editor">
          {annotation.exclusions.map((item) => (
            <div key={item.id}>
              <span>{seconds(item.start_sample)}–{seconds(item.end_sample)}s</span>
              <select
                value={item.kind}
                onChange={(event) =>
                  updateExclusion(item.id, {
                    kind: event.target.value as ExclusionRegion["kind"],
                  })
                }
              >
                <option value="music">Music</option>
                <option value="advertisement">Advertisement</option>
                <option value="noise">Noise</option>
                <option value="third_speaker">Third speaker</option>
                <option value="unusable">Unusable</option>
              </select>
              <input
                value={item.note}
                placeholder="Optional note"
                onChange={(event) => updateExclusion(item.id, { note: event.target.value })}
              />
              <button className="danger-soft" onClick={() => removeSelected(item.id)}>Remove</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
});

export default WaveformEditor;
