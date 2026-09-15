/** 股票表、板块表的结果：列名和单位来自 /api/fields，数字格式见 format.ts。 */

import { Alert, Card, Table, Tooltip, type TableColumnsType } from "antd";

import { useCatalog } from "../context";
import {
  cellColor,
  columnTitle,
  compareCells,
  fieldMap,
  formatCell,
  sortColumn,
  visibleColumns,
} from "../format";
import type { BoardListSpec, Cell, ListResult, Row, StockListSpec, Target } from "../types";

type Column = TableColumnsType<Row>[number];

const TEXT_COLUMNS: Record<string, { title: string; width: number }> = {
  code: { title: "代码", width: 110 },
  name: { title: "名称", width: 120 },
  industry: { title: "行业", width: 100 },
};

export function ListResultView({
  result,
  spec,
}: {
  result: ListResult;
  spec: StockListSpec | BoardListSpec;
}) {
  const { fields } = useCatalog();
  const target: Target = spec.shape === "board_list" ? spec.board_type : "stock";
  const board = target !== "stock";
  const metas = fieldMap(fields?.fields[target]);
  const sortMeta = sortColumn(spec.sort, metas);
  const unit = board ? "个" : "只";

  const columns = visibleColumns(result.columns, sortMeta).map((name): Column => {
    const sorter = (a: Row, b: Row) => compareCells(a[name], b[name]);
    const text = TEXT_COLUMNS[name];
    if (text) {
      return {
        title: text.title,
        dataIndex: name,
        width: text.width,
        fixed: name === "code" ? "left" : undefined,
        sorter,
      };
    }
    const isSort = name === "sort_value";
    const meta = isSort ? sortMeta : (metas.get(name) ?? { name, label: name, unit: "" });
    return {
      title: isSort ? (
        <Tooltip title={`排序依据：${spec.sort?.by ?? "$amount"}`}>{sortMeta.label}</Tooltip>
      ) : (
        columnTitle(meta, board)
      ),
      dataIndex: name,
      align: "right",
      sorter,
      render: (value: Cell) => (
        <span style={{ color: cellColor(meta.name, value, meta.unit) }}>{formatCell(meta.name, value, meta.unit)}</span>
      ),
    };
  });

  return (
    <Card
      size="small"
      title={`共 ${result.total} ${unit}满足条件，按排序取前 ${result.rows.length} ${unit}`}
    >
      {result.notes.length > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          title={result.notes.length === 1 ? result.notes[0] : "提示"}
          description={
            result.notes.length > 1 ? result.notes.map((note) => <div key={note}>{note}</div>) : undefined
          }
        />
      )}
      <Table<Row>
        size="small"
        rowKey="code"
        columns={columns}
        dataSource={result.rows}
        pagination={{ pageSize: 50, hideOnSinglePage: true }}
        scroll={{ x: "max-content" }}
      />
    </Card>
  );
}
