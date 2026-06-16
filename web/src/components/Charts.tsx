import { useEffect, useRef } from "react";
import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  createChart,
  type IChartApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Bar, EquityPoint } from "../lib/types";

const THEME = {
  layout: {
    background: { type: ColorType.Solid, color: "transparent" },
    textColor: "#9fb0c3",
    fontFamily: "Geist Mono, JetBrains Mono, ui-monospace, monospace",
  },
  grid: {
    vertLines: { color: "rgba(150,170,200,0.06)" },
    horzLines: { color: "rgba(150,170,200,0.06)" },
  },
  rightPriceScale: { borderColor: "rgba(150,170,200,0.12)" },
  timeScale: { borderColor: "rgba(150,170,200,0.12)" },
  crosshair: { vertLine: { labelVisible: false }, horzLine: { labelVisible: true } },
};

function useChart(build: (chart: IChartApi) => void, deps: unknown[]) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const chart = createChart(el, { ...THEME, height: el.clientHeight || 220, autoSize: true });
    build(chart);
    chart.timeScale().fitContent();
    const ro = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }));
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return ref;
}

// Daily candlesticks. Gold is identity here, so candles use the semantic
// long/short colors (up green, down red), never gold.
export function PriceChart({ bars }: { bars: Bar[] }) {
  const data = bars
    .filter((b) => b.open != null && b.high != null && b.low != null && b.close != null)
    .map((b) => ({
      time: b.time as unknown as UTCTimestamp,
      open: b.open as number,
      high: b.high as number,
      low: b.low as number,
      close: b.close as number,
    }));
  const ref = useChart((chart) => {
    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#3fb68b",
      downColor: "#e5484d",
      borderUpColor: "#3fb68b",
      borderDownColor: "#e5484d",
      wickUpColor: "#3fb68b",
      wickDownColor: "#e5484d",
    });
    series.setData(data);
  }, [JSON.stringify(data.map((d) => [d.time, d.close]))]);
  return <div className="chart" ref={ref} />;
}

// Session equity, drawn as a gold area (this IS the account, identity color is
// apt here). Times are real UNIX seconds, deduped to keep them strictly rising.
export function EquityChart({ points, height = 150 }: { points: EquityPoint[]; height?: number }) {
  const data = dedupeRising(
    points.map((p) => ({
      time: Math.floor(new Date(p.ts_utc).getTime() / 1000) as UTCTimestamp,
      value: p.equity,
    })),
  );
  const ref = useChart((chart) => {
    const series = chart.addSeries(AreaSeries, {
      lineColor: "#e8b341",
      topColor: "rgba(232,179,65,0.22)",
      bottomColor: "rgba(232,179,65,0.0)",
      lineWidth: 2,
    });
    series.setData(data);
  }, [JSON.stringify(data)]);
  return <div className="chart" ref={ref} style={{ height }} />;
}

function dedupeRising<T extends { time: UTCTimestamp; value: number }>(rows: T[]): T[] {
  const out: T[] = [];
  let last = -Infinity;
  for (const r of rows) {
    let t = r.time as number;
    if (t <= last) t = last + 1;
    last = t;
    out.push({ ...r, time: t as UTCTimestamp });
  }
  return out;
}
