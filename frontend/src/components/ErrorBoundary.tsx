import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = {
  children: ReactNode;
  /** Offered as a way out that keeps the session, when the caller has one. */
  onReset?: () => void;
  resetLabel?: string;
};

type State = { error: Error | null };

/**
 * Catches render errors so a mistake in one screen cannot blank the whole page.
 *
 * React unmounts the entire tree when a render throws, which showed up as a
 * white page that only a browser reload recovered from.
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Kept for the browser console; there is no error reporting service here.
    console.error("Unhandled render error", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="error-boundary" role="alert">
        <h2>Something on this screen stopped working</h2>
        <p>
          The page recovered instead of going blank. Reopening the source loads its
          last successful save; edits made since then may need to be repeated.
        </p>
        <pre className="error-boundary-detail">{error.message}</pre>
        <div className="error-boundary-actions">
          {this.props.onReset && (
            <button
              type="button"
              className="primary"
              onClick={() => {
                this.setState({ error: null });
                this.props.onReset?.();
              }}
            >
              {this.props.resetLabel || "Go back"}
            </button>
          )}
          <button type="button" onClick={() => window.location.reload()}>
            Reload the page
          </button>
        </div>
      </div>
    );
  }
}
