/** 接口的请求、响应类型，对应 litmus/api（ARCHITECTURE §6）。 */

export type Target = "stock" | BoardType;
export type BoardType = "sw_industry" | "sw_industry_l2" | "concept";

// ── 查询条件（spec）：五个部分，对应 litmus/spec/query.py（DESIGN.md §1）──────

export interface Scope {
  target: Target;
  base: "all_a" | "hs300" | "zz500";
  industry: string | null;
  board: { type: "concept"; code: string } | null;
  exclude: string[];
}

export interface Mention {
  mention: string;
  guess?: string | null;
}

export interface Subject {
  kind: "pool" | "codes" | "aggregate";
  mentions: Mention[];
  codes: string[];
}

export interface When {
  as_of: string | null;
  range: { from: string; to: string } | null;
}

/** 一个名字 + 一个公式 */
export interface Metric {
  name: string;
  expr: string;
}

export interface Condition {
  expr: string;
  label: string;
}

export interface Sort {
  /** metrics 里某一项的 name */
  by: string;
  order: "asc" | "desc";
}

export interface TableOutput {
  kind: "table";
  filter: Condition | null;
  sort: Sort | null;
  limit: number;
}

export interface CardOutput {
  kind: "card";
  benchmark: "industry" | "index:000300.SH" | "index:000905.SH";
}

export interface EventRef {
  preset_id: string;
  params: Record<string, number>;
  expr?: string;
  label?: string;
  library_version?: number | null;
}

export interface EventStudyOutput {
  kind: "event_study";
  event: EventRef;
  horizons: number[];
  benchmark: string;
  cost_bps: number;
}

export type Output = TableOutput | CardOutput | EventStudyOutput;
export type Kind = Output["kind"];

export interface Spec {
  version?: 2;
  scope: Scope;
  subject: Subject;
  when: When;
  metrics: Metric[];
  output: Output;
  narrate?: boolean;
  /** 后端检查时由代码填：用了哪些默认值、确认卡上的说明文字。请求里带来的不作数 */
  defaults_used?: string[];
  assumptions?: string[];
}

// ── 结果 ────────────────────────────────────────────────────────

export type Cell = string | number | boolean | null;
export type Row = Record<string, Cell>;

/** 确认卡上的一条说明。group 是左边的小标题（看谁 / 看哪天 / 看哪些数 / 怎么出 / 什么事件 / 怎么算），空串不分组 */
export interface AssumptionItem {
  group: string;
  field: string | null;
  text: string;
  default: boolean;
}

/** 每种结果都带着：上方那句「我把你的问题理解成」和按部分分好的说明 */
interface Explained {
  understood: string;
  assumptions: AssumptionItem[];
}

export interface Column {
  name: string;
  /** 元 / % / 倍 / 个 / 天 / 小数百分比 / 分位 / 布尔 / 空串 */
  unit: string;
  /** 公式就是一个字段时的字段名，决定金额按亿万、涨跌带正负号 */
  field: string | null;
}

export interface TableResult extends Explained {
  kind: "table";
  as_of: string;
  total: number;
  pool_size: number;
  /** 前几列：code、name、（股票还有 industry） */
  head: string[];
  columns: Column[];
  rows: Row[];
  notes: string[];
}

export interface CardRow {
  name: string;
  value: number | boolean | null;
  unit: string;
  /** 屏幕上的值：「3.00」「第 75 名（共 100 只）」「无」 */
  text: string;
  /** 解释行，没什么可解释的为空串 */
  note: string;
}

export interface CardItem {
  code: string;
  name: string | null;
  /** 股票：「农林牧渔 / 养殖业」；板块为空 */
  industry: string | null;
  rows: CardRow[];
}

export interface CardResult extends Explained {
  kind: "card";
  as_of: string;
  pool_size: number;
  items: CardItem[];
  notes: string[];
  /** 要写小结：卡先出，页面再调 /api/run/{run_id}/narrative */
  narrate: boolean;
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

export interface HistoryResult extends Explained {
  kind: "event_study";
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

export type Result = TableResult | CardResult | HistoryResult;

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
  /** 旧版查询结构的记录没有 kind，页面上说明打不开 */
  result: Result | null;
  error: string | null;
  plan_id: string | null;
  data_through: string | null;
  library_version: number | null;
  duration_ms: number | null;
  /** 卡的小结：写过是那段话，没写过是 null */
  narrative: string | null;
}

export interface NarrativeResponse {
  /** 为空时页面上小结这一块不显示 */
  text: string;
  error: string | null;
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

export interface Candidate {
  code: string;
  name: string;
  note: string;
  board_type?: BoardType | null;
}

/** 一句原话对应不止一个，要用户选一个。slot：选中的填进「看谁」（subject）还是「算的范围」（scope） */
export interface Choice {
  slot: "subject" | "scope";
  mention: string;
  message: string;
  candidates: Candidate[];
}

export interface PlanQuestion {
  question: string;
  options: string[];
}

export interface PlanResponse {
  status:
    | "ok"
    | "done"
    | "needs_clarification"
    | "unsupported"
    | "not_an_event"
    | "data_not_ready"
    | "failed";
  plan_id: string | null;
  spec: SpecDraft | null;
  /** ok：确认卡最上面那句「我把你的问题理解成」 */
  summary: string;
  assumptions: AssumptionItem[];
  questions: PlanQuestion[];
  choices: Choice[];
  alternatives: string[];
  message: string | null;
  data: StatusResponse | null;
  /** done：卡不走确认卡，提问时就算完了 */
  run_id: string | null;
  result: Result | null;
}

export interface CheckResponse {
  status: "ok" | "needs_revision" | "data_not_ready";
  issues: Issue[];
  spec: Spec | null;
  summary: string;
  assumptions: AssumptionItem[];
  message: string | null;
  data: StatusResponse | null;
}
