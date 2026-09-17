/** 接口的请求、响应类型，对应 litmus/api（ARCHITECTURE §6）。 */

export type Target = "stock" | BoardType;
export type BoardType = "sw_industry" | "sw_industry_l2" | "concept";

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

/** 后端检查时由代码填：用了哪些默认值、确认卡上的说明文字。请求里带来的不作数 */
export interface SpecMeta {
  assumptions?: string[];
  defaults_used?: string[];
}

export interface StockListSpec extends SpecMeta {
  shape: "stock_list";
  as_of: string;
  filter: Condition | null;
  sort: Sort | null;
  limit: number;
  universe: Universe;
}

export interface BoardListSpec extends SpecMeta {
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

export interface StockHistorySpec extends SpecMeta {
  shape: "stock_history";
  /** mention、guess 是提问时用户的原话和大模型猜的全称 */
  target: { code: string; mention?: string; guess?: string | null };
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

/** 一类数据本地最新到哪天：股票行情、概念板块成分…… */
export interface DataDate {
  key: string;
  label: string;
  date: string;
}

export interface StatusResponse {
  status: DataStatus;
  /** 各类数据最新到哪天，本地没有的那类不列 */
  latest: DataDate[];
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
  /** 申万二级行业的上级一级行业名 */
  parent?: string;
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

// ── 提问、确认卡 ────────────────────────────────────────────────

/** 页面开关，由后端 .env 决定 */
export interface SettingsResponse {
  show_trace: boolean;
}

/** 过程记录：这次提问 / 运行的每一步。steps 里每一项的字段随步骤而异，所以是宽松类型 */
export interface TraceResponse {
  record_id: string;
  query: string;
  steps: Array<Record<string, unknown>>;
  created_at: string;
}

/** 还没检查过的查询条件草稿（大模型给的，可能缺栏目） */
export type SpecDraft = Record<string, unknown>;

export interface AssumptionItem {
  field: string | null;
  text: string;
  default: boolean;
}

export interface Candidate {
  code: string;
  name: string;
  note: string;
  /** 板块候选的口径：选中申万行业填进股票池的行业，概念板块填进板块 */
  board_type?: BoardType | null;
}

export interface PlanQuestion {
  question: string;
  options: string[];
}

export interface PlanResponse {
  status:
    | "ok"
    | "needs_clarification"
    | "unsupported"
    | "not_an_event"
    | "data_not_ready"
    | "failed";
  plan_id: string | null;
  spec: SpecDraft | null;
  assumptions: AssumptionItem[];
  questions: PlanQuestion[];
  stock_candidates: Candidate[];
  board_candidates: Candidate[];
  alternatives: string[];
  message: string | null;
  data: StatusResponse | null;
}

export interface CheckResponse {
  status: "ok" | "needs_revision" | "data_not_ready";
  issues: Issue[];
  spec: Spec | null;
  assumptions: AssumptionItem[];
  message: string | null;
  data: StatusResponse | null;
}
