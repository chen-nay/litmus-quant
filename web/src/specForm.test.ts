import dayjs from "dayjs";
import { describe, expect, it } from "vitest";

import {
  buildStudy,
  buildTable,
  disabledDay,
  dropUntouchedDefaults,
  fieldForIssue,
  issueText,
  splitIssues,
  studyValues,
  tableValues,
} from "./specForm";
import type { EventInfo, Scope, Spec } from "./types";

const BREAKOUT: EventInfo = {
  id: "breakout_ma",
  name: "突破均线",
  category: "均线",
  template: "Cross($close, Mean($close, {ma}))",
  label: "突破 {ma} 日均线",
  params: [
    {
      name: "ma",
      label: "均线天数",
      unit: "天",
      allowed: "5/10/20/60/120/250",
      default: 250,
      kind: "choice",
      choices: [5, 10, 20, 60, 120, 250],
      min: null,
      max: null,
    },
  ],
  constraints: [],
  example: { question: "", params: { ma: 250 } },
};

const SCOPE: Scope = {
  target: "stock",
  base: "all_a",
  industry: "农林牧渔",
  board: null,
  exclude: ["ST", "suspended", "new_listing_60d"],
};

/** 确认卡上的一张表：农林牧渔里今年以来涨幅前 10 */
const TABLE: Spec = {
  version: 2,
  scope: SCOPE,
  subject: { kind: "pool", mentions: [], codes: [] },
  when: { as_of: "2026-09-16", range: null },
  metrics: [{ name: "今年以来涨幅", expr: "PctSince($close, 20251231)" }],
  output: { kind: "table", filter: null, sort: { by: "今年以来涨幅", order: "desc" }, limit: 10 },
  narrate: false,
  defaults_used: ["when", "scope.base", "scope.exclude"],
  assumptions: ["算的范围：农林牧渔（申万一级行业，100 只）"],
};

/** 确认卡上的一份统计：茅台放量突破年线 */
const STUDY: Spec = {
  version: 2,
  scope: { ...SCOPE, industry: null },
  subject: { kind: "codes", mentions: [{ mention: "茅台", guess: "贵州茅台" }], codes: ["600519.SH"] },
  when: { as_of: null, range: { from: "2016-01-04", to: "2026-09-16" } },
  metrics: [],
  output: {
    kind: "event_study",
    event: { preset_id: "breakout_ma", params: { ma: 250 } },
    horizons: [5, 20, 60],
    benchmark: "universe_equal_weight",
    cost_bps: 30,
  },
  defaults_used: ["when", "output.horizons", "output.benchmark", "output.cost_bps"],
};

describe("表单 ↔ 查询条件", () => {
  it("表：填回表单再生成，和原来的一样", () => {
    const { version: _v, narrate: _n, defaults_used: _d, assumptions: _a, ...rest } = TABLE;
    expect(buildTable(tableValues(TABLE))).toEqual(rest);
  });

  it("表：改了的栏目转成接口要的类型；空的筛选是 null；空行的指标去掉", () => {
    const values = tableValues(TABLE);
    values.when.as_of = dayjs("2026-09-11");
    values.output.limit = "20";
    values.output.filter = { expr: "  $pct_chg > 9 ", label: "涨停" };
    values.metrics.push({ name: " ", expr: "" });
    const spec = buildTable(values);
    expect(spec.when.as_of).toBe("2026-09-11");
    expect(spec.output).toEqual({
      kind: "table",
      filter: { expr: "$pct_chg > 9", label: "涨停" },
      sort: { by: "今年以来涨幅", order: "desc" },
      limit: 20,
    });
    expect(spec.metrics).toHaveLength(1);
  });

  it("表：排板块时不带股票池、行业、概念、剔除", () => {
    const values = tableValues(TABLE);
    values.scope.target = "sw_industry";
    expect(buildTable(values).scope).toEqual({
      target: "sw_industry",
      base: "all_a",
      industry: null,
      board: null,
      exclude: ["ST", "suspended", "new_listing_60d"],
    });
  });

  it("统计：填回表单再生成一样；持有天数去重排序、代码转大写、没填的参数用默认值", () => {
    const { version: _v, defaults_used: _d, ...rest } = STUDY;
    const rebuilt = buildStudy(studyValues(STUDY), BREAKOUT, STUDY.scope);
    expect(rebuilt).toEqual({ ...rest, subject: { kind: "codes", mentions: [], codes: ["600519.SH"] } });

    const values = studyValues(STUDY);
    values.subject.codes = " 000001.sz ";
    values.output.horizons = ["20", "5", "20"];
    values.output.event.params = {};
    const spec = buildStudy(values, BREAKOUT, STUDY.scope);
    expect(spec.subject.codes).toEqual(["000001.SZ"]);
    expect(spec.output).toMatchObject({ horizons: [5, 20], event: { params: { ma: 250 } } });
  });

  it("写错的数字发出去是 null，由接口报错", () => {
    const values = tableValues(TABLE);
    values.output.limit = "";
    const spec = buildTable(values);
    expect(JSON.parse(JSON.stringify(spec)).output.limit).toBeNull();
  });

  it("日期选择：周末和本地数据之外的日子不能选", () => {
    const disabled = disabledDay("2016-01-04", "2026-09-11");
    expect(disabled(dayjs("2026-09-11"))).toBe(false);
    expect(disabled(dayjs("2026-09-12"))).toBe(true); // 星期六
    expect(disabled(dayjs("2026-09-14"))).toBe(true); // 本地数据之后
    expect(disabled(dayjs("2015-12-31"))).toBe(true);
  });
});

describe("改完再提交：默认值", () => {
  it("原来是默认值、没改的栏目不发，改过的照发；没有默认值清单原样发", () => {
    const values = studyValues(STUDY);
    values.output.cost_bps = 50;
    const edited = buildStudy(values, BREAKOUT, STUDY.scope);
    const sent = dropUntouchedDefaults(edited, STUDY) as unknown as Record<string, unknown>;
    expect(sent.when).toBeUndefined(); // 没改的回看区间让后端再补，确认卡上照样标默认
    expect(sent.output).toEqual({ kind: "event_study", event: { preset_id: "breakout_ma", params: { ma: 250 } }, cost_bps: 50 });
    expect(dropUntouchedDefaults(edited, undefined)).toEqual(edited);
  });

  it("确认卡上用一句话改条件：整份条件和自己比，默认值全去掉，交给大模型的只剩用户说过的", () => {
    const sent = dropUntouchedDefaults(TABLE, TABLE) as unknown as Record<string, unknown>;
    expect(sent.when).toBeUndefined();
    expect(sent.scope).toEqual({ target: "stock", industry: "农林牧渔", board: null });
  });
});

describe("问题 → 输入框", () => {
  const fields = [
    ["when", "as_of"],
    ["when", "range"],
    ["output", "filter", "expr"],
    ["output", "horizons"],
    ["scope", "board"],
    ["output", "event", "params", "ma"],
  ];

  it("路径一样的直接对上", () => {
    expect(fieldForIssue("output.filter.expr", fields)).toEqual(["output", "filter", "expr"]);
    expect(fieldForIssue("output.event.params.ma", fields)).toEqual(["output", "event", "params", "ma"]);
  });

  it("更细的路径对到最近的输入框", () => {
    expect(fieldForIssue("when.range.from", fields)).toEqual(["when", "range"]);
    expect(fieldForIssue("output.horizons.1", fields)).toEqual(["output", "horizons"]);
    expect(fieldForIssue("scope.board.code", fields)).toEqual(["scope", "board"]);
  });

  it("指标按名字报，对到列表里那一行的公式", () => {
    expect(fieldForIssue("metrics.今年以来涨幅", fields, ["成交额", "今年以来涨幅"])).toEqual(["metrics", 1, "expr"]);
    expect(fieldForIssue("metrics.不存在", fields, ["成交额"])).toBeNull();
  });

  it("对不上的放到表单上方", () => {
    const split = splitIssues(
      [
        { path: "output.filter.expr", message: "缺少右括号", allowed: null, position: 7 },
        { path: null, message: "本地股票日频缺 2020-03", allowed: null, position: null },
        { path: "colour", message: "不认识这一项", allowed: null, position: null },
      ],
      fields,
    );
    expect(split.fields).toEqual([{ name: ["output", "filter", "expr"], errors: ["第 8 个字符：缺少右括号"] }]);
    expect(split.other).toEqual(["本地股票日频缺 2020-03", "colour：不认识这一项"]);
  });

  it("说明里没写可选范围的补上", () => {
    expect(issueText({ path: "output.benchmark", message: "对照写错了", allowed: "沪深300、中证500", position: null })).toBe(
      "对照写错了（可选：沪深300、中证500）",
    );
    expect(issueText({ path: "x", message: "可选 1.2~10，收到 30", allowed: "1.2~10", position: null })).toBe(
      "可选 1.2~10，收到 30",
    );
  });
});
