import { useEffect, useRef } from "react";
import {
  createChart,
  CandlestickSeries,
  LineSeries,
  HistogramSeries,
  ColorType,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Market } from "./types";

type Item = {
  time: UTCTimestamp;
  value?: number;
  open?: number;
  high?: number;
  low?: number;
  close?: number;
};
function apply(series: ISeriesApi<any>, next: Item[], old: Item[]) {
  if (
    !old.length ||
    !next.length ||
    next[0].time !== old[0].time ||
    next.length < old.length ||
    next.length > old.length + 1 ||
    old
      .slice(0, -1)
      .some((p, i) => JSON.stringify(p) !== JSON.stringify(next[i]))
  )
    series.setData(next);
  else
    for (const p of next.slice(Math.max(0, old.length - 1))) series.update(p);
}

export function Chart({
  market,
  enabled,
  oscillator = "rsi",
}: {
  market: Market;
  enabled: string[];
  oscillator?: string;
}) {
  const root = useRef<HTMLDivElement>(null),
    lower = useRef<HTMLDivElement>(null);
  const refs = useRef<{
    chart: IChartApi;
    sub: IChartApi;
    candle: ISeriesApi<"Candlestick">;
    lines: Map<string, ISeriesApi<"Line">>;
    osc: ISeriesApi<any>;
    data: Map<string, Item[]>;
  } | null>(null);
  useEffect(() => {
    if (!root.current || !lower.current) return;
    const options = {
      layout: {
        background: { type: ColorType.Solid, color: "#0e1623" },
        textColor: "#8494ab",
        attributionLogo: true,
      },
      grid: {
        vertLines: { color: "#182332" },
        horzLines: { color: "#182332" },
      },
      rightPriceScale: { borderColor: "#263347" },
      timeScale: { timeVisible: true, borderColor: "#263347" },
      crosshair: { mode: 0 },
      height: 330,
    };
    const chart = createChart(root.current, options),
      sub = createChart(lower.current, { ...options, height: 105 });
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: "#36d9ad",
      downColor: "#f47e8c",
      borderVisible: false,
      wickUpColor: "#36d9ad",
      wickDownColor: "#f47e8c",
    });
    const osc =
      oscillator === "volume" || oscillator === "macdHist"
        ? sub.addSeries(HistogramSeries, { color: "#499ed8" })
        : sub.addSeries(LineSeries, { color: "#8eafff", lineWidth: 2 });
    const instance = {
      chart,
      sub,
      candle,
      lines: new Map<string, ISeriesApi<"Line">>(),
      osc,
      data: new Map<string, Item[]>(),
    };
    refs.current = instance;
    const ro = new ResizeObserver(() => {
      if (root.current && lower.current) {
        chart.resize(root.current.clientWidth, 330);
        sub.resize(lower.current.clientWidth, 105);
      }
    });
    ro.observe(root.current);
    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (range) sub.timeScale().setVisibleLogicalRange(range);
    });
    return () => {
      ro.disconnect();
      refs.current = null;
      chart.remove();
      sub.remove();
    };
  }, [market.instrument_id, oscillator]);
  useEffect(() => {
    const r = refs.current;
    if (!r) return;
    const tick = Number(market.spec?.tick_size),
      precision = market.spec
        ? Math.max(
            0,
            (
              market.spec.tick_size
                .split(/[Ee]/)[0]
                .replace(/0+$/, "")
                .split(".")[1] || ""
            ).length - Number(market.spec.tick_size.split(/[Ee]/)[1] || 0),
          )
        : 2;
    if (Number.isFinite(tick) && tick > 0)
      r.candle.applyOptions({
        priceFormat: { type: "price", precision, minMove: tick },
      });
    const bars = market.candles.map((b) => ({
      time: b.time as UTCTimestamp,
      open: Number(b.open),
      high: Number(b.high),
      low: Number(b.low),
      close: Number(b.close),
    }));
    const first = !r.data.get("candles")?.length;
    apply(r.candle, bars, r.data.get("candles") || []);
    r.data.set("candles", bars);
    const colors = [
      "#79a8ff",
      "#f2c878",
      "#cf9df7",
      "#74ddce",
      "#8fa2b8",
      "#ffab80",
      "#7194ff",
      "#7194ff",
    ];
    for (const [name, series] of r.lines)
      if (!enabled.includes(name)) {
        r.chart.removeSeries(series);
        r.lines.delete(name);
        r.data.delete(name);
      }
    enabled.forEach((name, i) => {
      let series = r.lines.get(name);
      if (!series) {
        series = r.chart.addSeries(LineSeries, {
          color: colors[i % colors.length],
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        r.lines.set(name, series);
      }
      const data = (market.indicators[name] || []).map((p) => ({
        time: p.time as UTCTimestamp,
        value: Number(p.value),
      }));
      apply(series, data, r.data.get(name) || []);
      r.data.set(name, data);
    });
    const vals = new Map(
      (market.indicators[oscillator] || []).map((p) => [
        p.time,
        Number(p.value),
      ]),
    );
    const allTimes = [
      ...new Set([...market.candles.map((b) => b.time), ...vals.keys()]),
    ].sort((a, b) => a - b);
    const os = allTimes.map((time) =>
      vals.has(time)
        ? { time: time as UTCTimestamp, value: vals.get(time)! }
        : { time: time as UTCTimestamp },
    );
    apply(r.osc, os, r.data.get("osc") || []);
    r.data.set("osc", os);
    if (first && bars.length) r.chart.timeScale().fitContent();
  }, [market.candles, market.indicators, market.spec, enabled, oscillator]);
  return (
    <div className="chart-frame">
      <div ref={root} aria-label="Canonical OHLC candlestick chart" />
      <div className="osc-label">
        {oscillator.toUpperCase()} · validated batch math · warm-up excluded
      </div>
      <div ref={lower} aria-label={`${oscillator} chart`} />
      {!market.candles.length && (
        <div className="empty-chart">
          <span className="empty-icon">⌁</span>
          <h3>Waiting for canonical candles</h3>
          <p>
            {market.source || "No source connected"} · Historical data never
            implies a live feed.
          </p>
        </div>
      )}
    </div>
  );
}
