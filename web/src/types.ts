/** 接口的请求、响应类型，对应 litmus/api（ARCHITECTURE §6）。 */

export type Target = "stock" | "sw_industry" | "concept";
export type BoardType = "sw_industry" | "concept";

// ── 查询条件（spec）────────────────────────────────────────────

export interface Condition {
  expr: string;
  label: string;
}

export interface Sort {
  by: string;
  order: "asc" | "desc";
  label: string;
}

export interface Universe {
  base: "all_a" | "hs300" | "zz500";
  industry: string | null;
  board: { type: "concept"; code: string } | null;
  exclude: string[];
}

export interface StockListSpec {
  shape: "stock_list";
  as_of: string;
  filter: Condition | null;
  sort: Sort | null;
  limit: number;
  universe: Universe;
}

export interface BoardListSpec {
  shape: "board_list";
  board_type: BoardType;
  as_of: string;
  filter: Condition | null;
  sort: Sort | null;
  limit: number;
}

export interface EventRef {
  preset_id: string;
  params: Record<string, number>;
  label?: string;
  library_version?: number | null;
}

export interface StockHistorySpec {
  shape: "stock_history";
  target: { code: string };
  event: EventRef;
  time_range: { from: string; to: string };
  horizons: number[];
  benchmark: string;
  cost_bps: number;
}

export type Spec = StockListSpec | BoardListSpec | StockHistorySpec;

// ── 结果 ────────────────────────────────────────────────────────

export type Cell = string | number | boolean | null;
export type Row = Record<string, Cell>;

export interface ListResult {
  shape: "stock_list" | "board_list";
  as_of: string;
  total: number;
  columns: string[];
  rows: Row[];
  notes: string[];
}

export interface Delay {
  days: number;
  reason: string;
}

/** 按持有天数分档的字段，key 是持有天数（JSON 里是字符串 "5"） */
export interface TriggerRecord {
  trigger_date: string;
  entry_date: string | null;
  entry_delay: Delay | null;
  status: Record<string, string>;
  exit_date: Record<string, string | null>;
  exit_delay: Record<string, Delay | null>;
  returns: Record<string, number | null>;
  market_returns: Record<string, number | null>;
  market_excluded: Record<string, number>;
  notes: string[];
}

export interface HorizonSummary {
  n: number;
  mean_return: number | null;
  mean_return_after_cost: number | null;
  mean_market_return: number | null;
  mean_baseline_return: number | null;
  win_rate: number | null;
  unfilled: number;
  pending: number;
}

export interface HistoryResult {
  shape: "stock_history";
  code: string;
  name: string | null;
  event_label: string;
  range: [string, string];
  benchmark: string;
  cost_bps: number;
  triggers: TriggerRecord[];
  summary: Record<string, HorizonSummary>;
  notes: string[];
}

export type Result = ListResult | HistoryResult;

export interface Issue {
  path: string | null;
  message: string;
  allowed: string | null;
  position: number | null;
}

export interface RunResponse {
  status: "done" | "needs_revision" | "data_not_ready" | "failed";
  issues: Issue[];
  run_id: string | null;
  result: Result | null;
  message: string | null;
  data: StatusResponse | null;
}

export interface RunRecord {
  run_id: string;
  created_at: string;
  status: "done" | "failed";
  spec: Spec;
  result: Result | null;
  error: string | null;
  plan_id: string | null;
  data_through: string | null;
  library_version: number | null;
  duration_ms: number | null;
}

// ── 数据状态与同步 ──────────────────────────────────────────────

export interface DataStatus {
  ready: boolean;
  reason: string;
  data_through: string | null;
  history_from: string | null;
  history_done: boolean;
  unlock_months: string[];
  unlock_missing: string[];
  synced_at: Record<string, string>;
  unavailable: Record<string, string>;
}

export type SyncState = "idle" | "running" | "stopping" | "stopped" | "failed" | "done";

export interface SyncProgress {
  state: SyncState;
  step: string | null;
  steps_done: string[];
  steps_failed: string[];
  daily_month: string | null;
  daily_done: number;
  daily_total: number;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface StatusResponse {
  status: DataStatus;
  sync: SyncProgress;
}

// ── 清单 ────────────────────────────────────────────────────────

export interface EventParam {
  name: string;
  label: string;
  unit: string;
  allowed: string;
  default: number;
  kind: "choice" | "int" | "float";
  choices: number[];
  min: number | null;
  max: number | null;
}

export interface EventInfo {
  id: string;
  name: string;
  category: string;
  template: string;
  label: string;
  params: EventParam[];
  constraints: string[];
  example: { question: string; params: Record<string, number> };
}

export interface EventsResponse {
  library_version: number;
  events: EventInfo[];
}

export interface Board {
  code: string;
  name: string;
}

export interface BoardsResponse {
  type: BoardType;
  range: [string, string];
  boards: Board[];
}

export interface FieldInfo {
  name: string;
  label: string;
  unit: string;
  type: string;
  note: string;
  time_series_ok: boolean;
}

export interface FieldsResponse {
  fields: Record<Target, FieldInfo[]>;
}

export interface KlineRow {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  open_raw: number;
  high_raw: number;
  low_raw: number;
  close_raw: number;
  amount: number;
  pct_chg: number | null;
}

export interface KlineResponse {
  code: string;
  name: string | null;
  adjust: string;
  base_date: string | null;
  range: [string, string];
  rows: KlineRow[];
}
