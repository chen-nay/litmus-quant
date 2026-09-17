/**
 * 技术细节：这次提问和运行的每一步调了什么、返回了什么、花了多久（过程记录，ARCHITECTURE §7）。
 *
 * 由 .env 的 LITMUS_SHOW_TRACE 控制显不显示。取不到就不显示——旧的运行记录还没有过程记录，
 * 这不算出错，不弹报错框。
 */

import { useEffect, useState } from "react";

import { api } from "../api";
import type { TraceResponse } from "../types";

/** 这几项单独排版：原始返回和长文本折行显示，其余并排成一行 */
const BLOCKS = new Set(["raw_reply", "user_message"]);

const MONO = "ui-monospace, SFMono-Regular, Menlo, monospace";

function text(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}

function Step({ step }: { step: Record<string, unknown> }) {
  const name = String(step.step ?? "?");
  const entries = Object.entries(step).filter(([key, value]) => key !== "step" && value !== null);
  const inline = entries.filter(([key]) => !BLOCKS.has(key));
  const blocks = entries.filter(([key]) => BLOCKS.has(key));
  return (
    <li style={{ marginBottom: 10 }}>
      <code style={{ fontFamily: MONO, fontWeight: 600 }}>{name}</code>
      <div style={{ color: "#666", fontSize: 12, marginTop: 2 }}>
        {inline.map(([key, value]) => (
          <span key={key} style={{ marginRight: 12, whiteSpace: "nowrap" }}>
            {key}=<span style={{ color: "#111" }}>{text(value).slice(0, 120)}</span>
          </span>
        ))}
      </div>
      {blocks.map(([key, value]) => (
        <details key={key} style={{ marginTop: 4 }}>
          <summary style={{ cursor: "pointer", fontSize: 12, color: "#1677ff" }}>{key}</summary>
          <pre
            style={{
              fontFamily: MONO,
              fontSize: 12,
              background: "#fafafa",
              padding: 8,
              margin: "4px 0 0",
              maxHeight: 300,
              overflow: "auto",
            }}
          >
            {text(value)}
          </pre>
        </details>
      ))}
    </li>
  );
}

function Chain({ title, recordId }: { title: string; recordId: string }) {
  const [trace, setTrace] = useState<TraceResponse | null>(null);

  useEffect(() => {
    let alive = true;
    setTrace(null);
    // 没记到过程记录（旧记录、或者当时存失败了）就不显示，不当成错误
    api.trace(recordId).then(
      (value) => alive && setTrace(value),
      () => {},
    );
    return () => {
      alive = false;
    };
  }, [recordId]);

  if (!trace) return null;
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>
        {title}
        <code style={{ fontFamily: MONO, fontWeight: 400, color: "#888", marginLeft: 8 }}>
          {trace.record_id}
        </code>
      </div>
      <ol style={{ paddingLeft: 20, margin: 0 }}>
        {trace.steps.map((step, index) => (
          <Step key={`${index}-${String(step.step)}`} step={step} />
        ))}
      </ol>
    </div>
  );
}

export function TraceView({ runId, planId }: { runId: string; planId?: string | null }) {
  return (
    <details style={{ marginTop: 8 }}>
      <summary style={{ cursor: "pointer", color: "#1677ff" }}>技术细节</summary>
      <Chain title="运行" recordId={runId} />
      {planId && <Chain title="提问" recordId={planId} />}
    </details>
  );
}
