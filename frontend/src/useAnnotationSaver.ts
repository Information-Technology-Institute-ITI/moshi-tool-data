import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, jsonRequest } from "./api";
import type { Annotation } from "./types";

export type SaveStatus = "idle" | "saving" | "saved" | "failed";

export type Conflict = {
  /** The complete working copy the reviewer attempted to save. */
  local: Annotation;
  /** The authoritative revision currently on the server. */
  server: Annotation;
};

/** Removes drafts written by versions that saved annotation edits locally. */
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
    // An unavailable store must never break the editor.
  }
}

type Options = {
  sourceId: string;
  onConflict: (conflict: Conflict) => void;
  onError: (message: string) => void;
};

/**
 * Sends one annotation snapshot only when the reviewer explicitly asks.
 *
 * There is deliberately no timer, queue, blur handler or navigation flush in
 * this hook. The caller owns the working document and decides exactly which
 * snapshot becomes the next immutable server revision.
 */
export function useAnnotationSaver({ sourceId, onConflict, onError }: Options) {
  const [status, setStatus] = useState<SaveStatus>("idle");
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const callbacks = useRef({ onConflict, onError });
  callbacks.current = { onConflict, onError };

  const save = useCallback(
    async (value: Annotation): Promise<Annotation | null> => {
      if (inFlight.current) return null;
      inFlight.current = true;
      if (mounted.current) setStatus("saving");
      try {
        const saved = await api<Annotation>(
          `/api/sources/${sourceId}/annotations`,
          jsonRequest("PUT", { expected_version: value.version, annotation: value }),
        );
        if (mounted.current) setStatus("saved");
        return saved;
      } catch (reason) {
        if (mounted.current) setStatus("failed");
        if (reason instanceof ApiError && reason.status === 409) {
          try {
            const envelope = await api<{ annotation: Annotation }>(
              `/api/sources/${sourceId}/annotations`,
            );
            const server = envelope?.annotation;
            if (!server || !Array.isArray(server.transcript)) {
              throw new Error("The server sent an annotation this app cannot read.");
            }
            if (mounted.current) callbacks.current.onConflict({ local: value, server });
          } catch {
            if (mounted.current) {
              callbacks.current.onError(
                "This annotation changed elsewhere and the latest revision could not be loaded.",
              );
            }
          }
        } else if (mounted.current) {
          callbacks.current.onError(reason instanceof Error ? reason.message : String(reason));
        }
        return null;
      } finally {
        inFlight.current = false;
      }
    },
    [sourceId],
  );

  const reset = useCallback(() => setStatus("idle"), []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  return {
    status,
    save,
    reset,
    isSaving: () => inFlight.current,
  };
}
