import { useCallback, useEffect, useState } from "react";
import type { Annotation } from "./types";

const DB_NAME = "moshi-review-drafts";
const STORE_NAME = "drafts";

export type RecoveryDraft = {
  key: string;
  userId: string;
  sourceId: string;
  baseRevision: number;
  savedAt: string;
  annotation: Annotation;
};

function openDraftDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (!window.indexedDB) {
      reject(new Error("IndexedDB is unavailable"));
      return;
    }
    const request = window.indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE_NAME)) {
        const store = database.createObjectStore(STORE_NAME, { keyPath: "key" });
        store.createIndex("userId", "userId");
        store.createIndex("sourceId", "sourceId");
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("Could not open draft storage"));
  });
}

async function withDraftStore<T>(
  mode: IDBTransactionMode,
  action: (store: IDBObjectStore, resolve: (value: T) => void, reject: (reason?: unknown) => void) => void,
): Promise<T> {
  const database = await openDraftDatabase();
  try {
    return await new Promise<T>((resolve, reject) => {
      const transaction = database.transaction(STORE_NAME, mode);
      transaction.onerror = () => reject(transaction.error || new Error("Draft storage failed"));
      action(transaction.objectStore(STORE_NAME), resolve, reject);
    });
  } finally {
    database.close();
  }
}

export async function findRecoveryDraft(userId: string, sourceId: string) {
  return withDraftStore<RecoveryDraft | null>("readonly", (store, resolve, reject) => {
    const request = store.getAll();
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const candidates = (request.result as RecoveryDraft[])
        .filter((item) => item.userId === userId && item.sourceId === sourceId)
        .sort((left, right) => right.savedAt.localeCompare(left.savedAt));
      resolve(candidates[0] || null);
    };
  });
}

export async function purgeRecoveryDrafts(userId: string, sourceId?: string) {
  return withDraftStore<void>("readwrite", (store, resolve, reject) => {
    const request = store.openCursor();
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        resolve();
        return;
      }
      const draft = cursor.value as RecoveryDraft;
      if (draft.userId === userId && (!sourceId || draft.sourceId === sourceId)) cursor.delete();
      cursor.continue();
    };
  });
}

async function putRecoveryDraft(draft: RecoveryDraft) {
  return withDraftStore<void>("readwrite", (store, resolve, reject) => {
    const request = store.put(draft);
    request.onerror = () => reject(request.error);
    request.onsuccess = () => resolve();
  });
}

export function draftMatchesRevision(draft: RecoveryDraft, revision: number) {
  return draft.baseRevision === revision;
}

export function useDraftRecovery({
  userId,
  sourceId,
  baseRevision,
  annotation,
  dirty,
  enabled,
}: {
  userId: string;
  sourceId: string;
  baseRevision: number;
  annotation: Annotation;
  dirty: boolean;
  enabled: boolean;
}) {
  const [draft, setDraft] = useState<RecoveryDraft | null>(null);
  const [available, setAvailable] = useState(true);

  const refresh = useCallback(async () => {
    if (!enabled) return setDraft(null);
    try {
      setDraft(await findRecoveryDraft(userId, sourceId));
    } catch {
      setAvailable(false);
    }
  }, [enabled, sourceId, userId]);

  useEffect(() => {
    setAvailable(true);
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!enabled || !dirty || !available) return;
    const timeout = window.setTimeout(() => {
      const record: RecoveryDraft = {
        key: `${userId}:${sourceId}:v${baseRevision}`,
        userId,
        sourceId,
        baseRevision,
        savedAt: new Date().toISOString(),
        annotation,
      };
      void putRecoveryDraft(record).catch(() => setAvailable(false));
    }, 700);
    return () => window.clearTimeout(timeout);
  }, [annotation, available, baseRevision, dirty, enabled, sourceId, userId]);

  const purge = useCallback(async () => {
    setDraft(null);
    try {
      await purgeRecoveryDrafts(userId, sourceId);
    } catch {
      setAvailable(false);
    }
  }, [sourceId, userId]);

  return {
    draft,
    available,
    conflict: !!draft && !draftMatchesRevision(draft, baseRevision),
    dismiss: () => setDraft(null),
    purge,
    refresh,
  };
}
