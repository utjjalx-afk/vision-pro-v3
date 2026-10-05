export type Point = { time: number; value: string };
export type Candle = {
  time: number;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  event_id: string;
  delivery_kind: string;
};
export type Spec = {
  instrument_id: string;
  symbol: string;
  venue: string;
  tick_size: string;
  base_currency: string;
  quote_currency: string;
};
export type Level = {
  price: string;
  bid: string;
  ask: string;
  delta: string;
  buy_ratio: string | null;
  sell_ratio: string | null;
};
export type FlowCandle = {
  time: number;
  levels: Level[];
  buy: string;
  sell: string;
  delta: string;
  delta_pct: string | null;
  poc: string;
  cvd: string;
  max_buy_imbalance: string | null;
  max_sell_imbalance: string | null;
};
export type Market = {
  instrument_id: string;
  spec: Spec | null;
  source: string | null;
  source_epoch: number | null;
  clock_warning?: string;
  health: string;
  quote_timestamp_basis?: string;
  candle_health?: string;
  quote_age_seconds: number | null;
  receipt_age_seconds: number | null;
  candle_age_seconds: number | null;
  new_signals_permitted: boolean;
  candles: Candle[];
  indicators: Record<string, Point[]>;
  quote: Record<string, unknown> | null;
  flow: {
    source?: string;
    bucket_increment?: string;
    unit?: string;
    health?: string;
    candles: FlowCandle[];
    trades: Record<string, unknown>[];
    ofi?: string | null;
    cvd_basis?: string;
    imbalance_basis?: string;
    unclassified_trades?: number;
  };
  depth: {
    state: string;
    reason: string | null;
    source: string;
    bids: string[][];
    asks: string[][];
    metrics: Record<string, string | null> | null;
  };
  derivatives: Record<string, unknown>;
};
export type Lane = {
  lane: string;
  assessment_id: string;
  status: string;
  bias: string | null;
  score: string | null;
  confidence: string | null;
  reasons: string[];
  features: Record<string, unknown>[];
  evidence: Record<string, unknown>[];
};
export type Snapshot = {
  schema_version: string;
  version: number;
  as_of: string;
  mode: string;
  live_enabled: false;
  live_eligible: false;
  phase11: string;
  market: Market;
  agents: {
    assessments: Lane[];
    decision: Record<string, unknown> | null;
    reason: string | null;
    reliability_basis?: string;
    validation_error?: string;
  };
  risk: {
    state: string;
    reasons: string[];
    open_risk: string | null;
    risk_limit_fraction: string;
  };
  broker: {
    state: string;
    reasons?: string[];
    reason?: string;
    snapshot: Record<string, unknown> | null;
    quote_status?: {
      symbol: string;
      state: string;
      source_age_seconds: number;
      receipt_age_seconds: number;
    }[];
  };
  execution: Record<string, unknown>;
  portfolio: {
    source: string;
    account: Record<string, unknown> | null;
    positions: Record<string, unknown>[];
    unavailable_metrics: string[];
  };
  journal: Record<string, unknown> | null;
  journal_entries: Record<string, unknown>[];
  timeline: Record<string, unknown>[];
  connections: Record<string, Record<string, unknown>>;
  instruments: Spec[];
  system: Record<string, unknown>;
};
