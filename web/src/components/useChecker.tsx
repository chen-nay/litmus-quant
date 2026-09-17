/** 表单提交：先检查、不计算（/api/check），通过了交给确认卡；要改的问题标到对应输入框，对不上的列在表单上方。 */

import { Alert, type FormInstance } from "antd";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, errorText } from "../api";
import { type FieldName, dropUntouchedDefaults, splitIssues } from "../specForm";
import type { CheckResponse, Issue, Spec, SpecMeta } from "../types";

interface Problem {
  type: "warning" | "error";
  title: string;
  lines: string[];
  syncLink?: boolean;
}

export interface CheckerOptions {
  /** 在改的查询条件：原来是默认值、这次没改的栏目不发，确认卡上照样标「默认值」 */
  initial?: SpecMeta;
  /** 提问记录编号：从确认卡点「修改」进来的带上，没改过的栏目继续用提问原话的说法 */
  planId?: string | null;
  /** 打开表单时就要标出来的问题（草稿检查没通过） */
  issues?: Issue[];
  onChecked: (response: CheckResponse) => void;
}

export function useChecker(
  form: FormInstance,
  fields: FieldName[],
  { initial, planId, issues, onChecked }: CheckerOptions,
): { running: boolean; submit: (spec: Spec) => Promise<void>; alert: ReactNode } {
  const [running, setRunning] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);

  const showIssues = useCallback(
    (list: Issue[]) => {
      const split = splitIssues(list, fields);
      form.setFields(split.fields);
      setProblem({
        type: "warning",
        title: split.fields.length ? "条件要改一下，说明在标红的输入框下面" : "条件要改一下",
        lines: split.other,
      });
    },
    [form, fields],
  );

  useEffect(() => {
    if (issues?.length) showIssues(issues);
  }, [issues, showIssues]);

  async function submit(spec: Spec) {
    setRunning(true);
    setProblem(null);
    form.setFields(fields.map((name) => ({ name, errors: [] })));
    try {
      const response = await api.check(dropUntouchedDefaults(spec, initial), planId);
      if (response.status === "ok") {
        onChecked(response);
      } else if (response.status === "needs_revision") {
        showIssues(response.issues);
      } else {
        setProblem({
          type: "warning",
          title: `本地数据还不够：${response.message ?? ""}`,
          lines: [],
          syncLink: true,
        });
      }
    } catch (reason) {
      setProblem({ type: "error", title: errorText(reason), lines: [] });
    } finally {
      setRunning(false);
    }
  }

  const alert = problem && (
    <Alert
      style={{ marginBottom: 16 }}
      showIcon
      type={problem.type}
      title={problem.title}
      description={
        problem.lines.length || problem.syncLink ? (
          <>
            {problem.lines.map((line) => (
              <div key={line}>{line}</div>
            ))}
            {problem.syncLink && <Link to="/sync">去同步页看看</Link>}
          </>
        ) : undefined
      }
    />
  );

  return { running, submit, alert };
}
