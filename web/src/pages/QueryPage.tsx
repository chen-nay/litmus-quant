/** 查询页：三种查询各一张表单。第 7 步之前直接填条件；确认卡上的「修改」会复用这几张表单。 */

import { Card, Tabs } from "antd";

import { BoardListForm } from "../components/BoardListForm";
import { HistoryForm } from "../components/HistoryForm";
import { StockListForm } from "../components/StockListForm";

export function QueryPage() {
  return (
    <Card size="small">
      <Tabs
        items={[
          { key: "stock_list", label: "股票表", children: <StockListForm /> },
          { key: "board_list", label: "板块表", children: <BoardListForm /> },
          { key: "stock_history", label: "个股回看", children: <HistoryForm /> },
        ]}
      />
    </Card>
  );
}
