/** 提交查询：算完跳到结果页；要改的问题标到对应输入框，对不上的列在表单上方。 */

import { Alert, type FormInstance } from "antd";
import { useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, errorText } from "../api";
import { type FieldName, splitIssues } from "../specForm";
import type { Spec } from "../types";

interface Problem {
  type: "warning" | "error";
  title: string;
  lines: string[];
  syncLink?: boolean;
}

export function useRunner(
  form: FormInstance,
  fields: FieldName[],
): { running: boolean; submit: (spec: Spec) => Promise<void>; alert: ReactNode } {
  const navigate = useNavigate();
  const [running, setRunning] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);

  async function submit(spec: Spec) {
    setRunning(true);
    setProblem(null);
    form.setFields(fields.map((name) => ({ name, errors: [] })));
    try {
      const response = await api.run(spec);
      if (response.status === "done" && response.run_id) {
        navigate(`/runs/${response.run_id}`);
        return;
      }
      if (response.status === "needs_revision") {
        const split = splitIssues(response.issues, fields);
        form.setFields(split.fields);
        setProblem({
          type: "warning",
          title: split.fields.length ? "条件要改一下，说明在标红的输入框下面" : "条件要改一下",
          lines: split.other,
        });
      } else if (response.status === "data_not_ready") {
        setProblem({
          type: "warning",
          title: `本地数据还不够：${response.message ?? ""}`,
          lines: [],
          syncLink: true,
        });
      } else {
        setProblem({ type: "error", title: response.message ?? "计算时出错了", lines: [] });
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
