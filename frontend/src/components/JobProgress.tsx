import type { Job } from "../types";

export default function JobProgress({
  job,
  onRetry,
}: {
  job: Job | null;
  onRetry: (job: Job) => void;
}) {
  if (!job) return null;
  return (
    <div className={`job-progress ${job.status}`}>
      <div className="job-copy">
        <strong>{job.kind.replaceAll("_", " ")}</strong>
        <span>{job.error || job.message || job.status}</span>
        {job.status === "failed" && (
          <button onClick={() => onRetry(job)}>Retry</button>
        )}
      </div>
      <div className="progress-track" aria-label={`${Math.round(job.progress * 100)} percent`}>
        <span style={{ width: `${Math.max(2, job.progress * 100)}%` }} />
      </div>
    </div>
  );
}
