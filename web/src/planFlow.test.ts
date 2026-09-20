import { describe, expect, it } from "vitest";

import { GENERAL_EXAMPLES, composeAnswer, exampleQuestions, kindOf, withPicks } from "./planFlow";
import type { Choice, EventInfo } from "./types";

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
  const stock = (code: string, name: string) => ({ code, name, note: "名称包含" });
  const choice = (slot: Choice["slot"], mention: string, candidates: Choice["candidates"]): Choice => ({
    slot,
    mention,
    message: "",
    candidates,
  });

  it("点名的：选中的代码加进看谁，查准的、原话、其他栏目都留着", () => {
    const draft = {
      subject: { kind: "codes", codes: ["002714.SZ"], mentions: [{ mention: "牧原", guess: "牧原股份" }] },
      output: { kind: "card" },
    };
    const choices = [choice("subject", "平安", [stock("601318.SH", "中国平安"), stock("000001.SZ", "平安银行")])];
    expect(withPicks(draft, choices, [stock("601318.SH", "中国平安")])).toEqual({
      subject: {
        kind: "codes",
        codes: ["002714.SZ", "601318.SH"],
        mentions: [{ mention: "牧原", guess: "牧原股份" }],
      },
      scope: {},
      output: { kind: "card" },
    });
  });

  it("几组一起选：一组一个，都填进去", () => {
    const choices = [
      choice("subject", "平安", [stock("601318.SH", "中国平安")]),
      choice("subject", "中国", [stock("601988.SH", "中国银行")]),
    ];
    const next = withPicks({ output: { kind: "card" } }, choices, [stock("601318.SH", "中国平安"), stock("601988.SH", "中国银行")]);
    expect((next.subject as { codes: string[] }).codes).toEqual(["601318.SH", "601988.SH"]);
  });

  it("算的范围：申万行业填行业，概念板块填板块，已有的限定留着", () => {
    const draft = { scope: { base: "hs300" }, output: { kind: "table" } };
    const concept = { code: "880500.TDX", name: "光通信", note: "通达信概念板块", board_type: "concept" as const };
    const industry = { code: "801081.SI", name: "半导体", note: "申万二级行业", board_type: "sw_industry_l2" as const };
    const choices = [choice("scope", "光模块", [concept, industry])];
    expect(withPicks(draft, choices, [concept])).toEqual({
      scope: { base: "hs300", board: { type: "concept", code: "880500.TDX" } },
      output: { kind: "table" },
    });
    expect(withPicks(draft, choices, [industry])).toEqual({
      scope: { base: "hs300", industry: "半导体" },
      output: { kind: "table" },
    });
  });
});

describe("其他", () => {
  it("认得三种回答", () => {
    expect(kindOf({ output: { kind: "card" } })).toBe("card");
    expect(kindOf({ output: { kind: "chart" } })).toBeNull();
    expect(kindOf(null)).toBeNull();
  });

  it("示例问句：先放表、卡的问法，事件库示例去重，限条数", () => {
    const event = (question: string) => ({ example: { question, params: {} } }) as unknown as EventInfo;
    const examples = exampleQuestions([event("茅台突破年线之后怎么走？"), event("茅台突破年线之后怎么走？"), event("")], 8);
    expect(examples).toEqual([...GENERAL_EXAMPLES, "茅台突破年线之后怎么走？"]);
    expect(exampleQuestions([event("a"), event("b"), event("c")], 3)).toHaveLength(3);
  });
});
