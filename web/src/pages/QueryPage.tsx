/**
 * 查询页（ARCHITECTURE §1.4、§5）：两步出结果。
 * 1. 用一句话提问（或者直接填条件）
 * 2. 确认卡把条件翻译成明确的定义；确认后才运行，点「修改」回到表单，改完说明文字跟着变
 *
 * 提问之后也可能是追问、选候选、回答不了——见 PlanOutcome。
 */

import { App as AntApp, Button, Card, Input, Space, Tabs, Typography } from "antd";
import { useRef, useState } from "react";

import { api, errorText } from "../api";
import { BoardListForm } from "../components/BoardListForm";
import { type Confirm, ConfirmCard } from "../components/ConfirmCard";
import { HistoryForm } from "../components/HistoryForm";
import { PlanOutcome } from "../components/PlanOutcome";
import { StockListForm } from "../components/StockListForm";
import { Suggestions, Thinking } from "../components/askParts";
import { useCatalog, useStatus } from "../context";
import { type Shape, exampleQuestions, shapeOf, withBoard, withStock } from "../planFlow";
import type {
  BoardListSpec,
  Candidate,
  CheckResponse,
  Issue,
  PlanResponse,
  SpecDraft,
  StockHistorySpec,
  StockListSpec,
} from "../types";

interface Editing {
  spec: SpecDraft;
  planId: string | null;
  issues: Issue[];
  /** 取消修改时回到的确认卡 */
  back: Confirm | null;
  key: number;
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
  const [editing, setEditing] = useState<Editing | null>(null);
  const [tab, setTab] = useState<Shape>("stock_list");
  const top = useRef<HTMLDivElement>(null);
  const forms = useRef<HTMLDivElement>(null);
  const busy = thinkingSince !== null || checking;

  async function ask(query: string, previousPlanId?: string | null) {
    const text = query.trim();
    if (!text) return;
    if (!previousPlanId) setQuestion(text);
    setThinkingSince(Date.now());
    setPlan(null);
    setConfirm(null);
    setEditing(null);
    try {
      const response = await api.plan(text, previousPlanId);
      if (response.status === "ok" && response.spec) {
        setConfirm({
          spec: response.spec as unknown as Confirm["spec"],
          planId: response.plan_id,
          assumptions: response.assumptions,
        });
      } else {
        setPlan(response);
      }
    } catch (reason) {
      setPlan(failure(errorText(reason)));
    } finally {
      setThinkingSince(null);
    }
  }

  async function pick(kind: "stock" | "board", candidate: Candidate) {
    if (!plan?.spec) return;
    const draft = kind === "stock" ? withStock(plan.spec, candidate) : withBoard(plan.spec, candidate);
    setChecking(true);
    try {
      const response = await api.check(draft, plan.plan_id);
      if (response.status === "ok" && response.spec) {
        showConfirm(response, plan.plan_id);
      } else if (response.status === "needs_revision") {
        startEdit(draft, plan.plan_id, response.issues);
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
    setConfirm({ spec: response.spec, planId, assumptions: response.assumptions });
    top.current?.scrollIntoView({ behavior: "smooth" });
  }

  function startEdit(spec: SpecDraft, planId: string | null, issues: Issue[] = []) {
    setTab(shapeOf(spec) ?? "stock_list");
    setEditing({ spec, planId, issues, back: confirm, key: Date.now() });
    setConfirm(null);
    window.setTimeout(() => forms.current?.scrollIntoView({ behavior: "smooth" }), 0);
  }

  function cancelEdit() {
    setConfirm(editing?.back ?? null);
    setEditing(null);
  }

  /** 表单预填：只给当前在改的那种形状 */
  function initialFor<S>(shape: Shape): Partial<S> | undefined {
    return editing && shapeOf(editing.spec) === shape ? (editing.spec as Partial<S>) : undefined;
  }

  const formProps = (shape: Shape) => ({
    planId: editing?.planId ?? null,
    issues: editing && shapeOf(editing.spec) === shape ? editing.issues : undefined,
    onChecked: (response: CheckResponse) => showConfirm(response, editing?.planId ?? null),
    onCancel: editing ? cancelEdit : undefined,
  });

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div ref={top} />
      <Card size="small" title="用一句话提问">
        <Input.TextArea
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          autoSize={{ minRows: 2, maxRows: 4 }}
          maxLength={500}
          placeholder="比如：茅台每次放量突破年线之后表现怎么样？"
          onPressEnter={(event) => {
            if (event.ctrlKey || event.metaKey) void ask(question);
          }}
        />
        <Space style={{ marginTop: 12 }}>
          <Button
            type="primary"
            loading={thinkingSince !== null}
            disabled={!question.trim() || ready === false || checking}
            onClick={() => ask(question)}
          >
            提问
          </Button>
          <Typography.Text type="secondary">
            {ready === false ? "本地数据还不够，先去同步" : "Ctrl + Enter 也能提问"}
          </Typography.Text>
        </Space>
        {thinkingSince !== null && <Thinking startedAt={thinkingSince} />}
        {thinkingSince === null && !plan && !confirm && !editing && (
          <Suggestions title="试试这些：" items={examples} disabled={ready === false} onAsk={ask} />
        )}
      </Card>

      {plan && (
        <PlanOutcome
          key={plan.plan_id ?? "none"}
          response={plan}
          busy={busy}
          examples={examples}
          onAnswer={(text) => ask(text, plan.plan_id)}
          onPick={pick}
          onAsk={ask}
          onEdit={() => startEdit(plan.spec ?? {}, plan.plan_id)}
        />
      )}
      {confirm && (
        <ConfirmCard
          key={`${confirm.planId}-${confirm.assumptions.map((item) => item.text).join("|")}`}
          confirm={confirm}
          onEdit={() => startEdit(confirm.spec as unknown as SpecDraft, confirm.planId)}
          onClose={() => setConfirm(null)}
        />
      )}

      <div ref={forms}>
        <Card size="small" title={editing ? "修改条件（改完点「下一步」，确认卡上的说明跟着变）" : "或者直接填条件"}>
          <Tabs
            activeKey={tab}
            onChange={(key) => setTab(key as Shape)}
            items={[
              {
                key: "stock_list",
                label: "股票表",
                children: (
                  <StockListForm
                    key={editing?.key ?? 0}
                    initial={initialFor<StockListSpec>("stock_list")}
                    {...formProps("stock_list")}
                  />
                ),
              },
              {
                key: "board_list",
                label: "板块表",
                children: (
                  <BoardListForm
                    key={editing?.key ?? 0}
                    initial={initialFor<BoardListSpec>("board_list")}
                    {...formProps("board_list")}
                  />
                ),
              },
              {
                key: "stock_history",
                label: "个股回看",
                children: (
                  <HistoryForm
                    key={editing?.key ?? 0}
                    initial={initialFor<StockHistorySpec>("stock_history")}
                    {...formProps("stock_history")}
                  />
                ),
              },
            ]}
          />
        </Card>
      </div>
    </div>
  );
}

function failure(text: string): PlanResponse {
  return {
    status: "failed",
    plan_id: null,
    spec: null,
    assumptions: [],
    questions: [],
    stock_candidates: [],
    board_candidates: [],
    alternatives: [],
    message: text,
    data: null,
  };
}
