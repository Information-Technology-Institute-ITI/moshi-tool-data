import { useEffect, useMemo, useRef, useState } from "react";
import { api, jsonRequest, seconds } from "../api";

type EvaluationOption = {
  source_id: string;
  original_name: string;
  project_id: string;
  project_name: string;
  corrected_version_ordinal: number;
  machine_ordinal: number;
  model_name: string;
  producer: string;
  provenance_status: "exact" | "historical_unknown";
};

type Metric = {
  distance: number;
  substitutions: number;
  insertions: number;
  deletions: number;
  reference_units: number;
  hypothesis_units: number;
  rate: number;
};

type EvaluationResult = {
  contract_version: string;
  word_error: Metric;
  character_error: Metric;
  normalized_word_error: Metric;
  normalized_character_error: Metric;
  coverage: number;
  evaluated_duration_samples: number;
  eligible_duration_samples: number;
  inputs: {
    source_id: string;
    machine_transcript: string;
    corrected_version: string;
    annotation_version: number;
    content_fingerprint: string;
  };
  diffs: {
    segment_id: string;
    speaker?: string | null;
    start_sample: number;
    end_sample: number;
    reference: string;
    hypothesis: string;
    word_operations: { operation: string; reference?: string | null; hypothesis?: string | null }[];
  }[];
};

type EvaluationReport = {
  id: string;
  source_id: string;
  original_name: string;
  project_id: string;
  project_name: string;
  machine_ordinal: number;
  corrected_version_ordinal: number;
  annotation_version: number;
  normalization_policy: string;
  overlap_policy: string;
  result: EvaluationResult;
  created_at: string;
};

function rate(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(2)}%`;
}

export default function EvaluationPage({
  setError,
  onOpenSource,
}: {
  setError: (message: string) => void;
  onOpenSource: (sourceId: string, projectId: string) => void;
}) {
  const [options, setOptions] = useState<EvaluationOption[]>([]);
  const [reports, setReports] = useState<EvaluationReport[]>([]);
  const [selection, setSelection] = useState("");
  const [normalization, setNormalization] = useState("arabic-normalized-v1");
  const [overlap, setOverlap] = useState("deduplicate");
  const [speaker, setSpeaker] = useState("both");
  const [preview, setPreview] = useState<EvaluationResult | null>(null);
  const [working, setWorking] = useState(false);
  const audio = useRef<HTMLAudioElement | null>(null);
  const stopAt = useRef<number | null>(null);

  const selected = useMemo(() => options.find((_, index) => String(index) === selection) || null, [options, selection]);

  async function load() {
    const [optionEnvelope, reportEnvelope] = await Promise.all([
      api<{ options: EvaluationOption[] }>("/api/admin/evaluations/options"),
      api<{ reports: EvaluationReport[] }>("/api/admin/evaluations"),
    ]);
    setOptions(optionEnvelope.options);
    setReports(reportEnvelope.reports);
    setSelection((current) => current || (optionEnvelope.options.length ? "0" : ""));
  }

  useEffect(() => {
    void load().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, []);

  function requestBody() {
    if (!selected) return null;
    return {
      source_id: selected.source_id,
      machine_ordinal: selected.machine_ordinal,
      corrected_version_ordinal: selected.corrected_version_ordinal,
      speakers: speaker === "both" ? [] : [speaker],
      verified_only: true,
      normalization_policy: normalization,
      overlap_policy: overlap,
    };
  }

  async function evaluate(persist: boolean) {
    const body = requestBody();
    if (!body || working) return;
    setWorking(true);
    try {
      if (persist) {
        const report = await api<EvaluationReport>(
          "/api/admin/evaluations",
          jsonRequest("POST", body),
        );
        setPreview(report.result);
        await load();
      } else {
        setPreview(await api<EvaluationResult>(
          "/api/admin/evaluations/preview",
          jsonRequest("POST", body),
        ));
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setWorking(false);
    }
  }

  function playRange(startSample: number, endSample: number) {
    if (!audio.current) return;
    audio.current.currentTime = startSample / 24_000;
    stopAt.current = endSample / 24_000;
    void audio.current.play();
  }

  return (
    <section className="page evaluation-page">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Administrator only</span>
          <h1>WER/CER evaluation</h1>
          <p>Compare one immutable machine transcript with one approved corrected version.</p>
        </div>
        <span>{reports.length} immutable report{reports.length === 1 ? "" : "s"}</span>
      </div>

      <div className="evaluation-controls card">
        <label>
          Fixed inputs
          <select value={selection} onChange={(event) => { setSelection(event.target.value); setPreview(null); }}>
            {options.map((option, index) => (
              <option key={`${option.source_id}-${option.machine_ordinal}-${option.corrected_version_ordinal}`} value={index}>
                {option.project_name} · {option.original_name} · M{option.machine_ordinal} vs V{option.corrected_version_ordinal}
              </option>
            ))}
          </select>
        </label>
        <label>
          Text policy
          <select value={normalization} onChange={(event) => { setNormalization(event.target.value); setPreview(null); }}>
            <option value="strict-v1">Strict v1</option>
            <option value="arabic-normalized-v1">Arabic normalized v1</option>
          </select>
        </label>
        <label>
          Overlap policy
          <select value={overlap} onChange={(event) => { setOverlap(event.target.value); setPreview(null); }}>
            <option value="deduplicate">Deduplicate copied text</option>
            <option value="include">Include both speakers</option>
            <option value="exclude">Exclude overlap ranges</option>
          </select>
        </label>
        <label>
          Speaker
          <select value={speaker} onChange={(event) => { setSpeaker(event.target.value); setPreview(null); }}>
            <option value="both">Both speakers</option>
            <option value="A">Speaker A</option>
            <option value="B">Speaker B</option>
          </select>
        </label>
        <div className="evaluation-actions">
          <button type="button" disabled={!selected || working} onClick={() => void evaluate(false)}>Preview</button>
          <button type="button" className="primary" disabled={!selected || working || !preview} onClick={() => void evaluate(true)}>Save immutable report</button>
          {selected && <button type="button" onClick={() => onOpenSource(selected.source_id, selected.project_id)}>Open source to correct</button>}
        </div>
        {selected?.provenance_status === "historical_unknown" && (
          <small>The M artifact is checksum verified; its historical model/config metadata was not stored.</small>
        )}
      </div>

      {selected && (
        <audio
          ref={audio}
          className="evaluation-audio"
          controls
          src={`/media/${selected.source_id}/canonical`}
          onTimeUpdate={() => {
            if (audio.current && stopAt.current !== null && audio.current.currentTime >= stopAt.current) {
              audio.current.pause();
              stopAt.current = null;
            }
          }}
        />
      )}

      {preview && (
        <>
          <div className="evaluation-metrics">
            <MetricCard label="WER strict" metric={preview.word_error} />
            <MetricCard label="CER strict" metric={preview.character_error} />
            <MetricCard label="WER selected policy" metric={preview.normalized_word_error} />
            <MetricCard label="CER selected policy" metric={preview.normalized_character_error} />
            <div className="card evaluation-metric"><small>Coverage</small><strong>{rate(preview.coverage)}</strong><span>{(preview.evaluated_duration_samples / 24_000).toFixed(1)}s evaluated</span></div>
          </div>
          <section className="card evaluation-diffs">
            <h2>Time-aligned differences</h2>
            {preview.diffs.map((diff) => (
              <button type="button" key={diff.segment_id} onClick={() => playRange(diff.start_sample, diff.end_sample)}>
                <span><strong>{diff.speaker || "?"}</strong> {seconds(diff.start_sample)}–{seconds(diff.end_sample)}s</span>
                <span dir="auto"><small>Machine</small>{diff.hypothesis || "(empty)"}</span>
                <span dir="auto"><small>Corrected</small>{diff.reference || "(empty)"}</span>
              </button>
            ))}
          </section>
        </>
      )}

      <section className="card evaluation-history">
        <h2>Report history</h2>
        {reports.map((report) => (
          <div key={report.id}>
            <button type="button" onClick={() => setPreview(report.result)}>
              <strong>{report.original_name}</strong>
              <span>M{report.machine_ordinal} vs V{report.corrected_version_ordinal} · WER {rate(report.result.normalized_word_error.rate)}</span>
              <small>{new Date(report.created_at).toLocaleString()}</small>
            </button>
            <a href={`/api/admin/evaluations/${report.id}/download`}>Download JSON</a>
          </div>
        ))}
        {!reports.length && <p>No saved evaluation reports yet.</p>}
      </section>
      <p className="evaluation-version-note">
        Evaluation never edits the approved V version. “Open source to correct” creates a new V on Save, preserving the evaluated version and report.
        Retention remains an explicit admin action under Approvals; a version referenced by an evaluation report cannot have its payload removed.
      </p>
    </section>
  );
}

function MetricCard({ label, metric }: { label: string; metric: Metric }) {
  return (
    <div className="card evaluation-metric">
      <small>{label}</small>
      <strong>{rate(metric.rate)}</strong>
      <span>S {metric.substitutions} · I {metric.insertions} · D {metric.deletions} / {metric.reference_units}</span>
    </div>
  );
}
