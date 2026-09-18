/**
 * 按部分分组的说明：确认卡、结果页上方、卡底下的「怎么算的」共用。
 * 文字全部由后端按查询条件生成（litmus/spec/confirm.py），这里只排版：左边小标题（看谁 / 看哪天 / 看哪些数 / 怎么出 /
 * 什么事件 / 怎么算），右边一行一条；默认值后面标「默认」；不属于任何一组的（本地数据截至）放在最后。
 */

import { Tag, Typography } from "antd";

import type { AssumptionItem } from "../types";

interface Group {
  title: string;
  items: AssumptionItem[];
}

/** 连着的同一组并在一起，顺序照后端给的 */
export function groupAssumptions(items: AssumptionItem[]): Group[] {
  const groups: Group[] = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (last && last.title === item.group) last.items.push(item);
    else groups.push({ title: item.group, items: [item] });
  }
  return groups;
}

export function Assumptions({ items, small = false }: { items: AssumptionItem[]; small?: boolean }) {
  const groups = groupAssumptions(items);
  const size = small ? 13 : 14;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "5em 1fr", rowGap: small ? 2 : 6, fontSize: size }}>
      {groups.map((group, index) => (
        <Row key={`${index}-${group.title}`} group={group} small={small} />
      ))}
    </div>
  );
}

function Row({ group, small }: { group: Group; small: boolean }) {
  // 不属于任何一组的（本地数据截至）占满一行，和上面的分开
  if (!group.title) {
    return (
      <div style={{ gridColumn: "1 / -1", marginTop: small ? 4 : 8 }}>
        {group.items.map((item, index) => (
          <Typography.Text key={`${index}-${item.text}`} type="secondary" style={{ fontSize: "inherit", display: "block" }}>
            {item.text}
          </Typography.Text>
        ))}
      </div>
    );
  }
  const lines = group.items.map((item, index) => (
    <div key={`${index}-${item.text}`}>
      <Typography.Text type={small ? "secondary" : undefined} style={{ fontSize: "inherit" }}>
        {item.text}
      </Typography.Text>
      {item.default && (
        <Tag color="orange" style={{ marginLeft: 8, fontSize: 12, lineHeight: "18px" }}>
          默认
        </Tag>
      )}
    </div>
  ));
  return (
    <>
      <Typography.Text type="secondary" style={{ fontSize: "inherit" }}>
        {group.title}
      </Typography.Text>
      <div>{lines}</div>
    </>
  );
}
