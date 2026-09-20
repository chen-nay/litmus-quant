/** K 线图：前复权，标出触发日，可高亮一笔买卖。配置见 kline.ts。 */

import { Alert } from "antd";
import { BarChart, CandlestickChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  MarkAreaComponent,
  MarkPointComponent,
  TitleComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef, useState } from "react";

import { api, errorText } from "../api";
import { type Highlight, buildKlineOption } from "../kline";
import type { KlineResponse } from "../types";

// 只注册用到的图表和组件，打包小很多
echarts.use([
  BarChart,
  CandlestickChart,
  DataZoomComponent,
  GridComponent,
  MarkAreaComponent,
  MarkPointComponent,
  TitleComponent,
  TooltipComponent,
  CanvasRenderer,
]);

interface Props {
  code: string;
  from: string;
  to: string;
  triggers: string[];
  highlight: Highlight | null;
}

export function KlineChart({ code, from, to, triggers, highlight }: Props) {
  const holder = useRef<HTMLDivElement>(null);
  const chart = useRef<ReturnType<typeof echarts.init> | null>(null);
  const [data, setData] = useState<KlineResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    api.kline(code, from, to).then(
      (value) => {
        if (alive) setData(value);
      },
      (reason) => {
        if (alive) setError(errorText(reason));
      },
    );
    return () => {
      alive = false;
    };
  }, [code, from, to]);

  useEffect(() => {
    if (!holder.current) return;
    const instance = echarts.init(holder.current);
    chart.current = instance;
    const resize = () => instance.resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      instance.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    // 合并更新：换高亮时保留用户拖动过的缩放范围
    if (chart.current && data) chart.current.setOption(buildKlineOption(data, triggers, highlight));
  }, [data, triggers, highlight]);

  return (
    <>
      {error && <Alert type="error" showIcon style={{ marginBottom: 8 }} title={`K 线加载失败：${error}`} />}
      <div ref={holder} style={{ width: "100%", height: 460 }} />
    </>
  );
}
