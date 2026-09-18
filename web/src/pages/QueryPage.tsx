/**
 * 查询页：用一句话提问，答案出在问题下面。
 *
 * - 卡：提问时就算完了，直接出在问题下面，卡上有分享链接（2026-09-18 定）；小结后到
 * - 表、统计：先出确认卡，确认后运行，跳到结果页。确认卡上改条件有两条路：用一句话说要改哪里
 *   （连同现在的条件一起交给大模型，见 revise），或者点「打开表单」逐栏改
 * - 提问之后也可能是追问、选候选、回答不了——见 PlanOutcome
 *
 * 不提供从空白开始填条件的表单（2026-09-18 定）：表单只用来改现有的条件。
 */

import { App as AntApp, Button, Card, Input, Space, Typography } from "antd";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api, errorText } from "../api";
import { CardView } from "../components/CardView";
import { type Confirm, ConfirmCard } from "../components/ConfirmCard";
import { PlanOutcome } from "../components/PlanOutcome";
import { StudyForm } from "../components/StudyForm";
import { TableForm } from "../components/TableForm";
import { Suggestions, Thinking } from "../components/askParts";
import { useCatalog, useStatus } from "../context";
import { exampleQuestions, kindOf, withPicks } from "../planFlow";
import { dropUntouchedDefaults } from "../specForm";
import type { Candidate, CardResult, CheckResponse, Issue, PlanResponse, Spec } from "../types";

interface Editing {
  spec: Spec;
  planId: string | null;
  issues: Issue[];
  /** 取消修改时回到的确认卡 */
  back: Confirm | null;
  key: number;
}

interface Answered {
  result: CardResult;
  runId: string;
}

export function QueryPage() {
  const { message } = AntApp.useApp();
  const catalog = useCatalog();
  const ready = useStatus().data?.status.ready;
  const examples = exampleQuestions(catalog.events?.events ?? []);

  const [question, setQuestion] = useState("");
  const [thinkingSince, setThinkingSince] = useState<number | null>(null);
  const [checking, setChecking] = useState(false);
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [card, setCard] = useState<Answered | null>(null);
  const [revisingSince, setRevisingSince] = useState<number | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const top = useRef<HTMLDivElement>(null);
  const forms = useRef<HTMLDivElement>(null);
  const busy = thinkingSince !== null || checking || revisingSince !== null;

  function clear() {
    setPlan(null);
    setConfirm(null);
    setCard(null);
    setEditing(null);
  }

  /** 提问、改条件得到的回答：卡直接显示，要确认的出确认卡，其余交给 PlanOutcome */
  function show(response: PlanResponse) {
    if (response.status === "done" && response.result?.kind === "card" && response.run_id) {
      setCard({ result: response.result, runId: response.run_id });
    } else if (response.status === "ok" && response.spec) {
      setConfirm({
        spec: response.spec as unknown as Spec,
        planId: response.plan_id,
        summary: response.summary,
        assumptions: response.assumptions,
      });
    } else {
      setPlan(response);
    }
  }

  async function ask(query: string, previousPlanId?: string | null) {
    const text = query.trim();
    if (!text) return;
    if (!previousPlanId) setQuestion(text);
    setThinkingSince(Date.now());
    clear();
    try {
      show(await api.plan(text, previousPlanId));
    } catch (reason) {
      setPlan(failure(errorText(reason)));
    } finally {
      setThinkingSince(null);
    }
  }

  /**
   * 确认卡上用一句话改条件。和提问不同的是把现在的条件一起发过去，大模型在这份条件上改：
   * 之前从候选里选的、表单上改过的栏目都不会丢（后端 routes/plan.py 第 7 条）。
   * 发之前去掉这次没碰过的默认值，后端重新补一遍，确认卡上照样标「默认」。
   */
  async function revise(change: string) {
    if (!confirm) return;
    setRevisingSince(Date.now());
    setPlan(null);
    try {
      const spec = dropUntouchedDefaults(confirm.spec, confirm.spec);
      const response = await api.revise(spec, change, confirm.planId);
      setConfirm(null);
      show(response);
    } catch (reason) {
      setConfirm(null);
      setPlan(failure(errorText(reason)));
    } finally {
      setRevisingSince(null);
    }
  }

  /** 每组候选都选好了：卡直接算，表和统计先检查、出确认卡 */
  async function pick(picked: Candidate[]) {
    if (!plan?.spec) return;
    const draft = withPicks(plan.spec, plan.choices, picked);
    setChecking(true);
    try {
      if (kindOf(draft) === "card") {
        const response = await api.run(draft as unknown as Spec, plan.plan_id);
        if (response.status === "done" && response.result?.kind === "card" && response.run_id) {
          setPlan(null);
          setCard({ result: response.result, runId: response.run_id });
        } else {
          message.warning(response.message ?? response.issues.map((issue) => issue.message).join("；"));
        }
        return;
      }
      const response = await api.check(draft, plan.plan_id);
      if (response.status === "ok" && response.spec) {
        showConfirm(response, plan.plan_id);
      } else if (response.status === "needs_revision") {
        startEdit(draft as unknown as Spec, plan.plan_id, response.issues);
      } else {
        message.warning(`本地数据还不够：${response.message ?? ""}`);
      }
    } catch (reason) {
      message.error(errorText(reason));
    } finally {
      setChecking(false);
    }
  }

  function showConfirm(response: CheckResponse, planId: string | null) {
    if (!response.spec) return;
    setPlan(null);
    setEditing(null);
    setConfirm({ spec: response.spec, planId, summary: response.summary, assumptions: response.assumptions });
    top.current?.scrollIntoView({ behavior: "smooth" });
  }

  function startEdit(spec: Spec, planId: string | null, issues: Issue[] = []) {
    setEditing({ spec, planId, issues, back: confirm, key: Date.now() });
    setConfirm(null);
    window.setTimeout(() => forms.current?.scrollIntoView({ behavior: "smooth" }), 0);
  }

  function cancelEdit() {
    setConfirm(editing?.back ?? null);
    setEditing(null);
  }

  const formProps = editing && {
    key: editing.key,
    initial: editing.spec,
    planId: editing.planId,
    issues: editing.issues,
    onChecked: (response: CheckResponse) => showConfirm(response, editing.planId),
    onCancel: cancelEdit,
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div ref={top} />
      <Card size="small" title="用一句话提问">
        <Input.TextArea
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          autoSize={{ minRows: 2, maxRows: 4 }}
          maxLength={500}
          placeholder="比如：牧原股份最近走势如何？"
          onPressEnter={(event) => {
            if (event.ctrlKey || event.metaKey) void ask(question);
          }}
        />
        <Space style={{ marginTop: 12 }}>
          <Button
            type="primary"
            loading={thinkingSince !== null}
            disabled={!question.trim() || ready === false || busy}
            onClick={() => ask(question)}
          >
            提问
          </Button>
          <Typography.Text type="secondary">
            {ready === false ? "本地数据还不够，先去同步" : "Ctrl + Enter 也能提问"}
          </Typography.Text>
        </Space>
        {thinkingSince !== null && <Thinking startedAt={thinkingSince} />}
        {thinkingSince === null && !plan && !confirm && !card && !editing && (
          <Suggestions title="试试这些：" items={examples} disabled={ready === false} onAsk={ask} />
        )}
      </Card>

      {card && (
        <CardView
          key={card.runId}
          result={card.result}
          runId={card.runId}
          extra={<Link to={`/runs/${card.runId}`}>分享链接</Link>}
        />
      )}
      {plan && (
        <PlanOutcome
          key={plan.plan_id ?? "none"}
          response={plan}
          busy={busy}
          examples={examples}
          onAnswer={(text) => ask(text, plan.plan_id)}
          onPick={pick}
          onAsk={ask}
          onEdit={() => plan.spec && startEdit(plan.spec as unknown as Spec, plan.plan_id)}
        />
      )}
      {confirm && (
        <ConfirmCard
          key={`${confirm.planId}-${confirm.assumptions.map((item) => item.text).join("|")}`}
          confirm={confirm}
          revisingSince={revisingSince}
          onRevise={revise}
          onEdit={() => startEdit(confirm.spec, confirm.planId)}
          onClose={() => setConfirm(null)}
        />
      )}

      <div ref={forms}>
        {formProps && (
          <Card size="small" title="修改条件（改完点「下一步」，确认卡上的说明跟着变）">
            {editing.spec.output.kind === "event_study" ? <StudyForm {...formProps} /> : <TableForm {...formProps} />}
          </Card>
        )}
      </div>
    </div>
  );
}

function failure(text: string): PlanResponse {
  return {
    status: "failed",
    plan_id: null,
    spec: null,
    summary: "",
    assumptions: [],
    questions: [],
    choices: [],
    alternatives: [],
    message: text,
    data: null,
    run_id: null,
    result: null,
  };
}
