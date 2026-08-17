import { useEffect, useRef, useState } from "react";

export default function StereoPlayer({ src }: { src: string }) {
  const audio = useRef<HTMLAudioElement>(null);
  const graph = useRef<{
    context: AudioContext;
    left: GainNode;
    right: GainNode;
  } | null>(null);
  const [leftOn, setLeftOn] = useState(true);
  const [rightOn, setRightOn] = useState(true);

  useEffect(() => {
    return () => {
      void graph.current?.context.close();
    };
  }, []);

  function ensureGraph() {
    if (graph.current || !audio.current) return graph.current;
    const context = new AudioContext();
    const source = context.createMediaElementSource(audio.current);
    const split = context.createChannelSplitter(2);
    const left = context.createGain();
    const right = context.createGain();
    const merge = context.createChannelMerger(2);
    source.connect(split);
    split.connect(left, 0);
    split.connect(right, 1);
    left.connect(merge, 0, 0);
    right.connect(merge, 0, 1);
    merge.connect(context.destination);
    graph.current = { context, left, right };
    return graph.current;
  }

  function toggle(channel: "left" | "right") {
    const value = ensureGraph();
    if (!value) return;
    if (channel === "left") {
      const next = !leftOn;
      value.left.gain.value = next ? 1 : 0;
      setLeftOn(next);
    } else {
      const next = !rightOn;
      value.right.gain.value = next ? 1 : 0;
      setRightOn(next);
    }
  }

  return (
    <div className="stereo-player">
      <audio ref={audio} src={src} controls onPlay={() => ensureGraph()?.context.resume()} />
      <div className="transport compact">
        <button className={leftOn ? "active" : ""} onClick={() => toggle("left")}>
          Moshi · left
        </button>
        <button className={rightOn ? "active" : ""} onClick={() => toggle("right")}>
          User · right
        </button>
      </div>
    </div>
  );
}
