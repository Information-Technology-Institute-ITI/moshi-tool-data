import type { ReactNode } from "react";

export default function ReviewPlayerCard({
  processing,
  children,
}: {
  processing: boolean;
  children: ReactNode;
}) {
  return (
    <section className="review-player-card card" aria-label="Review player">
      <header className="review-player-heading">
        <div>
          <span className="eyebrow">Review audio and transcript</span>
          <h1>Check what was said, and when.</h1>
        </div>
        {processing && <span className="pill">Preparing</span>}
      </header>
      {children}
    </section>
  );
}
