/** 提问流程里的纯逻辑：拼追问的回答、把选中的候选填进查询条件、示例问句。 */

import type { Candidate, Choice, EventInfo, Kind, PlanQuestion, SpecDraft } from "./types";

/** 表、卡的问法；统计的问法用事件库里的示例 */
export const GENERAL_EXAMPLES = [
  "牧原股份最近走势如何？",
  "牧原股份跟温氏股份，今年谁涨得多？",
  "农林牧渔里今年以来涨幅前 10 的股票",
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

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

/**
 * 每组候选选中的那个填进条件：填进「看谁」的加到 subject.codes（原话留着，确认卡上写「「平安」理解为：中国平安」）；
 * 填进「算的范围」的，申万行业填 scope.industry，概念板块填 scope.board（2026-09-15 定）。
 */
export function withPicks(spec: SpecDraft, choices: Choice[], picked: Candidate[]): SpecDraft {
  const subject = record(spec.subject);
  const scope = { ...record(spec.scope) };
  const codes = Array.isArray(subject.codes) ? [...(subject.codes as string[])] : [];
  choices.forEach((choice, index) => {
    const candidate = picked[index];
    if (!candidate) return;
    if (choice.slot === "subject") {
      if (!codes.includes(candidate.code)) codes.push(candidate.code);
    } else if (candidate.board_type === "sw_industry" || candidate.board_type === "sw_industry_l2") {
      scope.industry = candidate.name;
    } else {
      scope.board = { type: "concept", code: candidate.code };
    }
  });
  const next: SpecDraft = { ...spec, scope };
  if (choices.some((choice) => choice.slot === "subject")) {
    next.subject = { ...subject, kind: "codes", codes };
  }
  return next;
}

/** 草稿要出成表、卡还是统计 */
export function kindOf(spec: SpecDraft | null | undefined): Kind | null {
  const kind = record(spec?.output).kind;
  return kind === "table" || kind === "card" || kind === "event_study" ? kind : null;
}

/** 给用户看的示例问句：表、卡各几句，其余用事件库里的示例，去重 */
export function exampleQuestions(events: EventInfo[], limit = 6): string[] {
  const fromEvents = events.map((event) => event.example.question).filter(Boolean);
  return [...new Set([...GENERAL_EXAMPLES, ...fromEvents])].slice(0, limit);
}
