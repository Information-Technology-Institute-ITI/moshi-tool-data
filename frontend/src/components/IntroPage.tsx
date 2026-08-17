import type { AuthUser } from "../types";

export default function IntroPage({
  user,
  onSignIn,
  onEnter,
}: {
  user: AuthUser | null;
  onSignIn: () => void;
  onEnter: () => void;
}) {
  return (
    <main className="intro-page">
      <header className="intro-nav">
        <div className="intro-brand">
          <span className="brand-mark">M</span>
          <span>
            <strong>Moshi Dataset Studio</strong>
            <small>Human-guided full-duplex audio</small>
          </span>
        </div>
        {user ? (
          <button className="intro-nav-action" type="button" onClick={onEnter}>
            Open workspace
          </button>
        ) : (
          <button className="intro-nav-action" type="button" onClick={onSignIn}>
            Sign in
          </button>
        )}
      </header>

      <section className="intro-hero" aria-labelledby="intro-title">
        <div className="intro-hero-shade" />
        <div className="intro-copy">
          <span className="eyebrow">Egyptian Arabic dialogue operations</span>
          <h1 id="intro-title">Moshi Dataset Studio</h1>
          <p>
            Prepare two-speaker recordings, validate WhisperX processing, resolve
            overlap, and publish only the dialogue clips your team has reviewed.
          </p>
          <div className="intro-actions">
            <button className="primary" type="button" onClick={user ? onEnter : onSignIn}>
              {user ? "Open workspace" : "Sign in"}
            </button>
          </div>
          {user && (
            <small className="intro-session">
              Signed in as {user.display_name}
            </small>
          )}
        </div>
      </section>

      <section className="intro-capabilities" aria-label="Studio capabilities">
        <div>
          <span>01</span>
          <strong>Prepare</strong>
          <p>Upload authorized source media and preserve immutable originals.</p>
        </div>
        <div>
          <span>02</span>
          <strong>Process</strong>
          <p>Wake the remote T4, validate WhisperX, and track durable jobs.</p>
        </div>
        <div>
          <span>03</span>
          <strong>Review</strong>
          <p>Correct speech, speakers, overlap, timing, and final clip quality.</p>
        </div>
      </section>
    </main>
  );
}
