import { performance } from "node:perf_hooks";

function annotation(segmentCount, wordCount) {
  return {
    source_id: "source_baseline",
    version: 12,
    activities: Array.from({ length: segmentCount }, (_, index) => ({
      id: `activity_${index}`,
      speaker: index % 2 ? "B" : "A",
      start_sample: index * 360_000,
      end_sample: (index + 1) * 360_000,
      origin: "model",
    })),
    exclusions: [],
    transcript: Array.from({ length: segmentCount }, (_, index) => ({
      id: `utterance_${index}`,
      speaker: index % 2 ? "B" : "A",
      start_sample: index * 360_000,
      end_sample: (index + 1) * 360_000,
      text: `اختبار المقطع رقم ${index}`,
      model_text: `اختبار المقطع رقم ${index}`,
      quality_flags: index % 20 === 0 ? ["review"] : [],
      human_verified: false,
    })),
    aligned_words: Array.from({ length: wordCount }, (_, index) => ({
      word: `word-${index}`,
      start: index * 0.5,
      end: index * 0.5 + 0.3,
      score: 0.9,
    })),
  };
}

function median(values) {
  const ordered = [...values].sort((left, right) => left - right);
  return ordered[Math.floor(ordered.length / 2)];
}

function measure(label, value, iterations = 25) {
  const stringify = [];
  const sortAndFilter = [];
  const selectedIds = value.transcript.filter((_, index) => index % 3 === 0).map((item) => item.id);
  for (let iteration = 0; iteration < iterations; iteration += 1) {
    let started = performance.now();
    const { version: _version, ...content } = value;
    JSON.stringify(content);
    stringify.push(performance.now() - started);

    started = performance.now();
    [...value.transcript]
      .sort((left, right) => left.start_sample - right.start_sample)
      .filter((item) => selectedIds.includes(item.id));
    sortAndFilter.push(performance.now() - started);
  }
  return {
    label,
    segments: value.transcript.length,
    aligned_words: value.aligned_words.length,
    serialized_bytes: Buffer.byteLength(JSON.stringify(value)),
    median_stringify_ms: Number(median(stringify).toFixed(3)),
    median_sort_and_includes_filter_ms: Number(median(sortAndFilter).toFixed(3)),
  };
}

const report = {
  runtime: process.version,
  generated_at: new Date().toISOString(),
  note: "Synthetic CPU baseline for the current whole-document operations; not a browser paint metric.",
  cases: [
    measure("short-source", annotation(120, 1_800)),
    measure("long-source", annotation(7_200, 108_000), 10),
  ],
};

process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);

