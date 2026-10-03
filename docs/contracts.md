# Phase-1 canonical contracts

`schemas/canonical-market-event.schema.json` and `schemas/instrument-spec.schema.json`
are JSON Schema Draft 2020-12 wire contracts. Python counterparts are in
`vision.core.contracts`; strict decoding is in `vision.core.codec`. Alignment
to the missing full frozen architecture text remains provisional.

Use synthetic fixtures. Prices/increments/contract size must be positive finite
decimals; quantities may be unsigned zero. Negative-zero quantities are rejected
to keep serialized output consistent with the unsigned decimal wire format.
Floats, NaN, and infinity are rejected by the
Python constructors. Decimal wire values are strings, including scientific notation.
UTC timestamps include a `Z` suffix in serialized output. Event sequence is a
nonnegative integer; its source scope is a future connector design concern.

Event kinds currently cover trade, quote, and bar only. Envelope event type must
match its typed payload. Bar open/close fall between low/high. Crossed quotes
are rejected by this initial stub; future raw-feed/quarantine behavior is unspecified.
JSON Schema validates wire structure and numeric string syntax. Python constructors
also validate semantic numeric relationships; JSON Schema alone is insufficient
for positivity/OHLC/crossed-quote validation. Deserialization is not implemented.

Phase 1 adds optional `timestamp_basis` (exchange/receipt), `buyer_is_maker` on
trades, and UTC `open_ts` on bars. Unknown optional fields are omitted on output;
explicitly supplied fields must have their declared types. Receipt provenance
requires source and receive timestamps to agree. Identity is stable across
REST/WS for the same symbol/kind/sequence (and candle interval).
Wire numeric strings are bounded to 64 characters and exponent magnitude 1000
during decoding and Binance normalization.

The bounded FIFO bus is implemented. State reduction, risk policy, durable
storage, and order lifecycle remain unimplemented.
