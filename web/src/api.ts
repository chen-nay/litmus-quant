/** 调后端接口。开发时 Vite 把 /api 转给 127.0.0.1:8000（vite.config.ts）。 */

import type {
  BoardType,
  BoardsResponse,
  EventsResponse,
  FieldsResponse,
  KlineResponse,
  RunRecord,
  RunResponse,
  Spec,
  StatusResponse,
  SyncProgress,
} from "./types";

const OFFLINE = "连不上后端：先在仓库目录运行 uv run python -m litmus serve";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new ApiError(0, OFFLINE);
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") throw new ApiError(response.status, detail);
    // 后端没起来时，Vite 的转发会回一个没有内容的 5xx
    const message = response.status >= 500 ? OFFLINE : `请求失败（HTTP ${response.status}）`;
    throw new ApiError(response.status, message);
  }
  return body as T;
}

function post<T>(url: string, body?: unknown): Promise<T> {
  return request<T>(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export const api = {
  status: () => request<StatusResponse>("/api/data/status"),
  startSync: () => post<{ started: boolean; sync: SyncProgress }>("/api/data/sync"),
  stopSync: () => post<{ stopped: boolean; sync: SyncProgress }>("/api/data/sync/stop"),
  events: () => request<EventsResponse>("/api/events"),
  boards: (type: BoardType) => request<BoardsResponse>(`/api/boards?type=${type}`),
  fields: () => request<FieldsResponse>("/api/fields"),
  run: (spec: Spec) => post<RunResponse>("/api/run", { spec }),
  record: (runId: string) => request<RunRecord>(`/api/run/${encodeURIComponent(runId)}`),
  kline: (code: string, from: string, to: string) =>
    request<KlineResponse>(
      `/api/stocks/${encodeURIComponent(code)}/kline?from=${from}&to=${to}`,
    ),
};

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
