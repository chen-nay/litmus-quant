/**
 * 卡（DESIGN.md §1.5）：点名看的一个或几个标的，一个指标一行，下面跟一行解释。
 *
 * - 只点名一只：竖着排，指标名在左、值在右，解释行在下面
 * - 点名几只：并排对比，一个指标一行、一只一列（2026-09-18 定）
 * - 小结：卡先出，要写小结的再调接口取，等的时候写「正在根据上面的数写小结…」；是空的就不显示这一块
 * - 怎么算的：卡不走确认卡，口径放在最下面，说法和确认卡同一份
 */

import { Alert, Card, Spin, Typography } from "antd";
import { type ReactNode, useEffect, useState } from "react";

import { api } from "../api";
import { trendColor } from "../format";
import type { CardItem, CardResult, CardRow } from "../types";
import { Assumptions } from "./Assumptions";

const RULE = "1px solid #f0f0f0";

/** 带正负号的值上色：涨跌、同比。排名、分位、倍数不带符号，不上色 */
function valueColor(row: CardRow): string | undefined {
  if (typeof row.value !== "number") return undefined;
  return /^[+-]\d/.test(row.text) ? trendColor(row.value) : undefined;
}

function Value({ row }: { row: CardRow }) {
  return (
    <Typography.Text strong style={{ color: valueColor(row), fontSize: 16 }}>
      {row.text}
    </Typography.Text>
  );
}

function Note({ text }: { text: string }) {
  if (!text) return null;
  return (
    <div style={{ color: "#8c8c8c", fontSize: 13, marginTop: 2 }}>
      <span style={{ marginRight: 4 }}>└</span>
      {text}
    </div>
  );
}

function Title({ item }: { item: CardItem }) {
  return (
    <span>
      {item.name ?? item.code}
      <Typography.Text type="secondary" style={{ marginLeft: 8, fontWeight: "normal" }}>
        {item.code}
      </Typography.Text>
    </span>
  );
}

function Single({ item, extra }: { item: CardItem; extra?: ReactNode }) {
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 16, paddingBottom: 8, borderBottom: RULE }}>
        <Typography.Text strong style={{ fontSize: 16 }}>
          <Title item={item} />
        </Typography.Text>
        <span style={{ display: "flex", gap: 16 }}>
          {item.industry && <Typography.Text type="secondary">{item.industry}</Typography.Text>}
          {extra}
        </span>
      </div>
      {item.rows.map((row) => (
        <div key={row.name} style={{ padding: "8px 0", borderBottom: RULE }}>
          <div style={{ display: "flex", gap: 24 }}>
            <span style={{ minWidth: 140 }}>{row.name}</span>
            <Value row={row} />
          </div>
          <Note text={row.note} />
        </div>
      ))}
    </div>
  );
}

/** 并排对比：一个指标一行、一只一列 */
function Compare({ items, extra }: { items: CardItem[]; extra?: ReactNode }) {
  const names = items[0]?.rows.map((row) => row.name) ?? [];
  const cell = { padding: "8px 12px", borderBottom: RULE, verticalAlign: "top" } as const;
  return (
    <div style={{ overflowX: "auto" }}>
      {extra && <div style={{ textAlign: "right" }}>{extra}</div>}
      <table style={{ borderCollapse: "collapse", width: "100%" }}>
        <thead>
          <tr>
            <th style={{ ...cell, textAlign: "left", width: 140 }} />
            {items.map((item) => (
              <th key={item.code} style={{ ...cell, textAlign: "left" }}>
                <Title item={item} />
                {item.industry && (
                  <div style={{ color: "#8c8c8c", fontWeight: "normal", fontSize: 12 }}>{item.industry}</div>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {names.map((name, index) => (
            <tr key={name}>
              <td style={cell}>{name}</td>
              {items.map((item) => {
                const row = item.rows[index];
                return (
                  <td key={item.code} style={cell}>
                    {row && <Value row={row} />}
                    {row && <Note text={row.note} />}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 小结：已经写过的直接给；要写而还没写的，调接口取 */
function Narrative({ runId, initial }: { runId: string; initial: string | null | undefined }) {
  const [text, setText] = useState<string | null>(initial ?? null);

  useEffect(() => {
    if (initial !== null && initial !== undefined) return;
    let alive = true;
    api.narrative(runId).then(
      (response) => alive && setText(response.text),
      () => alive && setText(""),
    );
    return () => {
      alive = false;
    };
  }, [runId, initial]);

  if (text === "") return null;
  return (
    <div style={{ marginTop: 16, paddingTop: 12, borderTop: RULE }}>
      <Typography.Text strong>小结</Typography.Text>
      <Typography.Text type="secondary"> · AI 根据上面的数写</Typography.Text>
      <div style={{ marginTop: 6, lineHeight: 1.8 }}>
        {text === null ? (
          <Typography.Text type="secondary">
            <Spin size="small" style={{ marginRight: 8 }} />
            正在根据上面的数写小结…
          </Typography.Text>
        ) : (
          text
        )}
      </div>
    </div>
  );
}

export function CardView({
  result,
  runId,
  narrative,
  extra,
}: {
  result: CardResult;
  runId: string;
  /** 结果页从运行记录里带来的小结；提问页刚算完的没有，由卡自己去取 */
  narrative?: string | null;
  /** 放在标题那一行右边：提问页上的「分享链接」 */
  extra?: ReactNode;
}) {
  return (
    <Card size="small">
      {result.notes.length > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          title={result.notes.length === 1 ? result.notes[0] : "提示"}
          description={result.notes.length > 1 ? result.notes.map((note) => <div key={note}>{note}</div>) : undefined}
        />
      )}
      {result.items.length === 1 ? (
        <Single item={result.items[0]} extra={extra} />
      ) : (
        <Compare items={result.items} extra={extra} />
      )}
      {result.narrate && <Narrative runId={runId} initial={narrative} />}
      <div style={{ marginTop: 16, paddingTop: 12, borderTop: RULE }}>
        <Typography.Text type="secondary" strong style={{ fontSize: 13 }}>
          怎么算的
        </Typography.Text>
        <div style={{ marginTop: 6 }}>
          <Assumptions items={result.assumptions} small />
        </div>
      </div>
    </Card>
  );
}
