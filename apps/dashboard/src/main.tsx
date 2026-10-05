import React, { useState, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { Chart } from "./Chart";
import { mergeBatch, displayFresh } from "./transport.mjs";
import type { Snapshot, Market } from "./types";
import "./style.css";

const pages = [
  "Dashboard",
  "Markets",
  "Chart",
  "Order Flow",
  "Agents",
  "Strategies",
  "Backtests",
  "Paper",
  "Portfolio",
  "Risk",
  "Journal",
  "Outcome",
  "Connections",
  "MT5",
  "Research",
  "LLM",
  "Settings",
];
const times = [
  ["1m", 60],
  ["5m", 300],
  ["15m", 900],
  ["1H", 3600],
  ["4H", 14400],
  ["1D", 86400],
] as const;
const initial = {
  page: "Dashboard",
  seconds: 300,
  instrument: "BINANCE:SPOT:BTCUSDT",
  layout: 1,
  enabled: ["ema20", "ema50", "vwap"],
  oscillator: "rsi",
};
function preferences() {
  try {
    const p = JSON.parse(localStorage.getItem("vision-view-v1") || "null");
    return p &&
      pages.includes(p.page) &&
      times.some((t) => t[1] === p.seconds) &&
      [1, 2, 4].includes(p.layout) &&
      Array.isArray(p.enabled) &&
      p.enabled.every((x: unknown) => typeof x === "string")
      ? { ...initial, ...p }
      : initial;
  } catch {
    return initial;
  }
}
function fmt(value: unknown) {
  if (value === null || value === undefined) return "N/A";
  if (typeof value === "object") return JSON.stringify(value);
  if (
    typeof value === "string" &&
    value.length > 16 &&
    /^-?\d+(\.\d+)?$/.test(value) &&
    Number.isFinite(Number(value))
  )
    return Number(value).toLocaleString("en-US", { maximumFractionDigits: 6 });
  return String(value);
}
function age(value: number | null) {
  return value === null
    ? "N/A"
    : value < 0
      ? `${(-value / 3600).toFixed(2)}h ahead`
      : `${value.toFixed(2)}s`;
}
function Badge({ value }: { value: string }) {
  return (
    <span
      className={`badge ${value === "HEALTHY" ? "good" : /BLOCK|STALE|FAIL|REAL/.test(value) ? "bad" : "muted"}`}
    >
      {value.replaceAll("_", " ")}
    </span>
  );
}
function Inspect({
  value,
  title = "Evidence",
}: {
  value: unknown;
  title?: string;
}) {
  return (
    <details className="inspect">
      <summary>{title}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}
function Metrics({ items }: { items: [string, unknown][] }) {
  return (
    <div className="metrics">
      {items.map(([k, v]) => (
        <div className="metric" key={k}>
          <span>{k}</span>
          <strong>{fmt(v)}</strong>
        </div>
      ))}
    </div>
  );
}
function Panel({
  title,
  kicker,
  children,
}: {
  title: string;
  kicker?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <small>{kicker || "BACKEND PROJECTION"}</small>
          <h2>{title}</h2>
        </div>
      </div>
      {children}
    </section>
  );
}
function Footprint({ market }: { market: Market }) {
  const [index, setIndex] = useState(-1);
  const candles = market.flow.candles;
  const c =
    candles[
      index < 0 ? candles.length - 1 : Math.min(index, candles.length - 1)
    ];
  return (
    <Panel
      title="Bid × Ask Footprint"
      kicker={`${market.flow.source || market.source || "UNAVAILABLE"} ORDER FLOW · BASE QUANTITY`}
    >
      <div className="flow-toolbar">
        <Badge value={market.flow.health || market.health} />
        <select
          aria-label="Footprint candle"
          value={index}
          onChange={(e) => setIndex(Number(e.target.value))}
        >
          <option value={-1}>Latest candle</option>
          {candles.map((c, i) => (
            <option value={i} key={c.time}>
              {new Date(c.time * 1000).toISOString().slice(11, 19)}
            </option>
          ))}
        </select>
        <span>
          {market.flow.bucket_increment || "N/A"} broker/provider tick buckets
        </span>
      </div>
      {c ? (
        <>
          <Metrics
            items={[
              ["Aggressor buy", c.buy],
              ["Aggressor sell", c.sell],
              ["Delta", c.delta],
              ["Delta %", c.delta_pct],
              ["POC", c.poc],
              ["CVD", c.cvd],
              ["Max buy imbalance", c.max_buy_imbalance],
              ["Max sell imbalance", c.max_sell_imbalance],
            ]}
          />
          <div className="table-scroll footprint-scroll">
            <table>
              <thead>
                <tr>
                  <th>Price</th>
                  <th>Bid / sell</th>
                  <th>Ask / buy</th>
                  <th>Delta</th>
                </tr>
              </thead>
              <tbody>
                {c.levels
                  .slice(-120)
                  .reverse()
                  .map((l) => (
                    <tr
                      key={l.price}
                      className={l.price === c.poc ? "poc" : ""}
                    >
                      <td>
                        {l.price}
                        {l.price === c.poc && <small> POC</small>}
                      </td>
                      <td className="sell">{l.bid}</td>
                      <td className="buy">{l.ask}</td>
                      <td className={Number(l.delta) >= 0 ? "buy" : "sell"}>
                        {l.delta}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          <p className="caption">
            {market.flow.imbalance_basis} · CVD {market.flow.cvd_basis} ·{" "}
            {market.flow.unclassified_trades} unclassified trades
          </p>
        </>
      ) : (
        <p className="empty">
          No verified aggressor trades. Footprint remains unavailable.
        </p>
      )}
    </Panel>
  );
}
function Depth({ market }: { market: Market }) {
  const d = market.depth;
  return (
    <Panel title="Depth & liquidity" kicker={`${d.source} · SEQUENCE CHECKED`}>
      <Badge value={d.state} />
      {d.reason && <p className="caption">{d.reason}</p>}
      <div className="book">
        <div>
          {d.asks
            .slice(0, 10)
            .reverse()
            .map(([p, q]) => (
              <div className="book-row sell" key={p}>
                <span>{p}</span>
                <b>{q}</b>
              </div>
            ))}
        </div>
        <div className="book-mid">
          MID <strong>{fmt(d.metrics?.mid)}</strong>
          <span>SPREAD {fmt(d.metrics?.spread)}</span>
        </div>
        <div>
          {d.bids.slice(0, 10).map(([p, q]) => (
            <div className="book-row buy" key={p}>
              <span>{p}</span>
              <b>{q}</b>
            </div>
          ))}
        </div>
      </div>
      {!d.bids.length && (
        <p className="empty">No synchronized depth snapshot.</p>
      )}
      <Metrics
        items={[
          ["Top 5 imbalance", d.metrics?.top5_imbalance],
          ["Top 10 imbalance", d.metrics?.top10_imbalance],
          ["Microprice", d.metrics?.microprice],
          ["Level-one OFI", market.flow.ofi],
        ]}
      />
      <p className="caption">
        Source-local descriptors; no direct BUY/SELL authority.
      </p>
    </Panel>
  );
}
function Agents({ data }: { data: Snapshot }) {
  return (
    <Panel
      title="Agent topology"
      kicker="INDEPENDENT LANES → RELIABILITY → SYNTHESIS"
    >
      {data.agents.validation_error && (
        <p className="caption">
          Canonical context blocked: {data.agents.validation_error}. No
          timestamps were adjusted.
        </p>
      )}
      <div className="topology">
        <div className="topology-source">
          CANONICAL MARKET DATA <Badge value={data.market.health} />
        </div>
        <div className="lane-grid">
          {["technical", "flow", "macro", "narrative"].map((name) => {
            const a = data.agents.assessments.find((a) => a.lane === name);
            return (
              <div className="lane" key={name}>
                <h3>{name}</h3>
                <Badge value={a?.status || "UNAVAILABLE"} />
                <strong>{a?.bias || "—"}</strong>
                <dl>
                  <dt>Score</dt>
                  <dd>{fmt(a?.score)}</dd>
                  <dt>Confidence</dt>
                  <dd>{fmt(a?.confidence)}</dd>
                  <dt>Reliability</dt>
                  <dd>UNPROVEN</dd>
                </dl>
                <p>
                  {a?.reasons.join(" · ") ||
                    data.agents.reason ||
                    "No validated live context"}
                </p>
                {a && <Inspect value={a} title="Assessment + lineage" />}
              </div>
            );
          })}
        </div>
        <div className="topology-line">
          ↓ reliability excludes unsupported evidence ↓
        </div>
        <div className="synthesis-node">
          <small>SYNTHESIZER</small>
          <h2>{fmt(data.agents.decision?.direction || "WAIT")}</h2>
          <p>{fmt(data.agents.decision?.reasons || data.agents.reason)}</p>
          <span>No trade intent generated</span>
          <Inspect value={data.agents.decision} title="Synthesis receipt" />
        </div>
        <div className="topology-line">
          ↓ candidate → risk veto → paper / demo ↓
        </div>
        <div className="risk-node">
          <Badge value={data.risk.state} />
          {data.risk.reasons.join(" · ")}
        </div>
      </div>
    </Panel>
  );
}
function Positions({ data }: { data: Snapshot }) {
  return (
    <Panel title="Portfolio" kicker="MT5 BROKER TRUTH · ACCOUNT CURRENCY">
      <Metrics
        items={[
          ["Currency", data.portfolio.account?.currency],
          ["Balance", data.portfolio.account?.balance],
          ["Equity", data.portfolio.account?.equity],
          ["Free margin", data.portfolio.account?.free_margin],
          ["Daily PnL", null],
          ["Open risk", data.risk.open_risk],
        ]}
      />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {["Symbol", "Side", "Volume", "Entry", "SL", "Source"].map(
                (c) => (
                  <th key={c}>{c}</th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {data.portfolio.positions.map((p, i) => (
              <tr key={i}>
                {["symbol", "side", "volume", "entry", "stop"].map((k) => (
                  <td key={k}>{fmt(p[k])}</td>
                ))}
                <td>MT5</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!data.portfolio.positions.length && (
        <p className="empty">
          {data.portfolio.account
            ? "No open positions in attached snapshot."
            : "Broker snapshot unavailable; positions are unknown."}
        </p>
      )}
      <Inspect value={data.portfolio} title="Portfolio evidence" />
    </Panel>
  );
}
function Timeline({ entries }: { entries: Record<string, unknown>[] }) {
  return (
    <div className="timeline">
      {entries.length ? (
        entries.map((e, i) => (
          <div key={i} className="timeline-item">
            <span className="timeline-dot" />
            <div>
              <small>{fmt(e.recorded_at || e.at)}</small>
              <strong>{fmt(e.kind)}</strong>
              <Inspect
                value={e}
                title={`Inspect ${String(e.entry_id || e.id || i).slice(0, 16)}`}
              />
            </div>
          </div>
        ))
      ) : (
        <p className="empty">No attached verified journal entries.</p>
      )}
    </div>
  );
}
function DemoResearch({
  data,
  onChart,
}: {
  data: Snapshot;
  onChart: (instrument: string) => void;
}) {
  const native = data.broker.snapshot;
  const quotes = (native?.quotes || []) as {
    symbol: string;
    bid: string;
    ask: string;
    source_at: string;
  }[];
  return (
    <Panel
      title="MT5 demo validation"
      kicker="STEP 1 · NATIVE BROKER · NO ORDER DISPATCH"
    >
      <Badge value={data.broker.state} />
      <p className="caption">
        Four-asset demo checks first → daily report at 8:00 PM IST → seven-day
        research review. Orders remain gated by Phase 11 acceptance.
      </p>
      <Metrics
        items={[
          [
            "Account",
            data.portfolio.account?.demo === true ? "DEMO" : "UNAVAILABLE",
          ],
          ["Currency", data.portfolio.account?.currency],
          ["Equity", data.portfolio.account?.equity],
          ["Free margin", data.portfolio.account?.free_margin],
        ]}
      />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Exact symbol</th>
              <th>Bid</th>
              <th>Ask</th>
              <th>Source clock</th>
              <th>Check</th>
              <th>Chart</th>
            </tr>
          </thead>
          <tbody>
            {["EURUSD", "XAUUSD", "XAGUSD", "BTCUSD"].map((symbol) => {
              const q = quotes.find((q) => q.symbol === symbol),
                check = data.broker.quote_status?.find(
                  (q) => q.symbol === symbol,
                ),
                id = `MT5:DEMO:${symbol}`;
              const available = data.instruments.some(
                (i) => i.instrument_id === id,
              );
              return (
                <tr key={symbol}>
                  <td>{symbol}</td>
                  <td>{fmt(q?.bid)}</td>
                  <td>{fmt(q?.ask)}</td>
                  <td>
                    {check
                      ? check.source_age_seconds < 0
                        ? `${(-check.source_age_seconds / 60).toFixed(1)} min ahead`
                        : `${check.source_age_seconds.toFixed(1)}s age`
                      : "UNKNOWN"}
                  </td>
                  <td>
                    <Badge value={check?.state || "UNAVAILABLE"} />
                  </td>
                  <td>
                    <button disabled={!available} onClick={() => onChart(id)}>
                      {available ? "Open native chart" : "Reader unavailable"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p role="status" className="caption">
        {data.broker.reasons?.join(" · ") || data.broker.reason}. Native history
        is for inspection; an unverified broker clock cannot approve sizing.
      </p>
      <div className="report-actions">
        <a className="primary" href="/api/research/report?format=md" download>
          Download daily report
        </a>
        <a href="/api/research/report" download>
          Download JSON evidence
        </a>
      </div>
      <p className="caption">
        Daily PnL stays unavailable until deal journal and clock attribution
        reconcile. No fabricated trade results. Reports contain private account
        evidence; keep them local.
      </p>
      <Inspect value={data.broker} title="Native snapshot and exact specs" />
    </Panel>
  );
}

function EvidencePage({
  data,
  page,
  onChart,
}: {
  data: Snapshot;
  page: string;
  onChart: (instrument: string) => void;
}) {
  let v: unknown = null;
  let title = page;
  if (page === "Risk") v = data.risk;
  else if (page === "MT5" || page === "Research")
    return <DemoResearch data={data} onChart={onChart} />;
  else if (page === "Connections")
    return (
      <div className="connection-grid">
        {Object.entries(data.connections).map(([name, c]) => (
          <Panel title={name.toUpperCase()} key={name}>
            <Badge value={fmt(c.state)} />
            <p className="caption">
              {fmt(
                c.reason ||
                  c.reasons ||
                  (c.state === "RECEIVING"
                    ? "Public analysis data receiving"
                    : "No verified connection"),
              )}
            </p>
            <Inspect value={c} title="Connection evidence" />
          </Panel>
        ))}
      </div>
    );
  else if (page === "Journal")
    return (
      <Panel title="Journal & lineage">
        <Timeline
          entries={
            data.journal_entries.length ? data.journal_entries : data.timeline
          }
        />
      </Panel>
    );
  else if (page === "Outcome") v = data.journal?.trade_outcomes;
  else if (page === "Strategies") v = data.journal?.strategy_lifecycle;
  else if (page === "Backtests") v = data.journal?.backtest_runs;
  else if (page === "Paper") v = data.journal?.paper_heads;
  else if (page === "LLM")
    return (
      <Panel title="LLM & narrative">
        <Badge value="UNAVAILABLE" />
        <p className="empty">
          No configured verified news/macro context. LLM output cannot create
          broker orders.
        </p>
      </Panel>
    );
  else if (page === "Settings")
    return (
      <Panel title="System & viewer settings">
        <Metrics
          items={[
            ["Role", "VIEWER"],
            ["Session", "1 hour / ephemeral"],
            ["Execution controls", "Unavailable in Phase 11.5"],
            ["Live eligibility", "NO"],
          ]}
        />
        <Inspect value={data.system} title="System health" />
      </Panel>
    );
  else v = data.system;
  return (
    <Panel title={title}>
      <Badge
        value={
          v === null || v === undefined
            ? "UNAVAILABLE"
            : page === "Risk"
              ? data.risk.state
              : "READ_ONLY"
        }
      />
      {v === null || v === undefined ? (
        <p className="empty">No verified backend evidence attached.</p>
      ) : (
        <Inspect value={v} title={`${page} backend truth`} />
      )}
      {["Strategies", "Backtests"].includes(page) && (
        <>
          <h3>Failure memory</h3>
          <Inspect
            value={data.journal?.failures || null}
            title="Rejected / parked experiments"
          />
          <p className="caption">
            Only journal records are shown; named historical failures require
            attached evidence.
          </p>
        </>
      )}
    </Panel>
  );
}
function App() {
  const [prefs, setPrefs] = useState(preferences),
    [data, setData] = useState<Snapshot | null>(null),
    [connected, setConnected] = useState(false),
    [auth, setAuth] = useState(false),
    [token, setToken] = useState(""),
    [error, setError] = useState(""),
    [now, setNow] = useState(Date.now()),
    [reconnect, setReconnect] = useState(0),
    [secondary, setSecondary] = useState<Market[]>([]),
    [historical, setHistorical] = useState<Market | null>(null);
  const received = useRef(0),
    historyGeneration = useRef(0),
    current = useRef<Snapshot | null>(null);
  useEffect(() => {
    localStorage.setItem("vision-view-v1", JSON.stringify(prefs));
  }, [prefs]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    let disposed = false,
      ws: WebSocket | null = null,
      retry: ReturnType<typeof setTimeout> | null = null;
    const abort = new AbortController();
    current.current = null;
    setData(null);
    received.current = 0;
    setHistorical(null);
    historyGeneration.current++;
    const url = `instrument=${encodeURIComponent(prefs.instrument)}&seconds=${prefs.seconds}`;
    async function open() {
      try {
        const res = await fetch(`/api/dashboard/summary?${url}`, {
          signal: abort.signal,
        });
        if (res.status === 401) {
          setAuth(false);
          return;
        }
        if (!res.ok) throw new Error("Snapshot unavailable");
        const d: Snapshot = await res.json();
        if (disposed) return;
        setAuth(true);
        setError("");
        setData(d);
        current.current = d;
        received.current = Date.now();
        let last = -1;
        ws = new WebSocket(
          `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/dashboard?${url}`,
        );
        ws.onopen = () => setConnected(true);
        ws.onmessage = (e) => {
          try {
            const m = mergeBatch(current.current, JSON.parse(e.data), last);
            last = m.sequence;
            current.current = m.data as Snapshot;
            received.current = Date.now();
            setData(current.current);
          } catch {
            ws?.close();
          }
        };
        ws.onclose = () => {
          setConnected(false);
          if (!disposed) retry = setTimeout(open, 2000);
        };
        ws.onerror = () => ws?.close();
      } catch (e) {
        if (!disposed) {
          setError("Dashboard connection unavailable");
          retry = setTimeout(open, 3000);
        }
      }
    }
    open();
    return () => {
      disposed = true;
      abort.abort();
      if (retry) clearTimeout(retry);
      ws?.close();
      setConnected(false);
    };
  }, [prefs.instrument, prefs.seconds, auth, reconnect]);
  useEffect(() => {
    if (!auth || prefs.layout === 1) {
      setSecondary([]);
      return;
    }
    let disposed = false;
    const abort = new AbortController();
    async function poll() {
      const secs = [60, 900, 3600].slice(0, prefs.layout - 1);
      try {
        const markets = await Promise.all(
          secs.map(async (seconds) => {
            const r = await fetch(
              `/api/dashboard/summary?instrument=${encodeURIComponent(prefs.instrument)}&seconds=${seconds}`,
              { signal: abort.signal },
            );
            if (!r.ok) throw new Error();
            return ((await r.json()) as Snapshot).market;
          }),
        );
        if (!disposed) setSecondary(markets);
      } catch {
        if (!disposed) setSecondary([]);
      }
    }
    poll();
    const t = setInterval(poll, 3000);
    return () => {
      disposed = true;
      abort.abort();
      clearInterval(t);
    };
  }, [prefs.instrument, prefs.layout, auth]);
  async function older() {
    const basis = historical || data?.market;
    if (!basis?.candles.length) return;
    const generation = historyGeneration.current;
    try {
      const r = await fetch(
        `/api/market/history?instrument=${encodeURIComponent(prefs.instrument)}&seconds=${prefs.seconds}&before=${basis.candles[0].time}`,
      );
      if (!r.ok) throw new Error();
      const h = await r.json();
      if (generation === historyGeneration.current)
        setHistorical({ ...basis, ...h, new_signals_permitted: false });
    } catch {
      setError("Historical data unavailable");
    }
  }
  async function login(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const r = await fetch("/auth/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      setToken("");
      if (!r.ok) throw new Error();
      setAuth(true);
    } catch {
      setError("Viewer login failed. Check the private local token file.");
    }
  }
  if (!auth)
    return (
      <div className="login-wrap">
        <div className="login">
          <div className="brand-mark">V</div>
          <small>VISION PRO V3</small>
          <h1>Command Center</h1>
          <p>One workspace. Evidence first.</p>
          <Badge value="LIVE OFF" />
          <form onSubmit={login}>
            <label htmlFor="token">Private local viewer token</label>
            <input
              id="token"
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="off"
              required
            />
            <button className="primary">Open command center →</button>
          </form>
          {error && (
            <p role="alert" className="sell">
              {error}
            </p>
          )}
          <p className="caption">
            Read-only session · no trading controls · credentials stay out of
            browser storage
          </p>
        </div>
      </div>
    );
  if (!data)
    return (
      <div className="login-wrap">
        <div>
          <p role="status">Loading broker and market snapshot… {error}</p>
          <button
            onClick={() => {
              setPrefs(initial);
              setReconnect((v) => v + 1);
            }}
          >
            Retry with supported market
          </button>
        </div>
      </div>
    );
  const fresh = connected && displayFresh(received.current, now);
  const health = fresh ? data.market.health : "DISCONNECTED";
  const market = {
    ...data.market,
    health,
    new_signals_permitted: fresh && data.market.new_signals_permitted,
  };
  const set = (key: string, value: unknown) =>
    setPrefs({ ...prefs, [key]: value });
  const latest = market.flow.candles.at(-1);
  const nativeDisplay = market.source === "mt5.demo";
  const visibleIndicators = prefs.enabled.filter(
    (n: string) => !nativeDisplay || n !== "vwap",
  );
  const visibleOscillator =
    nativeDisplay && ["cvd", "delta"].includes(prefs.oscillator)
      ? "rsi"
      : prefs.oscillator;
  const chartPages = ["Dashboard", "Markets", "Chart", "Order Flow"].includes(
    prefs.page,
  );
  return (
    <div className="app">
      <header className="global">
        <div className="brand">
          <div className="brand-mark">V</div>
          <div>
            <strong>
              VISION PRO <b>V3</b>
            </strong>
            <small>COMMAND CENTER</small>
          </div>
        </div>
        <span className="mode">PAPER / DEMO · VIEWER</span>
        <Badge value={health} />
        <span className="global-age">
          {market.quote_timestamp_basis === "receipt"
            ? "RECEIPT AGE"
            : "QUOTE AGE"}{" "}
          {age(market.quote_age_seconds)}
        </span>
        <button
          onClick={() => {
            setPrefs({ ...prefs, page: "MT5" });
          }}
        >
          MT5 demo checks
        </button>
        <button onClick={() => setReconnect((v) => v + 1)}>
          Refresh connection
        </button>
        <span className="live-off">● LIVE OFF</span>
        <button
          className="logout"
          onClick={async () => {
            await fetch("/auth/logout", { method: "POST" });
            setAuth(false);
          }}
        >
          Lock
        </button>
      </header>
      <div className="shell">
        <nav className="sidebar" aria-label="Workspace navigation">
          <small>WORKSPACE</small>
          {pages.map((p, i) => (
            <button
              key={p}
              onClick={() => set("page", p)}
              className={prefs.page === p ? "active" : ""}
            >
              <span className="nav-icon">
                {
                  [
                    "◈",
                    "⌁",
                    "▥",
                    "≋",
                    "◎",
                    "◇",
                    "▦",
                    "▧",
                    "◫",
                    "⛨",
                    "≡",
                    "↗",
                    "⊙",
                    "⇄",
                    "✧",
                    "⚙",
                  ][i]
                }
              </span>
              {p}
            </button>
          ))}
          <div className="sidebar-foot">
            PHASE 11.5<small>PROVENANCE FIRST</small>
            <span className="status-dot" /> LOCAL WORKSPACE
          </div>
        </nav>
        <main>
          <div className="workspace-heading">
            <div>
              <small>INTELLIGENCE / {prefs.page.toUpperCase()}</small>
              <h1>
                {prefs.page === "Dashboard" ? "Market command" : prefs.page}
              </h1>
            </div>
            <span className="server-time">
              {new Date(data.as_of).toISOString().slice(11, 19)} UTC
            </span>
          </div>
          <div className="veto" role="status">
            <span>⛨ RISK BLOCKED</span>
            <p>
              {!fresh
                ? "Dashboard stream disconnected — new signals blocked"
                : data.risk.reasons.join(" · ")}
            </p>
            <Badge value="EXECUTION DISARMED" />
          </div>
          {chartPages ? (
            <>
              <div className="workspace-grid">
                <div className="market-column">
                  <section className="panel market-panel">
                    {market.clock_warning && (
                      <p className="caption">
                        MT5 DEMO history · {market.clock_warning}
                      </p>
                    )}
                    <div className="chart-toolbar">
                      <select
                        aria-label="Market instrument"
                        value={prefs.instrument}
                        onChange={(e) => set("instrument", e.target.value)}
                      >
                        {[
                          ...new Set([
                            prefs.instrument,
                            ...data.instruments.map((i) => i.instrument_id),
                          ]),
                        ].map((s) => (
                          <option key={s}>{s}</option>
                        ))}
                      </select>
                      <div className="time-buttons">
                        {times.map(([label, seconds]) => (
                          <button
                            className={
                              prefs.seconds === seconds ? "selected" : ""
                            }
                            key={seconds}
                            onClick={() => set("seconds", seconds)}
                          >
                            {label}
                          </button>
                        ))}
                      </div>
                      <select
                        aria-label="Workspace layout"
                        value={prefs.layout}
                        onChange={(e) => set("layout", Number(e.target.value))}
                      >
                        <option value={1}>1 chart</option>
                        <option value={2}>2 charts</option>
                        <option value={4}>4 charts</option>
                      </select>
                    </div>
                    <div className="history-controls">
                      <button onClick={older} disabled={!market.candles.length}>
                        ← Load older 512 bars
                      </button>
                      {historical && (
                        <>
                          <Badge value="HISTORICAL" />
                          <button onClick={() => setHistorical(null)}>
                            Return to current window →
                          </button>
                        </>
                      )}
                      {error && <span role="alert">{error}</span>}
                    </div>
                    <div className="provenance">
                      <span>
                        {market.source === "mt5.demo"
                          ? "BROKER HISTORY"
                          : "ANALYSIS"}{" "}
                        <b>{market.source || "UNAVAILABLE"}</b>
                      </span>
                      <span>
                        BROKER <b>MT5 · {data.broker.state}</b>
                      </span>
                      <span>
                        EPOCH <b>{fmt(market.source_epoch)}</b>
                      </span>
                      <span>
                        CANDLE AGE <b>{age(market.candle_age_seconds)}</b>
                      </span>
                    </div>
                    <div className="indicator-toolbar">
                      <details>
                        <summary>Indicators +</summary>
                        <div className="indicator-menu">
                          {[
                            "ema20",
                            "ema50",
                            "ema100",
                            "ema200",
                            "sma20",
                            "vwap",
                            "bbUpper",
                            "bbLower",
                          ].map((name) => (
                            <label key={name}>
                              <input
                                type="checkbox"
                                disabled={nativeDisplay && name === "vwap"}
                                checked={visibleIndicators.includes(name)}
                                onChange={() =>
                                  set(
                                    "enabled",
                                    prefs.enabled.includes(name)
                                      ? prefs.enabled.filter(
                                          (n: string) => n !== name,
                                        )
                                      : [...prefs.enabled, name],
                                  )
                                }
                              />
                              {name.toUpperCase()}
                            </label>
                          ))}
                        </div>
                      </details>
                      {visibleIndicators.map((name: string) => (
                        <span className="indicator-chip" key={name}>
                          {name.toUpperCase()}
                        </span>
                      ))}
                      <select
                        aria-label="Lower chart indicator"
                        value={visibleOscillator}
                        onChange={(e) => set("oscillator", e.target.value)}
                      >
                        {[
                          "rsi",
                          "atr",
                          "macd",
                          "macdSignal",
                          "macdHist",
                          "adx",
                          "stochK",
                          "stochD",
                          "volume",
                          "cvd",
                          "delta",
                        ]
                          .filter(
                            (n) =>
                              !nativeDisplay || !["cvd", "delta"].includes(n),
                          )
                          .map((n) => (
                            <option key={n}>{n}</option>
                          ))}
                      </select>
                    </div>
                    {health !== "HEALTHY" && (
                      <div className="chart-warning">
                        {health} DATA — NEW SIGNALS BLOCKED
                      </div>
                    )}
                    <div className={`charts charts-${prefs.layout}`}>
                      <Chart
                        market={historical || market}
                        enabled={visibleIndicators}
                        oscillator={visibleOscillator}
                      />
                      {secondary.map((m, i) => (
                        <div key={i}>
                          <small className="tile-label">
                            {m.instrument_id} · {["1m", "15m", "1H"][i]} ·{" "}
                            {m.health}
                          </small>
                          <Chart
                            market={m}
                            enabled={visibleIndicators}
                            oscillator={visibleOscillator}
                          />
                        </div>
                      ))}
                    </div>
                    <a
                      className="attribution"
                      href="https://www.tradingview.com/"
                      target="_blank"
                      rel="noreferrer"
                    >
                      Charting by TradingView
                    </a>
                    <a
                      className="attribution"
                      href="/assets/third-party-notices.txt"
                      target="_blank"
                      rel="noreferrer"
                    >
                      Third-party licenses
                    </a>
                  </section>
                  <Metrics
                    items={
                      nativeDisplay
                        ? [
                            ["Symbol", market.spec?.symbol],
                            ["Bid", market.quote?.bid],
                            ["Ask", market.quote?.ask],
                            ["Base", market.spec?.base_currency],
                            ["Profit currency", market.spec?.quote_currency],
                            ["Volume", "TICK COUNT"],
                          ]
                        : [
                            ["CVD", latest?.cvd],
                            ["Candle delta", latest?.delta],
                            [
                              "Book imbalance",
                              market.depth.metrics?.top5_imbalance,
                            ],
                            ["Funding", null],
                            ["Open interest", null],
                            ["Flow scope", "PROVIDER LOCAL"],
                          ]
                    }
                  />
                  <div className="flow-grid">
                    {nativeDisplay ? (
                      <Panel
                        title="Native demo history"
                        kicker="READ ONLY · BROKER CLOCK"
                      >
                        <p className="caption">
                          Native BID candles and tick-count volume. Aggressor
                          order flow/depth are unavailable for this feed. The
                          displayed clock offset keeps sizing blocked; these
                          history charts do not approve orders.
                        </p>
                      </Panel>
                    ) : (
                      <Footprint market={market} />
                    )}
                    {!nativeDisplay && <Depth market={market} />}
                  </div>
                </div>
                <aside className="intel">
                  <Panel title="Agent council" kicker="LANE STATUS">
                    <div className="council">
                      {["technical", "flow", "macro", "narrative"].map((n) => {
                        const a = data.agents.assessments.find(
                          (a) => a.lane === n,
                        );
                        return (
                          <div key={n}>
                            <span>{n}</span>
                            <Badge
                              value={a?.bias || a?.status || "UNAVAILABLE"}
                            />
                          </div>
                        );
                      })}
                    </div>
                    <div className="council-decision">
                      <small>SYNTHESIS</small>
                      <strong>
                        {fmt(data.agents.decision?.direction || "WAIT")}
                      </strong>
                      <p>
                        {fmt(
                          data.agents.decision?.reasons || data.agents.reason,
                        )}
                      </p>
                    </div>
                  </Panel>
                  <Panel title="Risk governor" kicker="HARD VETO">
                    <Badge value={data.risk.state} />
                    <Metrics
                      items={[
                        ["Equity", data.portfolio.account?.equity],
                        ["Currency", data.portfolio.account?.currency],
                        ["Open risk", data.risk.open_risk],
                        ["Risk cap", "5%"],
                      ]}
                    />
                    <p className="caption">{data.risk.reasons.join(" · ")}</p>
                  </Panel>
                  <Panel title="Execution" kicker="MT5 DEMO · READ ONLY">
                    <Badge value="DISARMED" />
                    <p>Phase 11 acceptance pending.</p>
                    <dl>
                      <dt>Intent</dt>
                      <dd>—</dd>
                      <dt>Sizing</dt>
                      <dd>—</dd>
                      <dt>Order</dt>
                      <dd>—</dd>
                      <dt>Live eligibility</dt>
                      <dd>NO</dd>
                    </dl>
                  </Panel>
                </aside>
              </div>
            </>
          ) : prefs.page === "Agents" ? (
            <Agents data={data} />
          ) : prefs.page === "Portfolio" ? (
            <Positions data={data} />
          ) : (
            <EvidencePage
              data={data}
              page={prefs.page}
              onChart={(instrument) =>
                setPrefs({ ...prefs, instrument, page: "Chart" })
              }
            />
          )}
        </main>
      </div>
      <footer>
        <span>
          <i className="status-dot" />
          FEED {health}
        </span>
        <span>
          RISK <b>BLOCKED</b>
        </span>
        <span>MT5 {data.broker.state}</span>
        <span>JOURNAL {data.journal ? "VERIFIED" : "UNAVAILABLE"}</span>
        <span className="footer-right">
          LIVE ELIGIBLE: NO · EXECUTION DISARMED
        </span>
      </footer>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
