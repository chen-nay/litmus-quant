/** 表的结果：前几列是代码、名称（股票还有行业），后面每个指标一列；单位和显示方式随结果给出（format.ts）。 */

import { Alert, Card, Table, type TableColumnsType } from "antd";

import { cellColor, columnTitle, compareCells, formatCell } from "../format";
import type { Cell, Row, TableResult, Target } from "../types";

type TableColumn = TableColumnsType<Row>[number];

const HEAD: Record<string, { title: string; width: number }> = {
  code: { title: "代码", width: 110 },
  name: { title: "名称", width: 120 },
  industry: { title: "行业", width: 100 },
};

export function TableResultView({ result, target }: { result: TableResult; target: Target }) {
  const board = target !== "stock";
  const unit = board ? "个" : "只";

  const head = result.head.map(
    (name): TableColumn => ({
      title: HEAD[name]?.title ?? name,
      dataIndex: name,
      width: HEAD[name]?.width,
      fixed: name === "code" ? "left" : undefined,
      sorter: (a: Row, b: Row) => compareCells(a[name], b[name]),
    }),
  );
  const metrics = result.columns.map(
    (column): TableColumn => ({
      title: columnTitle(column, board),
      dataIndex: column.name,
      align: "right",
      sorter: (a: Row, b: Row) => compareCells(a[column.name], b[column.name]),
      render: (value: Cell) => (
        <span style={{ color: cellColor(value, column) }}>{formatCell(value, column)}</span>
      ),
    }),
  );

  return (
    <Card size="small" title={`共 ${result.total} ${unit}满足条件，按排序取前 ${result.rows.length} ${unit}`}>
      {result.notes.length > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          title={result.notes.length === 1 ? result.notes[0] : "提示"}
          description={result.notes.length > 1 ? result.notes.map((note) => <div key={note}>{note}</div>) : undefined}
        />
      )}
      <Table<Row>
        size="small"
        rowKey="code"
        columns={[...head, ...metrics]}
        dataSource={result.rows}
        pagination={{ pageSize: 50, hideOnSinglePage: true }}
        scroll={{ x: "max-content" }}
      />
    </Card>
  );
}
