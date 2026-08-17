import { FormEvent, useEffect, useRef, useState } from "react";
import {
  activateAccount,
  resendActivation,
  signin,
  signup,
} from "../api";
import type { AuthUser } from "../types";

type Mode = "signin" | "signup";

function activationToken(): string | null {
  const [, query = ""] = window.location.hash.split("?", 2);
  return new URLSearchParams(query).get("token");
}

function message(value: unknown, fallback: string): string {
  return value instanceof Error ? value.message : fallback;
}

export default function AuthScreen({
  onAuthenticated,
  onBack,
}: {
  onAuthenticated: (user: AuthUser) => void;
  onBack?: () => void;
}) {
  const [mode, setMode] = useState<Mode>("signin");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const activationStarted = useRef(false);

  useEffect(() => {
    const token = activationToken();
    if (!token || activationStarted.current) return;
    activationStarted.current = true;
    setBusy(true);
    setNotice("Activating your account...");
    activateAccount(token)
      .then(({ user }) => {
        setEmail(user.email);
        setMode("signin");
        setNotice("Account activated. Sign in to continue.");
        window.history.replaceState(
          null,
          "",
          `${window.location.pathname}${window.location.search}`,
        );
      })
      .catch((value) => {
        setNotice("");
        setError(message(value, "The activation link is invalid or expired."));
      })
      .finally(() => setBusy(false));
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (mode === "signup") {
        const response = await signup({
          display_name: displayName,
          email,
          password,
        });
        setPassword("");
        setNotice(response.message);
        return;
      }
      const response = await signin(email, password);
      onAuthenticated(response.user);
    } catch (value) {
      setError(message(value, "Authentication failed."));
    } finally {
      setBusy(false);
    }
  }

  async function resend() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const response = await resendActivation(email);
      setNotice(response.message);
    } catch (value) {
      setError(message(value, "The activation email could not be sent."));
    } finally {
      setBusy(false);
    }
  }

  function switchMode(next: Mode) {
    setMode(next);
    setError("");
    setNotice("");
    setPassword("");
  }

  return (
    <main className="auth-page">
      <section className="auth-intro" aria-labelledby="auth-title">
        <span className="brand-mark auth-brand-mark">M</span>
        <p className="eyebrow">Moshi Dataset Studio</p>
        <h1 id="auth-title">Dataset operations, secured.</h1>
        <p>
          Sign in to manage source audio, GPU processing, review, and export
          from the shared m8i workspace.
        </p>
      </section>

      <section className="auth-panel" aria-label="Account access">
        {onBack && (
          <button className="auth-back" type="button" onClick={onBack}>
            Back to introduction
          </button>
        )}
        <div className="auth-tabs" role="tablist" aria-label="Account action">
          <button
            type="button"
            role="tab"
            aria-selected={mode === "signin"}
            className={mode === "signin" ? "active" : ""}
            onClick={() => switchMode("signin")}
          >
            Sign in
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "signup"}
            className={mode === "signup" ? "active" : ""}
            onClick={() => switchMode("signup")}
          >
            Create account
          </button>
        </div>

        <form className="auth-form" onSubmit={submit}>
          <div>
            <span className="eyebrow">
              {mode === "signin" ? "Welcome back" : "Email verification required"}
            </span>
            <h2>{mode === "signin" ? "Sign in" : "Create your account"}</h2>
            <p>
              {mode === "signin"
                ? "Use your activated account."
                : "We will email you a link before your first sign in."}
            </p>
          </div>

          {mode === "signup" && (
            <label>
              Name
              <input
                autoComplete="name"
                maxLength={120}
                required
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
              />
            </label>
          )}
          <label>
            Email
            <input
              autoComplete="email"
              inputMode="email"
              type="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>
          <label>
            Password
            <input
              autoComplete={mode === "signin" ? "current-password" : "new-password"}
              minLength={8}
              maxLength={256}
              type="password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>

          {error && <div className="auth-message error" role="alert">{error}</div>}
          {notice && <div className="auth-message success" role="status">{notice}</div>}

          <button className="primary auth-submit" disabled={busy} type="submit">
            {busy
              ? "Please wait..."
              : mode === "signin"
                ? "Sign in"
                : "Send activation email"}
          </button>
          <button
            className="auth-resend"
            disabled={busy || !email}
            type="button"
            onClick={resend}
          >
            Resend activation email
          </button>
        </form>
      </section>
    </main>
  );
}
