import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, jsonRequest } from "./api";
import type { Annotation } from "./types";

export const AUTOSAVE_DEBOUNCE_MS = 1_200;

export type SaveStatus = "idle" | "saving" | "saved" | "failed";

export type Conflict = {
  /** The edits the user still has, rebased onto nothing yet. */
  local: Annotation;
  /** The authoritative revision currently on the server. */
  server: Annotation;
};

/**
 * A recoverable local draft. Scoped by user, source, and the server version it
 * was based on, so one user's draft can never surface for another, and a draft
 * from an older base is never silently applied to a newer one.
 */
function draftKey(userId: string, sourceId: string, baseVersion: number): string {
  return `moshi.draft.${userId}.${sourceId}.v${baseVersion}`;
}

export function readDraft(
  userId: string,
  sourceId: string,
  baseVersion: number,
): Annotation | null {
  try {
    const raw = window.localStorage.getItem(draftKey(userId, sourceId, baseVersion));
    if (!raw) return null;
    const draft = JSON.parse(raw) as Annotation;
    // A draft written by an older build could be missing a list the review
    // screen reads, which would throw while rendering. Ignoring it costs the
    // user one recovery offer; installing it would blank the screen.
    const lists = [
      draft?.transcript,
      draft?.activities,
      draft?.exclusions,
      draft?.aligned_words,
    ];
    return lists.every(Array.isArray) ? draft : null;
  } catch {
    return null;
  }
}

function writeDraft(
  userId: string,
  sourceId: string,
  baseVersion: number,
  annotation: Annotation,
) {
  try {
    window.localStorage.setItem(
      draftKey(userId, sourceId, baseVersion),
      JSON.stringify(annotation),
    );
  } catch {
    // A full or unavailable store must never break editing.
  }
}

export function clearDraft(userId: string, sourceId: string, baseVersion: number) {
  try {
    window.localStorage.removeItem(draftKey(userId, sourceId, baseVersion));
  } catch {
    // ignore
  }
}

/** Removes every draft belonging to a user. Used on sign-out. */
export function clearDraftsForUser(userId: string) {
  try {
    const prefix = `moshi.draft.${userId}.`;
    const doomed: string[] = [];
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (key?.startsWith(prefix)) doomed.push(key);
    }
    doomed.forEach((key) => window.localStorage.removeItem(key));
  } catch {
    // ignore
  }
}

type Options = {
  sourceId: string;
  userId: string;
  onSaved: (saved: Annotation) => void;
  onConflict: (conflict: Conflict) => void;
  onError: (message: string) => void;
};

/**
 * Debounced, serialized annotation autosave.
 *
 * Saves carry `expected_version` and are sent one at a time per source; rapid
 * edits coalesce into the newest complete document. A save never enqueues GPU
 * work. On failure the local draft is kept so nothing the user typed is lost.
 */
export function useAnnotationSaver({
  sourceId,
  userId,
  onSaved,
  onConflict,
  onError,
}: Options) {
  const [status, setStatus] = useState<SaveStatus>("idle");
  const timer = useRef<number | null>(null);
  const inFlight = useRef(false);
  const queued = useRef<Annotation | null>(null);
  const latest = useRef<Annotation | null>(null);
  const dirty = useRef(false);
  const callbacks = useRef({ onSaved, onConflict, onError });
  callbacks.current = { onSaved, onConflict, onError };

  const send = useCallback(
    async (value: Annotation) => {
      if (timer.current) {
        window.clearTimeout(timer.current);
        timer.current = null;
      }
      if (inFlight.current) {
        // Coalesce: only the newest complete document is worth sending next.
        queued.current = value;
        return;
      }
      inFlight.current = true;
      setStatus("saving");
      // The draft is written before the request so a crash mid-flight still
      // leaves the user's content recoverable.
      writeDraft(userId, sourceId, value.version, value);
      try {
        const saved = await api<Annotation>(
          `/api/sources/${sourceId}/annotations`,
          jsonRequest("PUT", { expected_version: value.version, annotation: value }),
        );
        clearDraft(userId, sourceId, value.version);
        dirty.current = false;
        setStatus("saved");
        callbacks.current.onSaved(saved);
      } catch (reason) {
        setStatus("failed");
        if (reason instanceof ApiError && reason.status === 409) {
          // Never overwrite the server and never discard local content.
          try {
            // This endpoint answers with an envelope, not a bare annotation.
            // Reading it as one left `server.transcript` undefined and the
            // conflict dialog threw while rendering it.
            const envelope = await api<{ annotation: Annotation }>(
              `/api/sources/${sourceId}/annotations`,
            );
            const server = envelope?.annotation;
            if (!server || !Array.isArray(server.transcript)) {
              throw new Error("The server sent an annotation this app cannot read.");
            }
            callbacks.current.onConflict({ local: value, server });
          } catch {
            callbacks.current.onError(
              "This annotation changed elsewhere and the latest revision could not be loaded.",
            );
          }
        } else {
          callbacks.current.onError(
            reason instanceof Error ? reason.message : String(reason),
          );
        }
      } finally {
        inFlight.current = false;
        const next = queued.current;
        queued.current = null;
        if (next) void send(next);
      }
    },
    [sourceId, userId],
  );

  /** Records an edit and schedules a debounced save. */
  const schedule = useCallback(
    (value: Annotation) => {
      latest.current = value;
      dirty.current = true;
      writeDraft(userId, sourceId, value.version, value);
      if (timer.current) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        timer.current = null;
        void send(value);
      }, AUTOSAVE_DEBOUNCE_MS);
    },
    [send, sourceId, userId],
  );

  const saveNow = useCallback(() => {
    if (latest.current) void send(latest.current);
  }, [send]);

  /**
   * Flushes pending work and awaits the active save. Used before in-app
   * navigation so the newest edit is never silently lost. Resolves false when
   * the save failed, letting the caller stay put.
   */
  const flush = useCallback(async (): Promise<boolean> => {
    if (timer.current) {
      window.clearTimeout(timer.current);
      timer.current = null;
      if (latest.current) void send(latest.current);
    }
    while (inFlight.current || queued.current) {
      await new Promise((resolve) => window.setTimeout(resolve, 40));
    }
    return !dirty.current;
  }, [send]);

  const reset = useCallback((value: Annotation) => {
    if (timer.current) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
    queued.current = null;
    latest.current = value;
    dirty.current = false;
    setStatus("idle");
  }, []);

  useEffect(
    () => () => {
      if (timer.current) window.clearTimeout(timer.current);
    },
    [],
  );

  // A truthful warning only. The browser cannot be relied on to finish a
  // network save during shutdown, so nothing is promised here.
  useEffect(() => {
    function beforeUnload(event: BeforeUnloadEvent) {
      if (!dirty.current) return;
      event.preventDefault();
      event.returnValue = "";
    }
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, []);

  return { status, schedule, saveNow, flush, reset, hasUnsaved: () => dirty.current };
}
