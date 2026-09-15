/** 提问流程里的纯逻辑：拼追问的回答、把选中的候选填进查询条件、示例问句。 */

import type { Candidate, EventInfo, PlanQuestion, SpecDraft } from "./types";

export type Shape = "stock_list" | "board_list" | "stock_history";

/** 事件库示例之外，股票表、板块表的问法 */
export const GENERAL_EXAMPLES = [
  "昨天涨停的股票里成交额最大的 20 只",
  "最近 5 个交易日涨得最多的申万行业",
];

/** 每个追问选的答案拼成一句话，交给 /api/plan 当回答：「「最近」指多长时间？最近一个月；板块按什么口径？申万一级行业」 */
export function composeAnswer(
  questions: PlanQuestion[],
  chosen: Record<number, string | undefined>,
  extra: string,
): string {
  const parts = questions
    .map((question, index) => (chosen[index] ? `${question.question}${chosen[index]}` : ""))
    .filter(Boolean);
  const more = extra.trim();
  if (more) parts.push(more);
  return parts.join("；");
}

/** 从候选里选了一只股票：只填代码，原话留着（确认卡上写「「平安」理解为：中国平安」） */
export function withStock(spec: SpecDraft, candidate: Candidate): SpecDraft {
  const target = (spec.target as Record<string, unknown> | undefined) ?? {};
  return { ...spec, target: { ...target, code: candidate.code } };
}

export function withBoard(spec: SpecDraft, candidate: Candidate): SpecDraft {
  const universe = (spec.universe as Record<string, unknown> | undefined) ?? {};
  return { ...spec, universe: { ...universe, board: { type: "concept", code: candidate.code } } };
}

export function shapeOf(spec: SpecDraft | null | undefined): Shape | null {
  const shape = spec?.shape;
  return shape === "stock_list" || shape === "board_list" || shape === "stock_history"
    ? shape
    : null;
}

/** 给用户看的示例问句：两句股票表、板块表，其余用事件库里的示例，去重 */
export function exampleQuestions(events: EventInfo[], limit = 6): string[] {
  const fromEvents = events.map((event) => event.example.question).filter(Boolean);
  return [...new Set([...GENERAL_EXAMPLES, ...fromEvents])].slice(0, limit);
}
