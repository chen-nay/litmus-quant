import { describe, expect, it } from "vitest";

import { GENERAL_EXAMPLES, composeAnswer, exampleQuestions, shapeOf, withBoard, withStock } from "./planFlow";
import type { EventInfo } from "./types";

describe("追问的回答", () => {
  const questions = [
    { question: "「最近」指多长时间？", options: ["最近一周", "最近一个月"] },
    { question: "板块按什么口径？", options: ["申万一级行业", "通达信概念板块"] },
  ];

  it("选了的问题拼成一句，没选的跳过", () => {
    expect(composeAnswer(questions, { 0: "最近一个月", 1: "申万一级行业" }, "")).toBe(
      "「最近」指多长时间？最近一个月；板块按什么口径？申万一级行业",
    );
    expect(composeAnswer(questions, { 1: "通达信概念板块" }, "")).toBe("板块按什么口径？通达信概念板块");
  });

  it("补充的话接在最后，什么都没选也没说就是空的", () => {
    expect(composeAnswer(questions, { 0: "最近一周" }, "  按涨幅排  ")).toBe(
      "「最近」指多长时间？最近一周；按涨幅排",
    );
    expect(composeAnswer(questions, {}, " ")).toBe("");
  });
});

describe("选候选", () => {
  it("选股票只填代码，原话和其他栏目留着", () => {
    const draft = { shape: "stock_history", target: { mention: "平安", guess: "中国平安" }, horizons: [5] };
    expect(withStock(draft, { code: "601318.SH", name: "中国平安", note: "名称包含" })).toEqual({
      shape: "stock_history",
      target: { mention: "平安", guess: "中国平安", code: "601318.SH" },
      horizons: [5],
    });
    expect(withStock({ shape: "stock_history" }, { code: "000001.SZ", name: "平安银行", note: "" })).toEqual({
      shape: "stock_history",
      target: { code: "000001.SZ" },
    });
  });

  it("选概念板块填进股票池，行业等其他限定留着", () => {
    const draft = { shape: "stock_list", universe: { industry: "电子" } };
    expect(withBoard(draft, { code: "880500.TDX", name: "光通信", note: "通达信概念板块" })).toEqual({
      shape: "stock_list",
      universe: { industry: "电子", board: { type: "concept", code: "880500.TDX" } },
    });
  });
});

describe("其他", () => {
  it("认得三种形状", () => {
    expect(shapeOf({ shape: "board_list" })).toBe("board_list");
    expect(shapeOf({ shape: "chart" })).toBeNull();
    expect(shapeOf(null)).toBeNull();
  });

  it("示例问句：先放股票表、板块表的问法，事件库示例去重，限条数", () => {
    const event = (question: string) => ({ example: { question, params: {} } }) as unknown as EventInfo;
    const examples = exampleQuestions([event("茅台突破年线之后怎么走？"), event("茅台突破年线之后怎么走？"), event("")], 6);
    expect(examples).toEqual([...GENERAL_EXAMPLES, "茅台突破年线之后怎么走？"]);
    expect(exampleQuestions([event("a"), event("b"), event("c")], 3)).toHaveLength(3);
  });
});
