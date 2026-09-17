/** 提问区的小零件：等大模型时的提示、可以点的问句。 */

import { Alert, Button, Space, Spin, Typography } from "antd";
import { useEffect, useState } from "react";

/** 大模型带思考，一次十几到几十秒（ARCHITECTURE §5.1）：让用户知道没卡住 */
export function Thinking({ startedAt, what = "你的问题" }: { startedAt: number; what?: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
  return (
    <Alert
      style={{ marginTop: 12 }}
      type="info"
      showIcon
      icon={<Spin size="small" />}
      title={`正在理解${what}，已等 ${seconds} 秒`}
      description="大模型先想再答，通常要十几到几十秒；翻译出来的条件没通过检查时会自动再试一次"
    />
  );
}

/** 一排可以点的问句：点一下就当成新的提问 */
export function Suggestions({
  title,
  items,
  disabled,
  onAsk,
}: {
  title: string;
  items: string[];
  disabled?: boolean;
  onAsk: (text: string) => void;
}) {
  if (!items.length) return null;
  return (
    <div style={{ marginTop: 12 }}>
      <Typography.Text type="secondary">{title}</Typography.Text>
      <Space wrap style={{ marginTop: 6 }}>
        {items.map((item) => (
          <Button key={item} type="dashed" size="small" disabled={disabled} onClick={() => onAsk(item)}>
            {item}
          </Button>
        ))}
      </Space>
    </div>
  );
}
