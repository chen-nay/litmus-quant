# Litmus 技术架构设计

本文档是开发实施的依据。所有设计决策都附有理由，实现时若需偏离请先确认理由是否仍成立。

**约定**：下文用 `模块.函数()` 指代调用，模块名即 §1 依赖图中的名字（`data` / `spec` / `expr` /
`signals` / `research` / `llm` / `store` / `api`）。`ds` 指 DataService 的实例。

---

## 0. 设计原则（不可违背）

| 原则 | 含义 |
|---|---|
| **确定性优先** | 从"确认卡"到"最终数字"这段必须是纯确定性代码。同一份查询规范，跑一万次结果必须完全相同 |
| **LLM 不碰数字** | LLM 只做两件事：把提问翻译成查询规范、审查用户修改的参数。任何数字的计算、排版、渲染都不经过 LLM，LLM 也不修改任何参数 |
| **LLM 的输出必须经确定性核对** | LLM 说的每一样东西（字段、表达式、股票、板块、事件）都要用代码和数据核对一遍，核对不过不执行。见 §5.2 |
| **不猜模糊的地方** | 猜不出合理默认值时先澄清再执行；用默认值时必须在确认卡上显示出来。见 §5.3 |
| **数据层只取数** | DataService 不计算任何指标和统计量。同步时对原始数据做的确定性处理（单位换算、复权、停牌 / 涨跌停 / 除权标志）属于数据准备，不算"计算" |
| **未来函数从语言层杜绝** | 表达式 DSL 中 `Ref(x, n)` 的 n 强制为正，从语法层面无法引用未来数据 |
| **单向依赖** | 模块只能依赖下层，禁止反向依赖；模块之间只通过公开 API 调用。见 §1.2 |
| **能推迟的都推迟** | P0 只做三种回答所必需的部分，推迟清单见 §11。少做不等于做错：推迟的部分不能给 P0 留下错误的口径 |

---

## 1. 模块架构

### 1.1 模块依赖图

```
                      ┌─────────┐
                      │   web   │  React 前端，只通过 HTTP 调用 api
                      └────┬────┘
                           ▼
                      ┌─────────┐
                      │   api   │  编排者：按顺序调用下层模块，本身不做计算
                      └┬──┬──┬──┘
          ┌────────────┘  │  └────────────┐
          ▼               ▼               ▼
     ┌─────────┐    ┌──────────┐    ┌─────────┐
     │   llm   │    │ research │    │  store  │
     └┬───┬────┘    └┬───────┬─┘    └─────────┘
      │   ▼          │       │
      │ ┌─────────┐  │       │
      │ │ signals │  │       │
      │ └───┬─────┘  │       │
      ▼     ▼        ▼       │
     ┌──────────────────┐    │
     │       expr       │    │
     └────────┬─────────┘    │
              ▼              ▼
     ┌──────────────────────────┐    ┌──────────┐
     │           data           │    │   spec   │  ← llm / research / api 依赖它
     └──────────────────────────┘    └──────────┘
```

| 模块 | 职责 | 依赖 |
|---|---|---|
| **data** | 取数（DataService，只读本地）与同步（DataSync，从 Tushare 下载并做能力探测） | 无 |
| **spec** | QuerySpec 的数据结构定义、格式校验、默认值表 | 无 |
| **expr** | 解析、校验、计算表达式；回答"哪只股票（或哪个板块）哪天满足条件" | data |
| **signals** | 预置事件库（加载时用 expr 校验每条表达式） | expr |
| **research** | 三种回答的计算：股票表、板块表、个股回看 | expr、data、spec |
| **llm** | 只有一个函数：`llm.plan()`（提问 → QuerySpec） | expr、signals、spec |
| **store** | 保存 plan 记录与 run 记录（JSON 文件） | 无 |
| **api** | HTTP 接口；编排调用顺序；LLM 输出的确定性兜底核对 | 以上全部 |

说明：
- **llm 与 research 平级**，互不调用，由 api 串起来
- **spec 只校验结构**（栏目齐不齐、类型对不对），表达式内容由 `expr.validate()` 校验，因此 spec 不依赖 expr
- **"执行"** 专指 `research.run()` 这段纯 Python 计算，全程不经过 LLM

### 1.2 依赖规则

1. **只能向下依赖**。禁止反向依赖，也禁止同层互相依赖：llm、research、store 三者互不依赖；data、spec 互不依赖
2. **允许跨层向下**。research 可以直接调用 data 取价格，不必经过 expr 转手
3. **只通过公开 API 调用**。每个模块在 `__init__.py` 用 `__all__` 声明对外 API；其他模块不能 import 内部子模块（如 `litmus.data.loaders.tushare`）
4. **依赖注入**。`ds` 由 api 在启动时创建一次，作为参数传给 research / expr，下层不自行创建
5. **只在确有多个实现的地方抽象接口**：
   - `Store`：JSON 文件实现 / 以后可换 SQLite
   - `LLMClient`：Anthropic 协议 / 以后 OpenAI 兼容协议

   DataService 只有一个实现，expr、research 是纯函数，都直接暴露类或函数
6. **API 语义靠契约测试保证**。DataService、Store 各有一套契约测试，见 §2.4、§7
7. **用 import-linter 自动检查**，违反依赖规则时 `lint-imports` 直接报错：

```toml
[tool.importlinter]
root_package = "litmus"
include_external_packages = true

# 只有 llm 模块能接触具体厂商的 SDK：以后加 OpenAI 兼容实现时，
# 改动被限制在 llm/client.py 内，其余模块不可能悄悄依赖某一家
[[tool.importlinter.contracts]]
name = "LLM 厂商 SDK 只能出现在 llm 模块"
type = "forbidden"
source_modules = [
    "litmus.api", "litmus.research", "litmus.data",
    "litmus.expr", "litmus.spec", "litmus.signals", "litmus.store",
]
forbidden_modules = ["anthropic", "openai"]

[[tool.importlinter.contracts]]
name = "模块单向依赖"
type = "layers"
layers = [
    "litmus.cli",
    "litmus.api",
    "litmus.llm | litmus.research | litmus.store",   # 同层且互相独立
    "litmus.signals",
    "litmus.expr",
    "litmus.data | litmus.spec",                     # 同层且互相独立
]
```

### 1.3 各模块公开 API（草案，实现对应模块时定稿）

| 模块 | 对外暴露 |
|---|---|
| data | `DataService`：`ds.get_fields()` / `ds.get_trading_calendar()` / `ds.latest_trading_day()` / `ds.get_universe()` / `ds.get_universe_mask()` / `ds.list_boards()` / `ds.board_members()` / `ds.resolve_stock()` / `ds.resolve_board()` / `ds.data_status()`；`DataSync`：`start()` / `status()`；字段目录 `data.FIELDS` |
| spec | `spec.QuerySpec`（三种形状）、`spec.DEFAULTS`（默认值表）、`spec.render_assumptions()`（由 spec 生成确认卡说明） |
| expr | `expr.parse()` / `expr.validate()` / `expr.collect_fields()` / `expr.collect_lookback()` / `expr.evaluate()`；`expr.field_catalog()` / `expr.operator_catalog()`（供 llm 组装提示词） |
| signals | `signals.load_events()`（事件库） |
| research | `research.run(spec, ds)` |
| llm | `llm.plan()` |
| store | 见 §7 |
| api | HTTP 接口，见 §6 |

### 1.4 一次查询的完整调用链

```
① POST /api/plan  {query: "昨天哪个股票成交量明显放大？", previous_plan_id?: "..."}

api ──→ DataSync.status()：最小可用区间未覆盖 → 直接返回 data_not_ready（附同步进度）
api ──→ 组装上下文：
        expr.field_catalog()     字段清单（按 manifest 的能力探测结果过滤）
        expr.operator_catalog()  算子清单
        signals.load_events()    事件库（15 条）
        ds.list_boards("sw_industry")  申万 31 个一级行业名（概念板块几百个，不进提示词，走 resolve）
        spec.DEFAULTS            默认值表
        ds.latest_trading_day()  最近已收盘交易日
api ──→ llm.plan(query, 上下文)
        llm 内部：
        ├─ 渲染 prompt "planner.system" → LLMClient → JSON
        ├─ spec.QuerySpec 校验格式
        ├─ expr.parse() + expr.validate() 校验表达式               ← 防线②
        └─ 不通过 → 渲染 "planner.repair" 带错误重试一次 → 仍失败返回 failed
api ──→ 确定性兜底核对（见 §5.2 防线③）：
        必填项、股票（ds.resolve_stock）、板块（ds.resolve_board）、事件与参数范围（signals）
        → 任一不过转成 needs_clarification，绝不执行
api ──→ store.save_plan(原话, spec) → plan_id
api ──→ 返回 {status, plan_id, spec, assumptions, questions?, alternatives?}
        status = ok | needs_clarification | unsupported | not_an_event | failed


② POST /api/run  {spec, plan_id}

api ──→ DataSync.status()：最小可用区间未覆盖 → data_not_ready
api ──→ 确定性检查：spec 校验 + expr.validate() + 事件参数范围 + 数值范围   不通过 → needs_revision
        （用户改过参数走的是同一套检查，不再调 LLM；assumptions 由 spec 重新生成，见 §5.4）
api ──→ research.run(spec, ds)
        research 内部按 spec.shape 分派：
        ├─ stock_list  → expr.evaluate(筛选表达式) → 排序 → 取前 N
        ├─ board_list  → 同上，标的换成板块
        └─ stock_history → 找出这只股票的全部触发日 → 逐笔算之后 N 天涨跌
                           → 对照（同期市场平均、这只股票平时的平均）
api ──→ store.save_run(spec, result) → 返回 {run_id, result}
```

**P0 没有结果缓存**：每次请求都重新计算（全 A 一次事件计算 3~8 秒，可接受）。缓存推迟到 P1（§11）。

### 1.5 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 前端 | React + Vite + ECharts | ECharts 对 K 线和 A 股红涨绿跌配色支持最好 |
| 后端 | FastAPI + Pydantic v2 | Pydantic 同时用于 API 校验和 LLM 输出 schema，一套定义两处复用 |
| 计算 | Polars | 面板数据操作比 pandas 快数倍、内存占用低 |
| 行情存储 | Parquet + DuckDB | 列式存储 + SQL 直查，用户可自行探索数据 |
| 应用数据 | JSON 文件（一条记录一个文件） | 本地单用户、数据量小、可直接打开审计；接口保留以后换 SQLite 的能力，见 §7 |
| LLM | anthropic SDK（base_url 可配） | 见 §5 |
| 任务 | FastAPI BackgroundTasks | P0 不引入 Celery |
| 工程 | uv + pytest + ruff + import-linter | import-linter 把 §1.2 的依赖规则变成自动检查 |

**不引入**：LangChain、LangGraph、Claude Agent SDK、Redis、PostgreSQL、SQLite。
P0 只有两处单轮 LLM 调用，没有 agent loop，引入框架是负债。

---

## 2. data 模块

### 2.1 DataService：只读本地，从不联网

```python
# litmus/data/service.py

class DataService:

    def get_fields(
        self, codes: list[str], start: date, end: date, fields: list[str],
        target: str = "stock",          # stock | sw_industry | concept
    ) -> pl.DataFrame:
        """统一取数入口。返回长表 (date, code, <fields...>)，按 (date, code) 排序。
        字段来自哪张表由 DataService 按 FIELDS 内部路由，调用方不关心。
        · 价格类字段一律后复权
        · 财务类字段按【披露日】对齐（PIT），不是报告期；前向填充至下次披露
        · 事件类、状态类字段为布尔值
        · 请求 FIELDS 之外、或当前账号能力不支持的字段，直接报错"""

    def get_universe(
        self, as_of: date, base: str = "all_a", industry: str | None = None,
        board: Board | None = None, exclude: list[str] | None = None,
    ) -> list[str]:
        """某一日的股票池（按当日状态，PIT）。
        base: all_a（沪深 A 股，不含北交所）/ hs300 / zz500
        industry: 申万行业名，按当日归属取成分
        board: {"type": "concept", "code": "..."}，P0 只有当前成分快照"""

    def get_universe_mask(
        self, start: date, end: date, base: str = "all_a", industry: str | None = None,
        board: Board | None = None, exclude: list[str] | None = None,
    ) -> pl.DataFrame:
        """按日的股票池：返回 (date, code, in_universe) 真假表。
        历史计算必须用它，不能用某一天的名单去套整段历史（否则产生幸存者偏差）"""

    def list_boards(self, board_type: str) -> list[BoardInfo]:
        """板块清单。board_type: sw_industry（申万行业）/ concept（概念板块，需能力可用）"""

    def board_members(self, board_code: str, as_of: date | None = None) -> list[str]:
        """板块成分股。P0 概念板块只有当前快照，as_of 只接受最新日期"""

    def get_trading_calendar(self, start: date, end: date) -> list[date]: ...

    def latest_trading_day(self) -> date:
        """本地最后一个**完整**交易日：当天所有必需接口的数据都已落盘（§2.5）。
        六个按日拉取的接口入库时间不同（涨跌停价 8:40、复权因子盘前、行情与每日指标 15~17 点），
        只落了一半的当天不算完整日、不对外可见，否则筛选结果会静默变空。
        和页面上"数据截至 X"显示的是同一天"""

    def resolve_stock(self, text: str) -> list[StockMatch]:
        """股票提及 → 候选股票列表。规则见 §2.3"""

    def resolve_board(self, text: str, board_type: str | None = None) -> list[BoardMatch]:
        """板块提及 → 候选板块列表，用和股票同一套匹配规则。见 §2.3"""

    def data_status(self) -> DataStatus:
        """本地数据覆盖范围、最近同步时间、各可选数据的可用状态（来自 manifest）"""
```

下载由 data 模块内的 `DataSync` 负责，见 §2.5。

### 2.2 字段目录 FIELDS

`data.FIELDS` 是**所有字段的唯一来源**：字段名、中文说明、单位、类型、Tushare 来源、换算方式、所属标的类型、以及需要的能力（积分）。

- `expr.validate()` 的字段白名单从它来
- `llm.plan()` 提示词里的字段清单也从它来（经 `expr.field_catalog()`，并按能力探测结果过滤）

两者永远是同一份，不会出现"LLM 以为有、校验器说没有"的情况。

**单位约定**：金额一律为元，股数一律为股，比率保持百分数（换手率 5 表示 5%），估值为倍数。

#### 股票字段

| 字段 | 含义 | 单位 | 来源（接口.字段） | 处理 |
|---|---|---|---|---|
| `$open` `$high` `$low` `$close` | 开高低收 | 元，后复权 | `daily` × `adj_factor` | 原始价 × 复权因子 |
| `$volume` | 成交量 | 股，复权后 | `daily.vol` | 手 × 100 得股数，再 ÷ 复权因子 |
| `$amount` | 成交额 | 元 | `daily.amount` | 千元 × 1000 |
| `$vwap` | 成交均价 | 元，后复权 | 由成交额、成交量推导 | 原始成交额 ÷ 原始成交股数 × 复权因子 |
| `$pct_chg` | 当日涨跌幅 | % | `daily.pct_chg` | 基于除权后的昨收计算 |
| `$close_raw` | 收盘价（不复权） | 元 | `daily.close` | 真实股价。**只能直接比较或参与横截面排名，不能进时序算子**（§3.4） |
| `$turnover` | 换手率 | % | `daily_basic.turnover_rate` | — |
| `$pe_ttm` | 市盈率 TTM | 倍 | `daily_basic.pe_ttm` | 亏损为空值 |
| `$pb` | 市净率 | 倍 | `daily_basic.pb` | — |
| `$ps_ttm` | 市销率 TTM | 倍 | `daily_basic.ps_ttm` | — |
| `$dv_ttm` | 股息率 TTM | % | `daily_basic.dv_ttm` | — |
| `$market_cap` | 总市值 | 元 | `daily_basic.total_mv` | 万元 × 10000 |
| `$circ_mv` | 流通市值 | 元 | `daily_basic.circ_mv` | 万元 × 10000 |
| `$roe` | 净资产收益率（年化） | % | `fina_indicator_vip.roe_yearly` | 按披露日对齐；口径第 1a 步实测确认 |
| `$revenue_yoy` | 营业收入同比 | % | `fina_indicator_vip.or_yoy` | 按披露日对齐 |
| `$profit_yoy` | 归母净利润同比 | % | `fina_indicator_vip.netprofit_yoy` | 按披露日对齐 |
| `$is_report_date` | 财报实际披露日 | 布尔 | `disclosure_date.actual_date` | — |
| `$is_forecast_date` | 业绩预告公告日 | 布尔 | `forecast_vip.ann_date` | — |
| `$is_ex_div` | 除权除息日 | 布尔 | 由 `adj_factor` 推导 | 复权因子较前一交易日变化 |
| `$is_st` | ST / *ST | 布尔 | `stock_st` | 当日在官方 ST 名单中 |
| `$is_limit_up` | 收盘涨停 | 布尔 | `daily.close` 与 `stk_limit.up_limit` | 收盘价等于涨停价 |
| `$is_limit_down` | 收盘跌停 | 布尔 | `daily.close` 与 `stk_limit.down_limit` | 收盘价等于跌停价 |
| `$is_new` | 次新股 | 布尔 | `stock_basic.list_date` | 上市不足 N 个交易日 |

#### 板块字段

| 字段 | 含义 | 单位 | 申万行业（`sw_daily`） | 概念板块（`tdx_daily`，需 6000 积分） |
|---|---|---|---|---|
| `$close` `$open` `$high` `$low` | 板块点位 | 点 | ✅ | ✅ |
| `$pct_chg` | 当日涨跌幅 | % | ✅ | ✅ |
| `$amount` | 成交额 | 元 | ✅ 万元 × 10000 | ✅ 万元 × 10000 |
| `$turnover` | 换手率 | % | ❌ | ✅ |
| `$pe` `$pb` | 估值 | 倍 | ✅ | ⚠️ 原始返回是**字符串**，loader 转数值并处理空串 |
| `$float_mv` | 流通市值 | 元 | ✅ 万元 × 10000 | ⚠️ **亿** × 1e8 |
| `$total_mv` | 总市值 | 元 | ✅ 万元 × 10000 | ❌ 只有 `ab_total_mv`（含 B 股，单位亿），口径不同，概念板块不提供此字段 |
| `$up_num` `$limit_up_num` | 上涨家数、涨停家数 | 个 | ❌ | ✅ |

- 板块不做复权（它本身是指数）；某个口径没有的字段不会出现在给 LLM 的清单里
- **两个口径的单位和类型不一致**：申万是"万元"，通达信是"亿"，且 `pe` / `pb` 返回字符串。
  loader 必须分口径处理，不能共用一套换算

#### 其他约定

- 原 `$ps` 更名为 `$ps_ttm`，与 `$pe_ttm` 口径一致
- **涨跌幅有两种写法**：`$pct_chg` 是百分数（5 表示 5%），`Pct($close,1)` 是小数（0.05 表示 5%）。
  算子清单里会同时标注、提示词里也给示例，避免 LLM 写出 `Pct($close,1) > 5` 这种差 100 倍的条件
- **"成交量"默认理解为成交额 `$amount`**（见 §5.3 默认值表），事件库里的"放量""缩量"也一律用 `$amount`。
  `$volume`（成交股数）保留，用户明确要按股数时才用
- 价格与涨跌停价的比较，在分位（0.01 元）上取整后进行，避免浮点误差
- **空值**：亏损股的 PE、新股的同比等为空值。比较运算遇到空值结果为"不满足"，`Rank` 忽略空值
- **停牌日不补齐**：面板是稀疏的，每只股票只保留它有成交的交易日。`$is_suspended` 字段 P0 不提供——
  停牌的两个实际用途（从股票池剔除、买卖顺延）都由代码处理，不需要表达式字段。窗口语义见 §3.3
- research 另需两个**不对表达式开放**的内部字段：原始（不复权）收盘价、开盘是否涨停（开盘价等于涨停价，用于判断买不进）

### 2.3 股票名解析 resolve_stock

**LLM 不直接给出股票代码。** `llm.plan()` 只提取用户原话里的股票提及（mention），可附带一个猜测的全称（guess）；
股票代码一律由 `ds.resolve_stock()` 在股票列表中确定性查找。

匹配规则（所有命中项都返回，按优先级排序）：

| 优先级 | 规则 | 例子 |
|---|---|---|
| 1 | 代码精确匹配 | `600519` / `600519.SH` |
| 2 | 名称完全一致 | `贵州茅台` |
| 3 | 名称包含 | `茅台` → 贵州茅台（同样含"茅台"的其他股票一并返回） |
| 4 | 拼音首字母 | `gzmt` → 贵州茅台 |
| 5 | 曾用名 | 按历史名称匹配（含已退市股票） |
| 6 | 字按顺序出现 | `招行` → 招商银行，`中石油` → 中国石油 |

- 代码覆盖不了的是**外号**（如"宁王"→宁德时代）：由 LLM 给出 guess，再用同样规则核对 guess
- 结果：0 个 → 提示"未找到"；1 个 → 直接填入；多个 → 确认卡列出候选让用户选
- 全部股票约 5500 只，内存中逐只比对即可
- **板块同样走"提及 + 解析"**：申万只有 31 个一级行业，整份放进提示词；概念板块有几百个，不进提示词，
  LLM 只填用户原话，由 `ds.resolve_board()` 用同一套规则（精确、包含、拼音、字序）查找，多个候选让用户选

### 2.4 DataService 契约测试

上层只依赖 DataService 的**语义**，语义写成一套契约测试（`tests/contract/test_dataservice.py`），直接在真实数据上运行：

| 契约 | 检查内容 |
|---|---|
| 返回格式 | 长表，列为 (date, code, <字段>)，按 (date, code) 排序，类型符合 FIELDS |
| 后复权 | 选一个真实送转日，检查后复权价格前后连续；同一查询重复调用结果逐位相同 |
| PIT | 选一个真实的财报更正案例，检查修正值只在修正披露日之后出现 |
| 交易日历 | 只含交易日；"N 日"位移按交易日计算 |
| 退市股 | 已退市股票在其上市期间可查到 |
| 按日股票池 | 某只股票在成为 ST 之前的日子在池内、之后不在；退市股在退市前的日子在池内 |
| 未知字段 | 请求 FIELDS 之外、或能力不可用的字段直接报错，不返回空列 |

DataService 只有一个实现（读本地 Parquet），不做抽象接口，也不做测试用的假数据实现。
算子、收益等计算逻辑的正确性由**小表格测试**保证，见 §10。

**契约测试跑在合成数据集上**：`tests/fixtures/` 下放一份按真实 Parquet 布局生成的小数据集
（约 20 只股票 × 2 年，含送转、停牌、ST、退市、一字涨停各一例），由一个生成脚本产出。
这样契约测试和 CI 都能离线跑，不需要 token。真实数据上的验证降级为"有 token 时才跑"的可选测试。
注意它和被砍掉的 FakeDataService 不是一回事：实现仍然只有一个，只是喂给它的数据是合成的。

### 2.5 数据来源、同步与存储

**数据来源：P0 只接入 Tushare**，token 为必填项，积分需 **5000 及以上**。
- 所有数据来自同一个源，不做多数据源融合，避免单位、复权基准、代码格式在接缝处不一致
- 免费数据源（BaoStock / AKShare）在 P0 之后再考虑；届时一次安装只使用一种数据源，换源即重建本地数据

**仓库不附带任何数据**（Tushare 数据不可再分发）。用户在页面上点击「同步历史数据」下载到本地。

**数据范围**：2016-01-01 至今；沪深 A 股（主板、创业板、科创板，含已退市股票），**不含北交所**。
- 起点选 2016 年：ST 名单接口 `stock_st` 从 2016 年起才有数据；约 10 年数据够用，以后可再向前补拉历史月份
- 不含北交所：股票代码整体变更过（`bse_mapping`，如 838163.BJ → 920163.BJ），历史会在换代码处断开；开市晚、数量少、流动性差

#### 数据清单

积分门槛与单次上限依据本地 `api_define/`（不提交到 git）；各接口 2016 年起的数据完整性在第 1a 步逐一核实。

**基础数据（5000 积分，必需）**

| # | 数据 | 用途 | 接口 | 拉取方式 |
|---|---|---|---|---|
| 1 | 日线行情 | 开高低收、成交量额；缺行即停牌 | `daily` | 按交易日 |
| 2 | 复权因子 | 后复权；变化即除权除息日 | `adj_factor` | 按交易日 |
| 3 | 每日指标 | 换手率、估值、市值 | `daily_basic` | 按交易日 |
| 4 | 涨跌停价 | 涨跌停状态、开盘买不进 | `stk_limit` | 按交易日（含 B 股与基金，需分页） |
| 5 | ST 名单 | `$is_st` | `stock_st` | 按日期区间（单次 1000 行，实测每天约 200 只 ST，约 4 天一次调用） |
| 6 | 财务指标（含披露日） | `$roe $revenue_yoy $profit_yoy` | `fina_indicator_vip` | 按报告期 |
| 7 | 财报披露日 | `$is_report_date` | `disclosure_date` | 按报告期（需分页） |
| 8 | 业绩预告 | `$is_forecast_date` | `forecast_vip` | 按报告期 |
| 9 | 限售解禁 | `$is_unlock_date` | `share_float` | **P0 不拉**，数据量见 §11 |
| 10 | 股票列表 | 股票池、上市日期、拼音首字母 | `stock_basic` | L / D / P 三种状态各取一次 |
| 11 | 曾用名 | 按旧名查股票 | `namechange` | 全量 |
| 12 | 交易日历 | 所有"N 日"的换算 | `trade_cal` | 全量 |
| 13 | 指数日线 | 可选的市场对照口径（沪深300），默认对照不用它（§4.3） | `index_daily` | 按指数 |
| 14 | 指数历史成分 | hs300 / zz500 股票池，防幸存者偏差 | `index_weight` | 按指数、按月 |
| 15 | 申万行业分类 | 行业清单 | `index_classify` | 全量（SW2021） |
| 16 | 申万行业历史归属 | 行业筛选与分组 | `index_member_all` | 按行业，传 `is_new='N'` |
| 17 | 申万行业日线 | 行业榜 | `sw_daily` | 按行业代码（单次 4000 行，31 个一级行业各一次） |

**可选数据（能力探测，缺权限自动关闭）**

| 数据 | 接口 | 积分 | 用途 |
|---|---|---|---|
| 概念板块清单 | `tdx_index` | 6000 | 板块名清单。实测共 613 个，含概念 / 行业 / 风格 / 地区四类，要按 `idx_type` 过滤出概念板块 |
| 概念板块日线 | `tdx_daily` | 6000 | 概念板块榜（按板块代码拉，单次 3000 行，约 600 次）。**实测历史只到 2025-03**，见 §2.7 |
| 概念板块成分 | `tdx_member` | 6000 | "某某概念股有哪些"（P0 只取当前快照） |

**不拉取的接口**：`suspend_d`（停牌由 `daily` 缺行推导）、`dividend`（除权除息日由 `adj_factor` 变化推导）、
`stk_factor_pro`（5000 积分时每分钟只能调 30 次，反而更慢）。龙虎榜、游资、人气榜、机构目标价、
涨停原因（`top_list` / `hm_*` / `ths_hot` / `dc_hot` / `report_rc` / `limit_list_ths`）P0 不接，见 §11。

#### 能力探测

同步开始时，对每个**可选数据集**试调一次最小请求：

- 调通 → 在 manifest 记 `available: true`，正常同步，相关字段进入给 LLM 的清单
- 报权限 / 积分不足 → 记 `available: false` 和原因，跳过同步，相关字段**不进入** LLM 清单，用户问到时走"数据不支持"并说明所需积分
- 其他错误（网络、格式）→ 直接报错，不静默降级

用户以后积分够了，再点一次同步，探测通过即自动开启。这套机制以后接免费数据源时同样复用。

#### loader 规则（`loaders/tushare.py`）

1. **单位与字段名归一**：Tushare 的"手""千元""万元"统一换算为"股""元"，字段改为 FIELDS 中的命名（映射见 §2.2）
2. **自动分页，禁止静默截断**：返回条数达到接口单次上限时继续分页。例如 `stk_limit` 单次最多 5800 条，但它包含 A/B 股和基金，一天的记录可能超过上限
3. **返回校验**：按接口校验返回的字段名和类型，日期统一归一化为 `YYYYMMDD` 字符串；与预期不符直接报错（代理版接口曾悄悄把日期从字符串改为浮点数）
4. **接入地址可配**：`TUSHARE_HTTP_URL` 为空时使用官方地址；通过第三方代理接入时在 `.env` 中填写
5. **按接口限速**：各接口每分钟上限不同（如 `stock_basic` 50 次、`daily` 500 次），限速参数按接口配置
6. **接口定义**以本地 `api_define/` 为准，写代码时只抄不猜

#### 同步机制（DataSync）

| 场景 | 行为 |
|---|---|
| 首次同步 | 用户点击「同步历史数据」，后台**从今天倒序**下载到 2016 年，页面显示进度与已覆盖区间 |
| 拉取方式 | 股票类按交易日拉（一次返回当天全部股票）；`sw_daily` 按行业、`tdx_daily` 按板块、`stock_st` 按日期区间，都比按交易日省得多 |
| 完整日判定 | 一个交易日的必需接口**全部**拉到才纳入当月面板，缺任何一个就中止这个月、不落盘。磁盘上因此不存在半天的数据，不必按天记账 |
| 原子写入 | Parquet 先写临时文件再改名，避免读到写了一半的文件 |
| 断点续传 | 以月为单位落盘：整月拉完才写文件并记入 manifest；中途关闭程序，没拉完的月份下次整月重来（一个月约 80 次调用，比维护半截文件划算） |
| 增量同步 | 之后每次只补"本地最新日期 → 今天"的缺口 |
| 限流与失败 | 按接口限速；失败自动重试，连续失败则暂停并在页面显示原因 |
| 并发 | 同一时间只运行一个同步任务 |

**同步从今天倒着往 2016 年拉，够用就解锁**：覆盖到最近两年（约 15 分钟）即可开始查询，更早的历史后台继续补。
`/api/plan`、`/api/run` 先检查 `DataSync.status()`，最小可用区间还没到就返回 `data_not_ready`（附进度），
前端输入框不可用；到了之后照常查询，页面标注「历史已补到 YYYY-MM，仍在继续」。

这样做的理由：实测全量要一个多小时，让用户装完干等着才能问第一个问题没有必要；而高频问题问的都是"最近"，
两年数据足够回答。**倒序同步不需要改动存储层**：manifest 按月记账，`missing_months()` 接受任意月份顺序，
倒着拉只是调用方换个遍历顺序。

问题的回看窗口超出已覆盖区间时，走确认卡的可用区间提示——这与概念板块只有 2025-03 起的数据是同一套机制，
不静默给出样本不足的结论。

完成后，本地数据即使落后几天也照常查询，页面标注"数据截至 YYYY-MM-DD"并提示同步更新。

**首次同步耗时（第 1a 步实测）**：按交易日拉的 4 个接口（行情、复权因子、每日指标、涨跌停价）
约 1.04 万次，ST 名单约 650 次，申万行业 31 次，概念板块约 600 次，财务、事件、指数与基础数据约 600 次，
合计约 1.2 万次调用。

**2026-09-13 实测（闲鱼代理）**：单次调用 5~10 秒，瓶颈在代理而不在 Tushare 的 500 次/分钟配额——
串行只跑到 8 次/分钟，配额只用掉 1.6%。并发是唯一的杠杆，但代理限制同时在飞的连接数：8 路连续 156 秒
零失败，10 路仍然零失败，12 路开始返回 `code=429 请勿使用过多线程，连接超限`。**取 8 路并发。**

按真实同步实测（2026 年 8 月，21 个交易日 = 84 次调用，跑两次）：耗时 34 秒与 42 秒，即 **120~148 次/分钟**。
全量十年约 **70~90 分钟**，最近两年约 **15 分钟**；单月面板 9.6 MB，十年磁盘约 1.2 GB。

> **只压测单个接口会低估真实吞吐。** 压测时反复调的都是 `daily`（11 列），测出 50 次/分钟；
> 而真实同步是 4 个接口混着调，`adj_factor` 只有 3 列、`stk_limit` 4 列，平均载荷小得多，
> 实测 120~148 次/分钟。换数据源时按真实的调用组合测，别拿最重的那个接口外推。

**并发不是全局一个数**（2026-09-13 实测）。财务类接口的承受力差别很大：

| 接口 | 单次耗时 | 串行拉完 | 8 路并发 |
|---|---:|---:|---|
| `fina_indicator_vip` | 12.3s | 42 个报告期约 8.6 分钟 | 正常 |
| `forecast_vip` | 2.6s | 42 个报告期约 1.8 分钟 | 正常 |
| `disclosure_date` | 1.5s | 42 个报告期约 63 秒 | **被限流** |
| `share_float` | 5.0s | 拉不完，见 §11 | **被限流** |

串行时四个接口都不会被限流，所以限流是被并发触发的，不是请求次数。于是按接口区分：
被限流的两个串行拉（`SERIAL_APIS`），代价合计约两分钟；真正需要并发的
`fina_indicator_vip` 恰好扛得住 8 路。**没有猜任何限额数字**——扛不住就串行，扛得住就并发。

配套的退避也必须分开：限流是按分钟计的配额，1/2/4 秒的退避四次全落在同一个窗口里，
必然全撞（这正是第一次财务同步整体失败的原因）。现在限流按 15/30/60/60 秒等，
网络抖动仍按 1/2/4 秒快速重试，两者各记各的次数。

#### 存储布局

```
data/                                  # 默认在仓库根目录（已 gitignore），可用 LITMUS_DATA_DIR 指定
├── market/                            # DataSync 写，DataService 读
│   ├── raw/                           # Tushare 原始返回；清洗逻辑修改后可据此重建，无需重新下载
│   ├── daily/2026-09.parquet          # 股票日频面板，按月一个文件：一行 = 一只股票一天
│   │                                  #（合并行情、复权因子、每日指标、涨跌停价、ST 状态）
│   ├── board/                         # 板块日频面板：申万行业、概念板块
│   ├── fina_indicator.parquet         # 含 ann_date（披露日）
│   ├── events/                        # 财报披露日、业绩预告（限售解禁见 §11）
│   ├── index/                         # 指数日线、指数历史成分
│   ├── meta/                          # 股票列表（含退市）、曾用名、交易日历、申万行业归属、板块清单与成分
│   └── manifest.json                  # 数据来源、覆盖范围、行数、首末日期、同步时间、能力探测结果
└── store/                             # 归 store 模块所有，见 §7
```

`manifest.json` 是本地数据的"户口本"：数据来源、每个数据集的覆盖范围、**按月**记录的行数、交易日数、首末日期、
是否整月已走完（`complete`）、同步时间，以及能力探测结果。五个用途：断点续传、增量同步、
页面显示数据截止日、能力开关、出问题时可追溯。

- 日频数据按月分文件：已走完的月份不再改动，还在长的当月每次同步整月重写，不做追加合并
- **月文件要么不存在，要么是整月**：没拉完就不落盘，下次重来。读的人不必判断文件是否残缺
- **原子写入**：先写同目录的临时文件，再 `os.replace` 改名，读的人不会读到写了一半的 Parquet

### 2.6 A股数据处理清单（P0 必做）

| 项 | 处理方式 |
|---|---|
| 复权 | **强制后复权**：后复权价 = 原始价 × 复权因子（与 Tushare 定义一致）。前复权的历史价格会随每次新除权而变化，导致结果不可复现 |
| 成交量 | `$volume` 随复权调整：复权成交量 = 原始成交股数 ÷ 复权因子，避免送转后成交股数跳变误触发"放量"。`$amount`、`$turnover` 保持原值 |
| 成交均价 | `$vwap` = 原始成交额 ÷ 原始成交股数 × 复权因子，与 `$close` 同为后复权口径 |
| 财务 PIT | 按 `ann_date`（披露日）对齐，只用 `ann_date <= 当前日` 的记录，禁止用报告期。**同一天披露多个报告期时（4 月底年报与一季报常同日），按 (披露日, 报告期) 双键排序，取报告期最大的那条**，再前向填充。更正公告的处理在第 1a 步实测后确定 |
| 同比口径 | `$revenue_yoy` / `$profit_yoy` 是最新一期的**累计**同比：年报是全年、一季报是单季，披露日会跳变。字段说明里必须写明，否则用户会以为数据错了 |
| 绝对价格 | 后复权价不是真实股价，"股价低于 10 元"必须用 `$close_raw`；股票表展示的收盘价也用原始价 |
| 空值 | 亏损股的 PE、新股的同比等为空值。比较运算遇空值判为"不满足"，`Rank` 忽略空值 |
| 退市股 | `stock_basic` 按 L / D / P 三种状态分别拉取后合并，保留所有曾上市股票及 `delist_date` |
| 涨跌停 | 以 `stk_limit` 的实际涨跌停价为准，不按比例自行推算（历次规则调整、ST、新股等情况都已包含） |
| 停牌 | 上市期内的交易日没有 `daily` 记录即为停牌。**面板不补齐**：每只股票只保留有成交的交易日，时序算子窗口按该股票的有效交易日计算（§3.3）。停牌只影响两处：从股票池剔除、买卖顺延 |
| ST | 按交易日取 `stock_st` 官方名单 |
| 次新股 | 上市不足 N 个交易日标记 `is_new`，默认排除 |
| 北交所 | **数据照常落盘，在股票池这一层排除**。Tushare 按日返回的数据里本来就含北交所（实测某日 `daily_basic` 5550 行中有 `.BJ` 代码），落盘时丢掉的话，将来想放开就得重新同步十年；存下来则只是改一条股票池规则。理由见 §2.5 |
| 交易日历 | 所有"N日"一律指**交易日**，不是自然日。这是确认卡必须澄清的项 |
| 曾用名 | `namechange` 会返回**整行重复**的记录（实测全量 34749 行里 14263 行重复；单只股票只有 12 行、根本不翻页也照样重复，是数据源本身的问题），落盘前整行去重。另外 `end_date` 为空**不**等于现用名——去重后仍有股票存在多行 `end_date` 为空，最多一只 8 行；判断现用名要取 `start_date` 最大的那一行 |

### 2.7 行业与概念板块

| | 申万行业 | 概念板块 |
|---|---|---|
| 例子 | 电子、医药生物、食品饮料（31 个一级行业） | 光模块、AI、华为概念 |
| 归属 | 一只股票同时只属于一个行业 | 一只股票可属于多个概念，经常调整 |
| 口径来源 | 申万 2021 版分类 | 通达信（`tdx_*`） |
| 积分 | 5000（基础功能） | 6000（可选增强） |
| 历史归属 | ✅ `index_member_all` 有纳入 / 剔除日期 | ❌ P0 只有当前成分快照 |
| 板块行情 | ✅ `sw_daily` | ✅ `tdx_daily` |

**概念板块口径定为通达信**（`tdx_*`）。三家的历史长度实测差别很大：

| 口径 | 历史起点 | 关键字段 |
|---|---|---|
| **通达信 `tdx_daily`（采用）** | 2025-03 | 涨停家数、上涨家数、量比、3/5/20 日涨幅 |
| 东财 `dc_daily` | 2020-01 | 无涨停家数 |
| 同花顺 `ths_daily` | 2016-01 | 无涨停家数、上涨家数 |

选通达信的理由：板块表回答的是"最近一周哪个板块热"这类**当下**的问题，一年半够用；
而"涨停家数"是判断板块热度最直接的指标，只有通达信有。代价是"2023 年哪个板块最热"答不了——
**板块数据的可用区间必须显示在确认卡上**（申万行业 2012-08 起，概念板块 2025-03 起）。
以后确实需要更长的概念板块历史，再评估换同花顺（要放弃涨停家数，且板块清单与成分接口需另行实测）。

- 两种口径都能做"板块表"（筛选、排序、取前 N），因为板块日线本身就是现成的
- 申万 2021 版对 2021 年以前的归属口径在第 1a 步实测
- 概念板块的历史成分（"2023 年的光模块概念包含哪些股票"）推迟到 P1
- 用概念板块**筛选股票**时（"光模块里最近放量的股票"），P0 用的是当前成分。当时在、现在不在的股票会被漏掉，
  这一点必须写进确认卡的说明
- UI 上必须显示当前用的是哪种口径，不能让用户以为问到了概念板块

---

## 3. expr 模块（表达式引擎，确定性核心）

expr 解析和计算的是**表达式字符串**（如 `Cross($close, Mean($close,250))`），像计算器解析公式一样，
纯代码，没有 LLM 参与。把用户的中文翻译成表达式字符串是 `llm.plan()` 的事。

### 3.1 为什么用表达式 DSL 而不是生成 Python 代码

| | 生成 Python | 表达式 DSL |
|---|---|---|
| 需要沙箱 | ✅ 必须 | ❌ 不执行代码，只求值 AST |
| 可静态检查未来函数 | ❌ 难 | ✅ 语法层面杜绝 |
| LLM 生成成功率 | 低 | 高（词表受限） |
| 可复现 | 差 | 完全确定 |

**所谓"预置事件"只是给常用表达式起了个名字**，作用是：①UI 标签 ②`llm.plan()` 的 few-shot 示例
③个股回看的白名单（§4.3）。它**不限制**股票表和板块表能用什么条件。

### 3.2 语法与标的类型

同一套引擎处理两类标的，区别只是可用字段不同（§2.2）：

- **股票**：`$close`、`$volume`、`$market_cap`……
- **板块**：`$close`、`$amount`、`$limit_up_num`……

```python
# 股票：放量突破年线（"放量"按成交额）
Cross($close, Mean($close, 250)) & ($amount > Mean(Ref($amount, 1), 20) * 2)

# 股票：成交额比前一周平均高 40% 以上（"前一周"不含当天，所以先 Ref 挪一天）
$amount > Mean(Ref($amount, 1), 5) * 1.4

# 股票：MACD 金叉
Cross(EMA($close,12) - EMA($close,26), EMA(EMA($close,12) - EMA($close,26), 9))

# 股票：净利润增速全市场前 20%
Rank($profit_yoy) > 0.8

# 板块：成交额比前 20 日均值放大 1.5 倍以上
$amount > Mean(Ref($amount, 1), 20) * 1.5

# 板块排序用的指标：最近 5 日成交额合计
Sum($amount, 5)
```

### 3.3 算子清单（P0 实现全部）

**时序算子**（沿时间轴，逐标的计算）

| 算子 | 语义 |
|---|---|
| `Mean(x, n)` | 过去 n 个交易日均值（含当日） |
| `EMA(x, n)` | 指数移动平均，平滑系数 2/(n+1)。MACD 依赖它 |
| `Std(x, n)` | 过去 n 日标准差 |
| `Sum(x, n)` | 过去 n 日求和 |
| `Max(x, n)` / `Min(x, n)` | 过去 n 日最大/最小值 |
| `Ref(x, n)` | n 个交易日前的值。**n 必须 > 0** |
| `Delta(x, n)` | `x - Ref(x, n)` |
| `Pct(x, n)` | `x / Ref(x, n) - 1`。**返回小数**（0.05 表示 5%）；要用百分数直接用 `$pct_chg` |
| `TsRank(x, n)` | 当前值在过去 n 日中的分位 |
| `Cross(x, y)` | 上穿：`Ref(x,1) <= Ref(y,1) & x > y` |
| `Count(cond, n)` | 过去 n 日内 cond 为真的次数 |

**窗口语义**：时序算子的"过去 n 个交易日"指**该标的自己有数据的交易日**。面板是稀疏的（停牌日没有行），
所以停牌期间不会进入窗口，也不会把成交额按 0 计算——否则长期停牌股复牌当天会被误判成"放量"。
注意区分：**表达式窗口按标的的有效交易日，而"持有 N 天"按交易日历**（§4.3）。

**横截面算子**

| 算子 | 语义 |
|---|---|
| `Rank(x)` | 当日在**当前查询的股票池内**的分位，返回 0~1（已应用行业 / 板块过滤，以及 ST、停牌、次新的剔除；板块表则是当日全部板块）；空值不参与排名。分位的计算范围会写进确认卡的说明 |

`Zscore`、`Quantile` 普通用户用不到，P0 不做。

**逻辑与算术**

```
& | ~ > < >= <= == != + - * /
If(cond, a, b)   Abs(x)   Log(x)   Sign(x)
```

算子的中文语义说明由 `expr.operator_catalog()` 提供给 `llm.plan()`，与校验器的白名单是同一份。

### 3.4 校验器

```python
def validate(ast: Node, target: str) -> ValidationResult:
    """校验五件事，任一不过即拒绝执行"""
    # 1. 所有字段在白名单内（白名单 = data.FIELDS 中该标的类型、且能力可用的字段）
    # 2. 所有算子在白名单内
    # 3. 未来函数检查：Ref/Mean/Std/... 的 n 参数必须是正整数字面量
    # 4. 窗口期上限：n <= 1000
    # 5. 表达式最终结果的类型正确（筛选必须是布尔，排序必须是数值）
    # 6. $close_raw（不复权价）不得出现在任何时序算子里，只能直接比较或参与横截面排名
    #    —— 原始价在除权日会断崖下跌，进均线、突破会产生假信号
```

**未来函数检查是整个系统最重要的一行约束。** 它保证了从语言层面无法写出引用未来数据的表达式。

### 3.5 自动推导

```python
def collect_fields(ast) -> set[str]:
    """从 AST 推导需要哪些字段。例：{close, volume}"""

def collect_lookback(ast) -> int:
    """从 AST 推导需要往前多取多少交易日。
    并列取最大：Mean($close,250) & Ref($volume,5) → max(250, 5) = 250
    嵌套要累加：Mean(Ref($close,5), 250)          → 5 + 250 = 255
    EMA(x, n) 理论上依赖全部历史，按 4n 取预热期"""
```

**这两步的存在，是"LLM 不需要选 tool"的根本原因。** 需要什么数据、往前取多久，全部从表达式自动推导。

由于数据从 2016 年开始，预热期会让实际可统计的起点更晚（例如年线要预热 250 个交易日）。
**确认卡上必须显示实际的统计区间**。

### 3.6 执行流程

```python
def evaluate(expr: str, codes: list[str], start: date, end: date,
             ds: DataService, target: str = "stock") -> pl.DataFrame:
    ast = parse(expr)
    validate(ast, target)                            # 不过则抛错
    fields = collect_fields(ast)
    lookback = collect_lookback(ast)
    calendar = ds.get_trading_calendar(...)
    real_start = shift_trading_days(calendar, start, -lookback)
    panels = ds.get_fields(codes, real_start, end, fields, target)
    result = eval_ast(ast, panels)                   # 返回 (date × code) 矩阵
    return result.filter(pl.col("date") >= start)    # 截掉预热期
```

`ds` 由调用方传入（§1.2 依赖注入），expr 不自行创建 DataService。

---

## 4. research 模块

三种回答，一个入口 `research.run(spec, ds)`，按 `spec.shape` 分派。

| 文件 | 职责 |
|---|---|
| `screener.py` | 股票表、板块表：筛选 → 排序 → 取前 N |
| `history.py` | 个股回看：找出触发日 → 算之后 N 天涨跌 → 对照 |
| `returns.py` | 收益计算的纯函数：起止价格、顺延、扣成本（小表格可测） |

### 4.1 QuerySpec —— 系统的中心数据结构

定义在 **spec 模块**。它是 `llm.plan()` 的输出、确认卡的数据源、`research.run()` 的输入。

**形状一：股票表**

```json
{
  "version": 1,
  "shape": "stock_list",
  "as_of": "2026-09-11",
  "filter": {
    "expr": "$amount > Mean(Ref($amount,1), 5) * 1.4",
    "label": "成交额比前一周均值高 40%"
  },
  "universe": { "base": "all_a", "industry": null, "board": null,
                "exclude": ["ST", "suspended", "new_listing_60d"] },
  "sort": { "by": "$amount / Mean(Ref($amount,1), 5)", "order": "desc" },
  "limit": 50,
  "defaults_used": ["limit"],
  "assumptions": ["「成交量」理解为成交额（元）", "「前一周」按 5 个交易日计算，不含当天"]
}
```

**形状二：板块表**

```json
{
  "version": 1,
  "shape": "board_list",
  "board_type": "concept",
  "as_of": "2026-09-11",
  "filter": null,
  "sort": { "by": "Sum($amount, 5)", "order": "desc" },
  "limit": 10,
  "assumptions": ["「最近一周」按 5 个交易日计算", "板块口径为通达信概念板块"]
}
```

**形状三：个股回看**

```json
{
  "version": 1,
  "shape": "stock_history",
  "target": { "mention": "茅台", "guess": "贵州茅台", "code": "600519.SH" },
  "event": {
    "preset_id": "breakout_ma_volume",
    "params": { "ma": 250, "volume_ratio": 2 },
    "expr": "Cross($close, Mean($close,250)) & ($amount > Mean(Ref($amount,1),20)*2)",
    "label": "放量突破年线"
  },
  "time_range": { "from": "2016-01-01", "to": "2026-09-11" },
  "horizons": [5, 20, 60],
  "benchmark": "universe_equal_weight",
  "cost_bps": 30,
  "assumptions": ["「放量」理解为成交额 > 前 20 日均额的 2 倍", "「1周/1月/3月」= 5/20/60 个交易日"]
}
```

- `assumptions` **由代码从 spec 生成**（§5.4），LLM 不写。它是确认卡最有价值的部分：把模糊描述翻译成明确定义
- `defaults_used` 列出哪些字段用的是默认值，确认卡据此标出"默认值，可修改"
- `target.code` 由 api 调 `ds.resolve_stock()` 填写，LLM 不填代码
- `event.preset_id` 必须在事件库（§4.3）之内，`event.expr` 由代码从模板渲染，LLM 只填 `preset_id` 和 `params`
- `benchmark` 默认 `universe_equal_weight`（当日股票池等权平均），可选 `index:000300.SH`，显示在确认卡上
- `universe.base = all_a` 指沪深 A 股（不含北交所）；`universe.industry` 为申万行业名，按每个交易日当时的归属取成分；
  `universe.board` 形如 `{"type": "concept", "code": "880728.TDX"}`，P0 用当前成分，必须写进 assumptions

### 4.2 股票表与板块表

```
筛选：expr.evaluate(filter.expr, 股票池, as_of, as_of) → 当日满足条件的标的
排序：expr.evaluate(sort.by, ...) → 排序取前 limit 个
输出：标的清单 + 展示列（代码、名称、行业、收盘价（用不复权的真实价格 `$close_raw`）、涨跌幅、成交额、市值，
      以及条件和排序用到的字段）
```

- 筛选和排序都可以为空：只筛选（不排序按成交额降序）、只排序（"涨幅前 50"）、两者都有
- 股票池按 `as_of` 当天的状态确定（PIT）：剔除 ST、停牌、次新，以及不在指定行业/指数中的股票
- 板块表的"股票池"就是该口径下的全部板块

### 4.3 个股回看

**事件库（15 个，参数可调）**，定义在 `signals/builtin.yaml`，加载时逐条过 `expr.validate()`：

| 类别 | 事件 | 表达式（默认参数） |
|---|---|---|
| 均线 | 突破均线 | `Cross($close, Mean($close, 250))` |
| | 跌破均线 | `Cross(Mean($close, 250), $close)` |
| | 均线金叉 | `Cross(Mean($close,5), Mean($close,20))` |
| | MACD 金叉 | `Cross(EMA($close,12)-EMA($close,26), EMA(EMA($close,12)-EMA($close,26),9))` |
| 量能 | 单日放量 | `$amount > Mean(Ref($amount,1),20) * 2` |
| | 放量突破均线 | 突破均线 & 单日放量 |
| | 缩量回调 | `($close < Ref($close,1)) & ($amount < Mean(Ref($amount,1),20) * 0.5)` |
| 价格 | 创 N 日新高 | `$close >= Max($close, 250)` |
| | 创 N 日新低 | `$close <= Min($close, 250)` |
| 涨跌停 | 涨停 | `$is_limit_up` |
| | 连板 | `Count($is_limit_up, 2) == 2`（N 可调） |
| | 跌停 | `$is_limit_down` |
| 日历 | 财报披露日 | `$is_report_date` |
| | 业绩预告日 | `$is_forecast_date` |
| | 除权除息日 | `$is_ex_div` |

**所有事件只取"由不满足变为满足"的那一天**：计算触发日时，引擎自动给事件表达式包一层
`cond & ~Ref(cond, 1)`。否则像"创 250 日新高""缩量回调""连板"这类会连续多天成立的条件，
同一段行情会被重复计入，相邻触发的观察窗口也几乎完全重叠。加上这层包装后，连续 5 天创新高只算 1 次，
三连板只在第 2 天触发一次（第 3 天因为前一天已经成立而不再触发）。

**事件表达式由代码渲染，LLM 不写**：`builtin.yaml` 里每个事件是一段带参数占位的模板，`llm.plan()` 只输出
`preset_id` 和 `params`，表达式由代码渲染。这样"参数和表达式对不上"这种错误从根上不可能发生。

**事件参数要有取值范围**：均线天数 ∈ {5, 10, 20, 60, 120, 250}、放量倍数 1.2~10、连板数 2~10 等，
在 `builtin.yaml` 里定义，加载时和 `llm.plan()` 输出时都由代码核对。

**为什么个股回看要用白名单**：回看要回答"每次发生之后怎样"，前提是这个条件确实"在某一天发生"。
像"市值低于 200 亿"这种会连续成立几百天的状态条件，没有"发生的那一天"。用户用这类条件要求回看时，
返回 `not_an_event` 并说明原因。用户自定义事件放 P1（§11）。

**"之后 N 天涨跌"的口径**

```python
买入日 = 触发日的次一个交易日            # 信号收盘后才能确认；开盘涨停或停牌则继续顺延
起点   = 买入日的开盘价
卖出日 = 买入日之后的第 N 个交易日       # 按交易日历计数，从买入日起算，不是从触发日起算
终点   = 卖出日的收盘价                  # 卖出日跌停或停牌则继续顺延
涨跌   = 终点 / 起点 - 1                # 明细表和平均值都用这个口径，不扣成本
扣成本 = 涨跌 - cost_bps / 10000        # 只在摘要里单独给一行
```

**持有期从买入日起算**，不是从触发日起算：顺延 3 天之后仍然持有完整的 N 个交易日，每一笔的持有长度一致，
对照窗口也用同一段"买入日 → 卖出日"。

`cost_bps` 默认 30，即 **0.3%，买卖双边合计**（佣金、印花税、滑点），用户可在确认卡上修改。
成本单列而不是直接扣进每一笔，是为了让用户看清原始涨跌和成本各占多少。

**必须处理的细节**

| 细节 | 处理 |
|---|---|
| **买入顺延** | 次日开盘涨停（开盘价 = 涨停价）或停牌 → 顺延到第一个"开盘未涨停且未停牌"的交易日 |
| **卖出顺延** | 第 N 天跌停（收盘价 = 跌停价）或停牌 → 顺延到第一个"未跌停且未停牌"的交易日 |
| **顺延上限** | 顺延超过 20 个交易日（长期停牌）→ 标记为"无法成交"，不计入平均，在明细中说明 |
| **未走完样本** | 触发日 + N 日超出数据末日 → 标记"观察中"，不计入平均 |
| **退市** | 持仓期内退市按退市整理期最后价格计算 |

每一次触发都记录顺延情况，接口返回，前端在明细表的"备注"列显示（如"买入顺延 1 天（涨停）"）。

**两行对照**（回答"这个涨幅是信号带来的，还是本来就会涨"）

| 对照 | 算法 |
|---|---|
| 同期市场平均（默认） | 取**这次触发实际的买入日与卖出日**，对当日股票池内全部股票用同一段日历窗口算涨跌，等权平均。对照股票不各自顺延——两边比较的必须是同一段时间。**买入日或卖出日停牌、拿不到价格的股票，从这一次对照中剔除**，并记录剔除只数 |
| 这只股票平时的平均 | 统计区间内每一个交易日都当作起点，用同样的起止算法算之后 N 天涨跌，取平均。不排除触发日，不扣成本 |

**为什么默认等权而不是沪深300**：等权池平均和触发股票同池、同算法，回答的正是"随便买一只是不是也这样"。
沪深300 是大盘股市值加权，拿它对照一只小盘股的触发，差异大半来自风格而不是信号。
用户可以在确认卡上把对照改成沪深300（`benchmark: "index:000300.SH"`）。

### 4.4 输出结构

```python
@dataclass
class ListResult:              # 股票表 / 板块表
    as_of: date
    rows: list[Row]            # 代码、名称、展示列
    total: int                 # 满足条件的总数（取前 N 之前）

@dataclass
class HistoryResult:           # 个股回看
    code: str
    name: str
    event_label: str
    range: tuple[date, date]   # 实际统计区间（已扣掉预热期）
    triggers: list[TriggerRecord]
    summary: dict[int, HorizonSummary]      # key 是 5 / 20 / 60
    n_excluded: dict           # {"无法成交": 1, "观察中": 2}
    notes: list[str]           # 如"仅 23 次，样本偏少"

@dataclass
class TriggerRecord:
    trigger_date: date
    entry_date: date
    entry_delay: Delay | None  # {days, reason: "涨停" | "停牌"}
    exit_date: dict[int, date]
    exit_delay: dict[int, Delay | None]
    returns: dict[int, float]          # 未扣成本
    market_returns: dict[int, float]   # 同期市场平均（同一段日历窗口）
    market_excluded: dict[int, int]    # 对照中因停牌拿不到价格而剔除的股票只数

@dataclass
class HorizonSummary:
    n: int
    mean_return: float             # 未扣成本
    mean_return_after_cost: float  # 扣掉 cost_bps 之后
    mean_market_return: float      # 同期市场平均
    mean_baseline_return: float    # 这只股票平时的平均
    win_rate: float                # 相对同期市场跑赢的比例
```

**P0 不做显著性检验、不给"有效性"结论**（§11）。

### 4.5 提示与警告

固定模板加数字填空，不经过 LLM：

```python
if n_triggers < 20:
    note("仅触发 {n} 次，样本偏少，不能排除是运气")

if n_excluded["无法成交"] > 0:
    note("{k} 次因长期停牌无法成交，未计入平均")
```

---

## 5. llm 模块

### 5.1 配置：Anthropic 协议，base_url 可配

P0 只支持 Anthropic Messages API 协议。使用 `anthropic` 官方 SDK，覆盖 `base_url` 即可对接火山引擎的兼容端点。
**P0 必须配置 LLM key 才能使用**，不做无 key 的降级路径。

```python
# litmus/llm/client.py
class LLMClient:
    """整个项目只通过这个接口调用 LLM。业务代码不感知底层 provider。"""

    def structured(self, system: str, user: str, schema: dict, max_retries: int = 1) -> dict:
        """结构化输出。Anthropic 协议下通过 tool_use 实现：定义一个名为 output 的 tool，
        input_schema 即目标 schema，用 tool_choice 强制调用，返回 tool_use block 的 input。
        失败时把校验错误喂回去重试一次。"""
```

```
# .env
LLM_BASE_URL=https://<火山引擎端点>
LLM_API_KEY=xxx
LLM_MODEL=<模型名>
LLM_TEMPERATURE=0
```

`LLMClient` 是抽象基类，P1 可加 `OpenAICompatClient` 支持 DeepSeek / 通义 / Ollama。

**强制 tool_use 已实测可用**（火山引擎 `ark.cn-beijing.volces.com/api/coding` + `glm-5.3-flash`，2026-09-12）：

- **认证头按端点不同，必须可配**：火山引擎用 `Authorization: Bearer`（SDK 里是 `Anthropic(auth_token=...)`），
  Anthropic 官方用 `x-api-key`（`Anthropic(api_key=...)`）。写死任何一种，另一边都会 401。
  由 `LLM_AUTH_STYLE` 控制：默认 `auto`（`api.anthropic.com` 用 x-api-key，其余用 bearer），
  可显式设为 `bearer` / `x-api-key`；收到 401 时自动换另一种重试一次，并在日志里记下哪种可用
- `tool_choice={"type":"tool","name":"output"}` 能强制调用，返回 `stop_reason=tool_use`
- **响应的 `content` 里可能含 `thinking` 块**（实测是 `["thinking", "tool_use"]`）。
  解析时必须遍历 content 找 `type == "tool_use"` 的块，**不能取 `content[0]`**
- 实测还确认了一件事：不给字段和算子清单时，模型会自己发明表达式语法（它写出了 `amount[-1] > 2 * mean(amount[-21:-2])`）。
  这正是 §5.2 防线② 存在的理由——表达式必须过 `expr.validate()`

**分工**：api 决定"什么时候调"，llm 决定"怎么调"（提示词、模型、重试、结果检查）。api 不接触任何提示词。

### 5.2 llm.plan()：提问 → QuerySpec

**输入上下文**（每次调用都注入，全部由代码生成）：

| 内容 | 来源 |
|---|---|
| 字段清单（含中文说明、单位） | `expr.field_catalog()`，按能力探测结果过滤 |
| 算子清单（含语义） | `expr.operator_catalog()` |
| 事件库（15 条，含表达式与可调参数） | `signals.load_events()` |
| 申万 31 个一级行业名 | `ds.list_boards("sw_industry")`。概念板块有几百个，不进提示词：LLM 只填用户原话，由 `ds.resolve_board()` 解析 |
| 默认值表 | `spec.DEFAULTS` |
| 当前日期与最近已收盘交易日 | `ds.latest_trading_day()` |

**输出**：`status` + 对应内容

| status | 含义 | 附带 |
|---|---|---|
| `ok` | 能回答 | spec、assumptions |
| `needs_clarification` | 有必须澄清的模糊点 | questions（每个 2~3 个选项） |
| `unsupported` | 数据没有 / 不回答的问题 | message、alternatives（改写建议） |
| `not_an_event` | 想回看，但条件不是事件 | message、alternatives |
| `failed` | 重试后仍拿不到合法输出 | — |

**改写建议（alternatives）**：status 不是 ok 时，LLM 额外给 2~3 条**系统能回答的问句**，用户点一下就当成新的提问重走
`/api/plan`。它只是几句问句，不含任何数字和结论；代码会检查条数、长度、是否含禁用词或百分比数字，不通过就丢弃。
LLM 调用本身失败时，前端展示事件库里的固定示例，完全不经过 LLM。

**三道防线**：判断"这个东西存不存在"不能只靠 LLM 自觉。

| 防线 | 位置 | 拦截什么 |
|---|---|---|
| ① LLM 自判 | `llm.plan()` 内，LLM 看清单 | 数据不支持时主动返回 unsupported |
| ② 表达式校验 | `llm.plan()` 内，`expr.validate()` | 编造的字段、未来函数、非法算子、类型不对 |
| ③ 确定性兜底 | api 内 | 见下 |

**防线③ 的三类检查**（任一不过 → 转成 needs_clarification，绝不执行）：

1. **必填项**：股票表必须有 `as_of`；板块表必须有 `board_type` 和排序指标；个股回看必须有 `target.code` 和 `event.preset_id`
2. **白名单核对**：股票过 `ds.resolve_stock()`；板块过 `ds.resolve_board()`；事件过 `signals.load_events()`
3. **参数范围**：事件参数必须落在 `builtin.yaml` 定义的取值范围内（均线天数、放量倍数、连板数等）。
   表达式由代码渲染，不存在"参数与表达式对不上"的可能（§4.3）

### 5.3 澄清与默认值

**必须澄清的清单**（写在 prompt 里，不给默认值）：

| 情况 | 例子 |
|---|---|
| 完全没给时间范围 | "最近哪个板块成交量最大" |
| 板块口径不明（两种口径都可用时） | 说"板块"但没说行业还是概念 |
| 问的是股票还是板块分不清 | "最近哪个最强" |
| 无法定义的说法 | 黄金坑、好股票、股性佳、主力吸筹 |
| 想回看但条件不是事件 | "茅台市值低于 2000 亿之后的表现" |

**可以给默认值的清单**（不打断，但必须写进 assumptions 显示在确认卡上），默认值写在 `spec.DEFAULTS` 并渲染进 prompt：

```python
# litmus/spec/defaults.py
DEFAULTS = {
    "volume_surge_ratio": 2.0,      # "放量" = 前 20 日均量的几倍
    "volume_shrink_ratio": 0.5,     # "缩量"
    "week_days": 5,                 # "1周" = 几个交易日
    "month_days": 20,
    "quarter_days": 60,
    "year_days": 250,
    "top_n": 50,                    # "前N名"没说 N 时取多少
    "volume_means": "amount",       # "成交量"默认理解为成交额
    "cost_bps": 30,                 # 交易成本，买卖双边合计
}
```

默认值放在代码里而不是写死在提示词文字中：改一次到处生效、可以写测试、确认卡上能标出"这是默认值"。
生成提示词时把这张表渲染进去，保证 LLM 看到的默认值和代码里的永远是同一份。

### 5.4 assumptions 由代码生成，不经过 LLM

确认卡上的说明文字（"「放量」理解为成交额 > 前 20 日均额的 2 倍"）**全部由 spec 用模板确定性生成**，
LLM 既不写也不审查。

```python
# litmus/spec/assumptions.py：模板按字段挂，spec 变了说明跟着变
"volume_ratio": "「放量」理解为成交额 > 前 {window} 日均额的 {volume_ratio} 倍"
"horizons":     "「1周/1月/3月」= {horizons} 个交易日"
"board":        "「{board_name}」按**当前成分**取，当时在、现在不在的股票不会出现"
"benchmark":    "对照口径：{benchmark_label}"
```

这样一次解决三件事：

- **说明永远不会过时**：用户把 2 倍改成 2.5 倍，说明文字跟着重新生成，不可能出现文字与参数对不上
- **不需要第二次 LLM 调用**：原设计中的 `llm.review()` 去掉了。用户在确认卡上改了参数，那就是新的意图，
  不需要 LLM 再判断"改得对不对"；改得离谱与否由参数范围（§4.3）和数值范围确定性拦截
- **没有死锁**：原设计中审查不通过就无法执行，而审查者本身可能判错，用户没有任何绕过路径

`spec.defaults_used` 记录哪些字段用的是默认值，确认卡据此标出"默认值，可修改"。

### 5.5 prompt 管理

一个 prompt 一个文件，文件名即 ID：

```
litmus/llm/prompts/
├── planner.system.md       # llm.plan() 主提示词
└── planner.repair.md       # 校验失败后带错误信息重试
```

**规则**：
1. 占位符用 `{{变量}}`。不用 `$变量`（与字段 `$close` 冲突），不用 `{变量}`（与 JSON 示例冲突）
2. prompt 只写固定说明文字；字段清单、算子清单、事件库、默认值表由代码生成后填入，
   保证"LLM 看到的"与"代码执行的"永远一致
3. 加载时严格检查：文件内 `id` 与文件名一致；`{{变量}}` 不缺不多
4. 更新 prompt 就是改 md 文件，git 历史即版本记录
5. 每次调用在日志中记录 `prompt ID + 内容哈希前 8 位`，可追溯

### 5.6 LLM 使用边界

**整个系统只有一处调用 LLM**：

| 环节 | 函数 | 调用方 |
|---|---|---|
| 提问 → QuerySpec（含澄清、改写建议） | `llm.plan()` | `POST /api/plan` |

**其余环节全部不使用 LLM**：

| 任务 | 怎么做 |
|---|---|
| 股票名 → 代码 | `ds.resolve_stock()` 确定性查找 |
| 确认卡渲染、改参数 | 直接修改 QuerySpec 字段；说明文字由模板生成，参数范围由代码校验（§5.4） |
| 渲染表格 | LLM 有概率把 +3.1% 写成 +3.7%，金融产品不可接受 |
| 选择数据 / 接口 | 由 `collect_fields` 从 AST 自动推导 |
| 计算任何数字 | 全部由 Python 计算 |
| 各类提示文案 | 固定模板加数字填空 |
| 自由问答 / 买卖建议 | **不做**。答不了时只给改写建议（§5.2） |

---

## 6. API 设计

```
POST /api/plan
  body: { "query": "...", "previous_plan_id": "..." }   # previous_plan_id 可选，用于多轮澄清与改意思
  resp: { "status": "ok|needs_clarification|unsupported|not_an_event|data_not_ready|failed",
          "plan_id": "...", "spec": QuerySpec, "assumptions": [...],
          "questions": [...], "alternatives": [...], "target_candidates": [...] }
  说明：调 llm.plan()，再做确定性兜底核对；保存原话与 spec 得到 plan_id；
        返回未执行的 QuerySpec，前端渲染成确认卡或澄清卡

POST /api/run
  body: { "spec": QuerySpec, "plan_id": "..." }
  resp: { "status": "done|needs_revision|data_not_ready|failed",
          "issues": [...], "run_id": "...", "result": ListResult | HistoryResult }
  说明：确定性检查（spec 校验、表达式校验、事件参数范围、数值范围）→ research.run()。
        用户改过参数走同一套检查，不再调 LLM；assumptions 由 spec 重新生成

GET  /api/run/{run_id}
  说明：轮询结果 / 分享链接打开

GET  /api/events
  说明：事件库列表（供 UI 标签与示例展示）

GET  /api/boards?type=sw_industry|concept
  说明：可用板块清单

POST /api/data/sync
  说明：启动后台同步（首次为 2016 年至今全部历史，之后只补缺口）；已在同步中则返回当前进度

GET  /api/data/status
  说明：同步进度、本地数据覆盖范围、最近同步时间、各可选数据的可用状态、失败原因
```

**性能预期**

| 场景 | 数据量 | 耗时 |
|---|---|---|
| 股票表 / 板块表（单日） | 5000 只 × 1 天 + 预热期 | < 1 秒 |
| 个股回看（单只股票 10 年） | 2600 天 | < 1 秒 |
| 个股回看的"同期市场平均" | 5000 只 × 触发日数 | 1~5 秒 |

**P0 全部同步返回**，前端显示加载中。"超过 3 秒走异步"事前无法判断——请求还没跑完，不知道它要多久。
等实测个股回看确实超过 10 秒，再改成按形状决定（个股回看异步、两张表同步）。

---

## 7. store 模块（JSON 文件存储）

```
data/store/
├── plans/<plan_id>.json     # llm.plan() 的原话与生成的 spec，供 /api/run 判断用户是否改过参数
└── runs/<run_id>.json       # 运行记录：spec + 结果（已序列化的 dict），供分享链接和排查问题
```

```python
class Store(Protocol):
    def save_plan(self, plan: PlanRecord) -> str: ...          # 返回 plan_id
    def get_plan(self, plan_id: str) -> PlanRecord | None: ...
    def save_run(self, run: RunRecord) -> str: ...             # 返回 run_id
    def get_run(self, run_id: str) -> RunRecord | None: ...
```

- **一条记录一个文件**，不把所有记录塞进一个大 JSON
- **原子写入**：先写临时文件，再 `os.replace` 改名
- **接口用业务语言**，不出现 `read_file`、`list_dir` 这类文件操作
- **只有 store 知道文件在哪**：其他模块一律通过接口读写
- **store 不认识 research 的类型**：`RunRecord.result` 是 dict，research 结果到 dict 的转换由 api 负责。
  store 与 research 同层，直接引用 `ListResult` / `HistoryResult` 会违反 §1.2 的依赖规则

以后换 SQLite：新增 `SqliteStore` 实现同一接口 → 通过同一套契约测试 → api 换一个类名，上层无需改动。

---

## 8. 目录结构

```
litmus/
├── spec/                   # 零内部依赖
│   ├── query_spec.py       # QuerySpec（Pydantic），三种形状
│   ├── defaults.py         # 默认值表
│   └── assumptions.py      # 由 spec 生成确认卡说明文字的模板
├── data/                   # 零内部依赖
│   ├── service.py          # DataService：读本地 Parquet
│   ├── fields.py           # 字段目录 FIELDS
│   ├── resolve.py          # 股票名解析
│   ├── loaders/            # tushare.py：拉取、归一、分页、返回校验
│   └── sync.py             # DataSync：全量/增量同步、断点续传、能力探测、manifest
├── expr/                   # 依赖 data
│   ├── parser.py           # 表达式 → AST
│   ├── validator.py        # 白名单 + 未来函数检查
│   ├── collector.py        # collect_fields / collect_lookback
│   ├── evaluator.py        # AST 求值
│   ├── catalog.py          # field_catalog / operator_catalog
│   └── operators/
│       ├── timeseries.py
│       ├── cross_section.py
│       └── logic.py
├── signals/                # 依赖 expr
│   ├── loader.py           # load_events()，加载时逐条 expr.validate()
│   └── builtin.yaml        # 事件库（15 条）
├── research/               # 依赖 expr、data、spec
│   ├── screener.py         # 股票表、板块表
│   ├── history.py          # 个股回看
│   └── returns.py          # 收益计算纯函数（起止价格、顺延、扣成本）
├── llm/                    # 依赖 expr、signals、spec
│   ├── client.py           # LLMClient
│   ├── planner.py          # plan()
│   ├── models.py           # PlanResult 等 llm 自有的数据结构
│   ├── prompts.py          # prompt 加载与渲染
│   └── prompts/            # 见 §5.5
├── store/                  # 零内部依赖
│   ├── base.py             # Store 接口 + PlanRecord / RunRecord
│   └── json_store.py       # JSON 文件实现
├── api/                    # 依赖以上全部
│   ├── main.py
│   ├── routes/
│   └── models.py           # HTTP 请求/响应模型
└── cli.py                  # litmus serve；litmus sync 仅供开发调试

web/                        # React 前端
tests/
├── contract/               # 契约测试：test_dataservice.py / test_store.py
├── fixtures/               # 合成数据集与生成脚本（§2.4）
└── ...                     # 各模块单元测试
api_define/                 # Tushare 接口定义（本地参考，不提交）
docker-compose.yml
Makefile
```

---

## 9. P0 开发顺序

严格按此顺序，每步都有可验证的产出：

| 步骤 | 内容 | 验收标准 |
|---|---|---|
| 0 | 项目骨架：uv + pyproject + 目录 + pytest/ruff；**用一条最小请求验证火山引擎支持强制 tool_use** | 骨架已完成（目录随各步补齐）；确认结构化输出可用，否则 §5 要换方案。import-linter 契约在出现第一个跨模块 import 时加入 |
| 1a | data：`FIELDS` + Tushare loader（归一、分页、返回校验）+ DataSync（全量/增量同步、断点续传、能力探测、manifest） | 能把指定日期范围同步到本地；中断可续传；再次同步只补缺口；完成 §12 的接口实测项 |
| 1b | data：DataService（含按日股票池、板块）+ 合成数据集 + 契约测试 | 契约测试在合成数据集上离线通过；有 token 时再跑一遍真实数据 |
| 2 | expr：parser / validator / collector / evaluator + 全部算子，支持股票与板块两类标的 | 小表格测试：`Cross`、`Mean`、`Rank` 等逐个验证 |
| 3 | spec（三种形状 + 默认值表）+ research：股票表、板块表、个股回看 | 手工核对若干笔"之后 N 天涨跌"，含顺延与扣成本 |
| 4 | signals：事件库 15 条 | YAML 加载，逐条校验通过，都能跑出回看结果 |
| 5 | store（JSON）+ FastAPI：`/api/run`（先不接 LLM，直接传 QuerySpec）+ `/api/data/sync`、`/api/data/status` | Postman 能跑出三种结果；能触发同步并查看进度 |
| 6 | 前端：同步页 + 三种结果页 | 页面上能完成同步，并看到三种结果 |
| 7 | `llm.plan()` + prompt 管理 + `ds.resolve_stock()` / `ds.resolve_board()` + 确认卡 / 澄清卡（说明文字由模板生成） | 自然语言能正确生成三种形状；编造字段被拦截；"平安"返回多个候选；"最近哪个板块最强"先澄清；"现在能买茅台吗"给改写建议；改完参数说明文字跟着变 |
| 8 | Docker 打包 | 全新环境按 README 能完成部署、同步并正常使用 |

---

## 10. 测试要求

**小表格测试**：计算逻辑（算子、收益）写成纯函数，测试里手写几行输入和手算的正确答案。不联网、不需要 key。

```python
def test_mean():
    close = [10, 11, 12, 13, 14]            # 手写 5 天收盘价
    expected = [None, None, 11, 12, 13]     # 手算的 3 日均线
    assert Mean(close, 3) == expected
```

必须有测试的部分：

| 模块 | 测试重点 |
|---|---|
| 依赖规则 | `lint-imports` 通过，每次提交必跑 |
| DataService 契约 | 在合成数据集上离线运行；有 token 时再跑一遍真实数据，见 §2.4 |
| loader | 需要 token：单位换算正确；达到单次上限时自动分页；字段名或类型不符时报错 |
| DataSync | 需要 token：中断后续传不重复不遗漏；增量同步只补缺口；能力探测结果正确写入 manifest |
| validator | 未来函数必须被拦截：`Ref($close, -1)` 应抛错；字段不在白名单、能力不可用时报错 |
| collector | lookback 推导：并列取最大、嵌套累加、EMA 按 4n |
| operators | 每个算子用小表格验证，特别是 `Cross` 的边界和 `Rank` 的空值处理 |
| 停牌语义 | 停牌日不进窗口：长期停牌股复牌当天不会被判成"放量"；`Ref($close,1)` 取到的是停牌前最后一个交易日 |
| returns | 手工构造 5 个触发点，核对起止价格、买入/卖出顺延、扣成本、无法成交的剔除 |
| history | 事件只在"由不满足变为满足"那天触发（连续成立不重复计、三连板只算一次）；同期市场平均与这只股票平时平均的计算口径 |
| screener | 股票表 / 板块表：筛选、排序、取前 N；板块字段正确路由到板块数据表 |
| resolve_stock | 六条匹配规则逐条覆盖；多个候选全部返回 |
| prompts | 所有 prompt 能渲染；变量不缺不多；id 与文件名一致 |
| llm.plan | 用假 LLMClient：编造字段被拦截；缺必填项转澄清；状态条件要求回看时返回 not_an_event；改写建议含禁用词时被丢弃 |
| assumptions | 改了参数说明文字跟着变；默认值字段被标出"默认值"；板块条件带上"按当前成分"的说明 |

---

## 11. 推迟到 P1 的内容

这些不是不做，是 P0 不做。推迟的部分不能给 P0 留下错误的口径。

| 内容 | 推迟原因 |
|---|---|
| 全市场有效性判定、显著性检验（t 检验、p 值、按触发日聚合） | 需要配套防过拟合才有意义，一起做 |
| 防过拟合：样本外锁定、调参次数计数与警告、多重检验折减 | 同上 |
| 结果缓存与数据版本号 | 每次重算 3~8 秒可接受，先不引入一致性问题 |
| 回归测试（固定数据结果逐位相同） | 没有缓存和统计量后优先级下降；小表格测试已覆盖计算正确性 |
| `llm.explain()`（LLM 写解读文字） | 页面上都是朴素数字，解读价值不大 |
| 用户自定义事件（个股回看） | 需要一套"这个条件算不算事件"的判定规则 |
| 概念板块历史成分 | `tdx_member` 按交易日拉取量大，且回溯范围待实测 |
| 龙虎榜、游资、人气榜、机构目标价、涨停原因 | 数据有（6000~10000 积分），但不是三种回答的必需品 |
| 资金门槛、容量估算 | 需要不复权价格字段，见 §12 |
| 个股回看的"同行业对照" | 数据已具备，UI 成本待评估 |
| 免费数据源（BaoStock / AKShare） | 一次安装只用一种数据源，需要各自的 loader |
| 无 LLM key 的降级路径 | P0 必须配 key |
| `llm.review()`（LLM 审查用户改动） | 已取消，不是推迟：说明文字改由模板生成、参数范围由代码校验后，它的两个目标都有确定性替代，而它本身会带来死锁 |
| 限售解禁（`share_float` → `$is_unlock_date`） | 数据量与收益不成比例。2026-09-13 实测：单个解禁日 22904 行，一个自然月超过 10 万行——**连一个月都拉不完**，翻到第 18 页就撞上代理的 offset 上限（offset=60000 正常、102000 被拒）。只能按天切，约 2600 个交易日、每天数页，而且这个接口扛不住并发、只能串行，估计十几个小时，只为换回一个布尔字段。归一逻辑 `normalize_share_float` 已经写好并有测试，P1 接上 DataSync 即可 |

---

## 12. 待决事项

| # | 事项 | 何时决定 | 当前倾向 |
|---|---|---|---|
| 1 | `spec.DEFAULTS` 里几个阈值的具体取值（"小市值"等） | 第 7 步 | 做确认卡时定，能显示能改 |
| 2 | 接口实测项 | 第 1a 步 | ① 各接口 2016 年起的数据是否完整；② `fina_indicator_vip` 同一报告期是否返回更正记录，据此确定 PIT 处理；③ `$roe` 用 `roe_yearly` 是否合适；④ 申万 2021 版对 2021 年以前的归属口径；⑤ 沪深300 在 `index_daily` / `index_weight` 中的代码写法；⑥ `tdx_member` 历史成分能回溯多久；~~⑦ `sw_daily` 与 `tdx_daily` 的历史能回溯到哪一年~~ **已实测**：`sw_daily` 从 2012-08 起（覆盖我们的 2016 起点）；概念板块三家差别很大——通达信 `tdx_daily` 只到 **2025-03**，东财 `dc_daily` 到 2020-01，同花顺 `ths_daily` 到 2016-01。**板块表的可用区间必须在确认卡上标注**，口径选择见 §2.7；⑧ 能力探测的失败分支：本账号 10000 积分、所有接口都通，需要用一个无权限 token 或不存在的接口名来验证权限错误长什么样<br>**已完成**：HTTP 协议与各积分档接口连通（`trade_cal` / `daily_basic` / `stock_st` / `sw_daily` / `tdx_index` / `hm_detail` 全部返回 `code=0`） |
