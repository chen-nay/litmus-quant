/**
 * 全站共用的两份数据：
 * - 数据状态与同步进度：同步中每 3 秒刷新一次，平时 30 秒
 * - 清单（事件、字段、板块）：打开页面时读一次；本地数据从「不够」变成「够了」时重读一次
 */

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

import { api, errorText } from "./api";
import type { BoardsResponse, EventsResponse, FieldsResponse, StatusResponse } from "./types";

interface StatusState {
  data: StatusResponse | null;
  error: string | null;
  refresh: () => Promise<void>;
}

const StatusContext = createContext<StatusState>({
  data: null,
  error: null,
  refresh: async () => {},
});

export function StatusProvider({ children }: { children: ReactNode }) {
  const [data, setData] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setData(await api.status());
      setError(null);
    } catch (reason) {
      setError(errorText(reason));
    }
  }, []);

  const busy = data?.sync.state === "running" || data?.sync.state === "stopping";
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), busy ? 3000 : 30000);
    return () => window.clearInterval(timer);
  }, [busy, refresh]);

  return (
    <StatusContext.Provider value={{ data, error, refresh }}>{children}</StatusContext.Provider>
  );
}

export function useStatus(): StatusState {
  return useContext(StatusContext);
}

export interface Catalog {
  events: EventsResponse | null;
  fields: FieldsResponse | null;
  sw: BoardsResponse | null;
  concept: BoardsResponse | null;
  /** 概念板块不可用的原因（没权限、还没同步） */
  conceptError: string | null;
  error: string | null;
}

const EMPTY_CATALOG: Catalog = {
  events: null,
  fields: null,
  sw: null,
  concept: null,
  conceptError: null,
  error: null,
};

const CatalogContext = createContext<Catalog>(EMPTY_CATALOG);

function settled<T>(result: PromiseSettledResult<T>): T | null {
  return result.status === "fulfilled" ? result.value : null;
}

export function CatalogProvider({ children }: { children: ReactNode }) {
  const ready = useStatus().data?.status.ready ?? false;
  const [catalog, setCatalog] = useState<Catalog>(EMPTY_CATALOG);

  useEffect(() => {
    let alive = true;
    void Promise.allSettled([
      api.events(),
      api.fields(),
      api.boards("sw_industry"),
      api.boards("concept"),
    ]).then(([events, fields, sw, concept]) => {
      if (!alive) return;
      const failed = [events, fields, sw].find((item) => item.status === "rejected");
      setCatalog({
        events: settled(events),
        fields: settled(fields),
        sw: settled(sw),
        concept: settled(concept),
        conceptError: concept.status === "rejected" ? errorText(concept.reason) : null,
        error: failed?.status === "rejected" ? errorText(failed.reason) : null,
      });
    });
    return () => {
      alive = false;
    };
  }, [ready]);

  return <CatalogContext.Provider value={catalog}>{children}</CatalogContext.Provider>;
}

export function useCatalog(): Catalog {
  return useContext(CatalogContext);
}
