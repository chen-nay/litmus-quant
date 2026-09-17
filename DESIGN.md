# 查询结构（v2）

> 状态：**设计定稿中，代码在 `feature/filter-p2` 上开发**。开发完成后并入 ARCHITECTURE.md，本文件删除。
> 依据是 [QUESTIONS.md](QUESTIONS.md) 的 79 条真实提问；改结构就要拿那份清单重新验一遍。

## 1. 五个维度

一次查询由五样正交的东西组成：

```json
{
  "version": 2,
  "scope":   { "在谁身上算" },
  "subject": { "最后看谁" },
  "when":    { "哪天 / 哪段" },
  "metrics": [ "算什么" ],
  "output":  { "怎么出" },
  "narrate": false
}
```

形态分布（79 条提问）：表 42、卡 17、统计 14、拒 6。

### 1.1 scope —— 在谁身上算

决定哪些标的参与计算，也是 `Rank` 的范围和「全A等权」对照的口径。

```json
{ "target": "stock",                              // stock / sw_industry / sw_industry_l2 / concept
  "base": "all_a",                                // all_a / hs300 / zz500，只对 stock 有意义
  "industry": "农林牧渔",                          // 可选，申万一级或二级
  "board": { "type": "concept", "code": "..." },  // 可选
  "exclude": ["ST", "suspended", "new_listing_60d"] }
```

没填时由代码补 `{target: stock, base: all_a, exclude: 默认三项}`，并记进 `defaults_used`。

### 1.2 subject —— 最后看谁

```json
{ "kind": "pool" }                             // 整个池子，配 output=table
{ "kind": "codes", "codes": ["002714.SZ"] }    // 点名，配 output=card / event_study
{ "kind": "aggregate" }                        // 对整个池子求一个数，没有标的
```

scope 和 subject 是两件事。「牧原股份在农林牧渔里涨幅排第几」：scope 是农林牧渔 116 只——排名在这里面算；
subject 是牧原 1 只——最后只显示它。

**卡和表的区别是行从哪来**：卡的行由 subject 点名，表的行由 output 筛出来。
所以「牧原跟温氏今年谁涨得多」是一张两行的卡。

大模型只填原话和猜测名，代码查代码（防线③，同 v1）：

```json
{ "kind": "codes", "mentions": [{ "mention": "牧原股份", "guess": "牧原股份" }] }
```

### 1.3 when —— 哪天 / 哪段

```json
{ "as_of": "2026-09-14" }                                  // 时点：table、card
{ "range": { "from": "2016-01-04", "to": "2026-09-14" } }  // 区间：event_study
```

二选一。没填时由代码补，**按这个查询实际用到哪类数据定**：用概念板块就取概念板块的最新一天
（本地到 2026-09-11），用股票行情就取股票的最新一天（2026-09-14）。区间没填取本地全部数据范围。

### 1.4 metrics —— 算什么

结果表和卡上要显示的指标，一组表达式。

```json
[ { "expr": "$pe_ttm",                    "label": "市盈率TTM" },
  { "expr": "TsRank($pe_ttm, 500)",       "label": "两年分位" },
  { "expr": "PctSince($close, 20251231)", "label": "今年以来涨幅" } ]
```

每一项过 `expr.parse` + `expr.validate`，和 filter、sort 走同一套校验，未来函数在语法层杜绝。
`label` 是确认卡和结果表上的列名。

### 1.5 output —— 怎么出

```json
{ "kind": "table",  "filter": {...}, "sort": {...}, "limit": 50 }
{ "kind": "card" }
{ "kind": "event_study", "event": {...}, "horizons": [5,20,60],
  "benchmark": "universe_equal_weight", "cost_bps": 30 }
```

- **table**：筛选 → 排序 → 取前 N
- **card**：把 metrics 原样列出来。没有 filter / sort / limit
- **event_study**：按事件触发点分组统计。`universe_equal_weight` 指 scope 的等权

### 1.6 narrate —— 要不要再写一段话

算完之后多一步：把算好的数字交给大模型，让它写一段总结。

- 数字全部由代码算，大模型只挑哪几个讲、怎么串
- 那段话旁边永远并排显示它引用的卡，用户能逐个核
- 代码把话里出现的每个数字跟算出来的值比一遍，对不上就重试（同防线②）
- **只在 `output=card` 时可用**
- **只有数字保证跑一万次相同，那段话不保证逐字相同**，所以话是数字的附属品，不能单独存在
  （这是对 ARCHITECTURE §0「确定性优先」的一处明确限定）

## 2. 几条代表性提问的 spec

### A101 牧原股份现在市盈率多少，两年里算高算低

```json
{ "scope":   { "target": "stock", "base": "all_a" },
  "subject": { "kind": "codes", "codes": ["002714.SZ"] },
  "when":    { "as_of": "2026-09-14" },
  "metrics": [ { "expr": "$pe_ttm", "label": "市盈率TTM" },
               { "expr": "TsRank($pe_ttm, 500)", "label": "两年分位" },
               { "expr": "$pb", "label": "市净率" },
               { "expr": "TsRank($pb, 500)", "label": "市净率两年分位" } ],
  "output":  { "kind": "card" },
  "narrate": true }
```

### A201 牧原在农林牧渔里涨幅排第几（scope ≠ subject）

```json
{ "scope":   { "target": "stock", "industry": "农林牧渔" },
  "subject": { "kind": "codes", "codes": ["002714.SZ"] },
  "when":    { "as_of": "2026-09-14" },
  "metrics": [ { "expr": "Rank(PctSince($close, 20251231))", "label": "今年以来涨幅排名" } ],
  "output":  { "kind": "card" } }
```

### B104 农林牧渔里今年以来涨幅前 10

```json
{ "scope":   { "target": "stock", "industry": "农林牧渔" },
  "subject": { "kind": "pool" },
  "when":    { "as_of": "2026-09-14" },
  "output":  { "kind": "table",
               "sort": { "by": "PctSince($close, 20251231)", "order": "desc" },
               "limit": 10 } }
```

### A301 牧原每次放量突破年线之后

```json
{ "scope":   { "target": "stock", "base": "all_a" },
  "subject": { "kind": "codes", "codes": ["002714.SZ"] },
  "when":    { "range": { "from": "2016-01-04", "to": "2026-09-14" } },
  "output":  { "kind": "event_study",
               "event": { "preset_id": "breakout_ma_volume", "params": { "ma": 250 } },
               "horizons": [5, 20, 60],
               "benchmark": "universe_equal_weight", "cost_bps": 30 } }
```

### D105 放量突破年线在半导体上比银行股更管用吗（P1）

`subject=pool` + `output=event_study` 就是全市场有效性判定，结构上不用新东西，计算另做。

> 上面这些表达式 2026-09-17 逐条过了 `expr.validate()`，全部通过。v2 不给表达式引擎加任何东西。

### 表达不了的

| 问句 | 缺什么 |
|---|---|
| E104 今天有多少只股票上涨 | `subject=aggregate` 要横截面聚合算子（计数、求和、均值），现在只有 `Rank` |
| B111 半导体这个板块历史上什么时候涨得最好 | `event_study` 对板块标的的买入卖出语义要定 |
| C208 走出 W 底的股票 | 要形态识别，是产品决策 |

## 3. 非法组合

`spec` 层逐条校验，一条规则一个测试。

| # | 组合 | 报什么 |
|---|---|---|
| 1 | `output=card` + `subject=pool` | 卡要点名看谁，或者改成表 |
| 2 | `output=card` 带了 `filter` / `sort` / `limit` | 卡不筛不排，这几项只有表能用 |
| 3 | `metrics` 里有 `Rank`，但 scope 只有 subject 点名的那几只 | 要排名就得给一个更大的范围 |
| 4 | `output=event_study` + `when.as_of` | 事件统计要一段区间 |
| 5 | `output=table` / `card` + `when.range` | 表和卡是某一天的截面 |
| 6 | `subject=aggregate` + `output=table` / `event_study` | 聚合只有一个数，没有行也没有触发点 |
| 7 | `output=card` + `metrics` 为空 | 卡上要有指标 |
| 8 | `subject.codes` 不在 scope 里 | 说明它不在这个范围内。只在 scope 有 industry / board 限定时查 |
| 9 | `output=event_study` 里用 `Rank` | 回看只有触发点，没有截面（沿用 v1 的报错） |
| 10 | `narrate=true` + `output` 不是 card | 总结只跟着卡走 |

## 4. 大模型输出格式

**嵌套 schema，2026-09-17 在火山引擎 `glm-5.3-flash` 上实测通过**：四个问题（卡、卡+scope≠subject、
表、事件回看）全部返回合法的嵌套结构，`metrics` 对象数组也正常，12~21 秒。
第一次调用 input 1117 token，后面三次只有 24~32 token——提示词被缓存了。

实测同时发现的几件事，提示词和校验要覆盖：

| 现象 | 对策 |
|---|---|
| `output=card` 里带了 `sort` | 非法组合第 2 条 |
| `scope` 整个缺失 | 代码补默认并记 `defaults_used` |
| 四题 `narrate` 全填 true，包括一张 10 行的表 | 非法组合第 10 条；提示词写清什么时候才给总结 |
| `guess` 被填成代码（`"002714"`、`"002714.SZ"`） | schema 描述写死：guess 是猜的**全称**，不是代码；代码由 `ds.resolve_stock` 查 |
| 「今年以来」写成 `PctSince($close, 20260101)` | 提示词里的日期换算表必须带过来（应为起始日**前一个**交易日 20251231） |
| 事件回看的区间自己编了个 5 年 | 提示词写清：没说区间就不填，代码用本地全部数据 |

## 5. 大模型接入

`/api/coding` 这条路只提供 Anthropic 协议。2026-09-17 实测火山引擎原生 SDK（`arkruntime`）：
`/api/coding` 下 `responses.create`、`chat.completions` 都是 404；换 `/api/v3` 两个都返回
`InvalidEndpointOrModel.NotFound`。所以用 `anthropic` SDK 打 `/api/coding`。

**每家大模型有自己的一组环境变量**，`litmus/llm/providers.py` 按固定顺序找，
第一家必填字段配齐的就是要用的那家：

```
# 火山引擎方舟
ARK_API_KEY=            # 必填
ARK_MODEL=              # 必填
# ARK_BASE_URL=         # 默认 https://ark.cn-beijing.volces.com/api/coding

# Anthropic
# ANTHROPIC_API_KEY=    # 必填
# ANTHROPIC_MODEL=      # 必填
# ANTHROPIC_BASE_URL=   # 默认 https://api.anthropic.com
```

认证头随厂商定死（火山引擎 `Authorization: Bearer`，Anthropic 官方 `x-api-key`），不再按域名猜、
也不再 401 之后换一种重试。

## 6. 范围

**不动**：`data` 4477 行（后复权、停牌、PIT、股票池）、`expr` 1416 行（30 个算子）、
`signals` 369 行（事件库）、`store` 180 行。

**重写**：`spec` 537 / `research` 692 / `llm` 971 / `api` 1727 / `web` 3362，
外加 82 处 `stock_list` / `board_list` / `stock_history` 硬编码、测试里 101 处引用。

v1 的代码路径、旧 spec 的兼容层都不留。`store/runs/` 里已有的 v1 运行记录直接作废，
开发期间的记录没有保留价值。

## 7. 开发顺序

| 步 | 做什么 | 验收 |
|---|---|---|
| 0 | ~~实测嵌套 schema~~ | ✅ 2026-09-17 通过，见 §4 |
| 1 | `llm/providers.py`：每家自己的环境变量 + 按顺序探测；认证头随厂商定死 | 两家各自配齐时选对；都没配时报错说清缺什么 |
| 2 | `spec` v2：五个维度 + 10 条非法组合 | 手写代表性 spec 过校验；非法组合一条一个测试。`defaults_used` 的路径做成常量，和 `DEFAULTS` 的 key 有一条比对测试 |
| 3 | 确认卡文案：先手写 5 份样子确认，再写代码 | 五个维度拼出来的说明，信息不比 v1 少 |
| 4 | `research` v2：取 scope → 算 metrics → 按 output 整形 | 表和统计在真实数据上出正确结果 |
| 5 | `card` 这条路 | A101、A201 在真实数据上正确 |
| 6 | `llm` v2：新 schema + 提示词（带上日期换算表、行业清单、事件库） | 79 条跑真实大模型，记录每条落到哪个形态、对不对 |
| 7 | `narrate` + 数字核对 | 故意让大模型写错数，核对要拦住 |
| 8 | 前端：卡的结果页、表单按维度重组 | 浏览器里把 A、B、C 三组各点几条 |

## 8. 还没定的

1. `output.sort.by` 要不要也必须出现在 `metrics` 里
2. `subject=aggregate` 要不要 P0 做（只有 E104 一条，且要新加横截面聚合算子）
3. 确认卡的排版——五个维度拼出来读着顺不顺，得做出来看
