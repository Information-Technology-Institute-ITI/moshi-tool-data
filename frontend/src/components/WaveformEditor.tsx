import { useEffect, useRef, useState } from "react";
import WaveSurfer from "wavesurfer.js";
import RegionsPlugin, { type Region } from "wavesurfer.js/dist/plugins/regions.esm.js";
import type { ActivityRegion, Annotation, ExclusionRegion, Speaker } from "../types";
import { sampleId, seconds } from "../api";

const SAMPLE_RATE = 24_000;
const COLORS = {
  A: "rgba(88, 214, 190, .30)",
  B: "rgba(255, 184, 108, .30)",
  exclusion: "rgba(255, 96, 120, .30)",
};

/**
 * A range the parent asks the player to move to. `nonce` lets the same range be
 * requested twice in a row (clicking the same transcript entry again).
 */
export type FocusRange = {
  startSample: number;
  endSample: number;
  loop: boolean;
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
  /** Fired when the user clicks an A/B activity region on the timeline. */
  onRegionClick?: (regionId: string) => void;
  /** Fired when the user marks a range with `[` then a selection key. */
  onRangeSelect?: (startSample: number, endSample: number) => void;
  readOnly?: boolean;
};

export default function WaveformEditor({
  audioUrl,
  videoUrl,
  annotation,
  durationSamples,
  frameRate,
  onChange,
  focusRange,
  onTimeChange,
  onRegionClick,
  onRangeSelect,
  readOnly = false,
}: Props) {
  const container = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const wave = useRef<WaveSurfer | null>(null);
  const waveReady = useRef(false);
  const regions = useRef<RegionsPlugin | null>(null);
  const annotationRef = useRef(annotation);
  // Held in refs because the WaveSurfer instance is created once per audioUrl
  // and its listeners would otherwise capture the first render's callbacks.
  const onTimeRef = useRef(onTimeChange);
  const onRegionClickRef = useRef(onRegionClick);
  const readOnlyRef = useRef(readOnly);
  const lastFocusNonce = useRef<number | null>(null);
  const syncing = useRef(false);
  const selectionStart = useRef<number | null>(null);
  const loopRangeRef = useRef<[number, number] | null>(null);
  const [current, setCurrent] = useState(0);
  const [zoom, setZoom] = useState(35);
  const [rate, setRate] = useState(1);
  const [anchor, setAnchor] = useState<number | null>(null);
  const [loopAnchor, setLoopAnchor] = useState<number | null>(null);
  const [loopRange, setLoopRange] = useState<[number, number] | null>(null);

  annotationRef.current = annotation;
  loopRangeRef.current = loopRange;
  onTimeRef.current = onTimeChange;
  onRegionClickRef.current = onRegionClick;
  readOnlyRef.current = readOnly;

  useEffect(() => {
    if (!container.current) return;
    const regionPlugin = RegionsPlugin.create();
    const instance = WaveSurfer.create({
      container: container.current,
      url: audioUrl,
      height: 126,
      waveColor: "#324052",
      progressColor: "#e7f6f2",
      cursorColor: "#f4d35e",
      normalize: true,
      minPxPerSec: zoom,
      plugins: [regionPlugin],
    });
    wave.current = instance;
    waveReady.current = false;
    regions.current = regionPlugin;
    instance.on("ready", () => {
      waveReady.current = true;
      instance.zoom(zoom);
    });
    instance.on("timeupdate", (time) => {
      setCurrent(time);
      onTimeRef.current?.(Math.round(time * SAMPLE_RATE));
      const loop = loopRangeRef.current;
      if (loop && time >= loop[1]) {
        instance.setTime(loop[0]);
        if (video.current) video.current.currentTime = loop[0];
        return;
      }
      if (video.current && !syncing.current && Math.abs(video.current.currentTime - time) > 0.18) {
        video.current.currentTime = time;
      }
    });
    instance.on("play", () => video.current?.play().catch(() => undefined));
    instance.on("pause", () => video.current?.pause());
    regionPlugin.on("region-updated", (region) => updateRegion(region));
    regionPlugin.on("region-clicked", (region) => onRegionClickRef.current?.(region.id));
    return () => {
      instance.destroy();
      wave.current = null;
      waveReady.current = false;
      regions.current = null;
    };
  }, [audioUrl]);

  useEffect(() => {
    if (waveReady.current) wave.current?.zoom(zoom);
  }, [zoom]);

  // Selecting a transcript entry moves the playhead to its start and, when
  // asked, loops its original-audio range. This never alters stored data.
  useEffect(() => {
    if (!focusRange || focusRange.nonce === lastFocusNonce.current) return;
    const instance = wave.current;
    if (!instance) return;
    lastFocusNonce.current = focusRange.nonce;
    const start = focusRange.startSample / SAMPLE_RATE;
    const end = focusRange.endSample / SAMPLE_RATE;
    const apply = () => {
      instance.setTime(start);
      if (video.current) video.current.currentTime = start;
      setLoopRange(focusRange.loop && end > start ? [start, end] : null);
      if (focusRange.loop) instance.play().catch(() => undefined);
    };
    if (waveReady.current) apply();
    else instance.once("ready", apply);
  }, [focusRange]);

  useEffect(() => {
    wave.current?.setPlaybackRate(rate);
    if (video.current) video.current.playbackRate = rate;
  }, [rate]);

  useEffect(() => {
    const plugin = regions.current;
    if (!plugin) return;
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
  }, [annotation.activities, annotation.exclusions, readOnly]);

  useEffect(() => {
    const keys = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) {
        return;
      }
      if (event.code === "Space") {
        event.preventDefault();
        wave.current?.playPause();
      } else if (event.key === "[") {
        if (!readOnly) beginSelection();
      } else if (event.key.toLowerCase() === "a") {
        if (!readOnly) finishActivity("A");
      } else if (event.key.toLowerCase() === "b") {
        if (!readOnly) finishActivity("B");
      } else if (event.key.toLowerCase() === "x") {
        if (!readOnly) finishExclusion();
      } else if (event.key.toLowerCase() === "t") {
        if (!readOnly && onRangeSelect) {
          const bounds = selectionBounds();
          if (bounds) onRangeSelect(bounds[0], bounds[1]);
        }
      } else if (event.key === "ArrowLeft") {
        seekRelative(-1);
      } else if (event.key === "ArrowRight") {
        seekRelative(1);
      } else if (event.key === ",") {
        seekRelative(-1 / frameRate);
      } else if (event.key === ".") {
        seekRelative(1 / frameRate);
      } else if (event.key.toLowerCase() === "l") {
        setLoopPoint();
      }
    };
    window.addEventListener("keydown", keys);
    return () => window.removeEventListener("keydown", keys);
  });

  function updateRegion(region: Region) {
    if (syncing.current || readOnlyRef.current) return;
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
    const time = wave.current?.getCurrentTime() ?? current;
    selectionStart.current = time;
    setAnchor(time);
  }

  function selectionBounds(): [number, number] | null {
    if (selectionStart.current === null) return null;
    const end = wave.current?.getCurrentTime() ?? current;
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

  function finishExclusion() {
    const bounds = selectionBounds();
    if (!bounds) return;
    const region: ExclusionRegion = {
      id: sampleId("exclude"),
      kind: "unusable",
      start_sample: bounds[0],
      end_sample: bounds[1],
      note: "",
    };
    onChange({
      ...annotationRef.current,
      exclusions: [...annotationRef.current.exclusions, region],
    });
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

  function setLoopPoint() {
    const time = wave.current?.getCurrentTime() ?? current;
    if (loopAnchor === null) {
      setLoopAnchor(time);
      return;
    }
    const bounds: [number, number] = [
      Math.min(loopAnchor, time),
      Math.max(loopAnchor, time),
    ];
    setLoopAnchor(null);
    if (bounds[1] - bounds[0] >= 0.05) setLoopRange(bounds);
  }

  function seekRelative(delta: number) {
    const instance = wave.current;
    if (!instance) return;
    instance.setTime(Math.max(0, Math.min(instance.getDuration(), instance.getCurrentTime() + delta)));
  }

  const playheadPosition = `${Math.max(
    0,
    Math.min(100, ((current * SAMPLE_RATE) / durationSamples) * 100),
  )}%`;

  return (
    <div className="editor-stack">
      {videoUrl && (
        <video
          className="source-video"
          ref={video}
          src={videoUrl}
          controls
          onSeeked={() => {
            if (!video.current || !wave.current) return;
            syncing.current = true;
            wave.current.setTime(video.current.currentTime);
            syncing.current = false;
          }}
        />
      )}
      <div className="wave-shell">
        <div ref={container} aria-label="Editable source waveform" />
        {anchor !== null && (
          <div className="selection-hint">
            Selection starts at {anchor.toFixed(2)}s — seek and choose A, B, or Exclude
          </div>
        )}
      </div>
      <div className="transport">
        <button onClick={() => seekRelative(-1)}>−1s</button>
        <button onClick={() => seekRelative(-1 / frameRate)}>− frame</button>
        <button className="primary" onClick={() => wave.current?.playPause()}>
          Play / pause
        </button>
        <button onClick={() => seekRelative(1 / frameRate)}>+ frame</button>
        <button onClick={() => seekRelative(1)}>+1s</button>
        <span className="time-readout">{current.toFixed(2)}s</span>
        <label>
          Speed
          <select value={rate} onChange={(event) => setRate(Number(event.target.value))}>
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
        <button className={loopRange ? "active" : ""} onClick={setLoopPoint}>
          {loopAnchor === null ? "Set loop start" : "Finish loop"}
        </button>
        {loopRange && <button onClick={() => setLoopRange(null)}>Clear loop</button>}
      </div>
      {!readOnly && (
        <div className="annotation-actions">
          <button onClick={beginSelection}>Set selection start [</button>
          <button className="speaker-a" onClick={() => finishActivity("A")}>Finish as A</button>
          <button className="speaker-b" onClick={() => finishActivity("B")}>Finish as B</button>
          <button className="danger-soft" onClick={finishExclusion}>Finish as Exclude</button>
          {onRangeSelect && (
            <button
              onClick={() => {
                const bounds = selectionBounds();
                if (bounds) onRangeSelect(bounds[0], bounds[1]);
              }}
            >
              Finish as transcript segment
            </button>
          )}
          <span className="shortcut-note">Shortcuts: [ then A / B / X / T · arrows seek · ,/. frame-step · L sets loop · space plays</span>
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
                    onClick={() => onRegionClick?.(item.id)}
                    onDoubleClick={() => !readOnly && removeSelected(item.id)}
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
      {!!annotation.exclusions.length && (
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
}
