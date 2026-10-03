# Phase-0 contract stubs

`schemas/canonical-market-event.schema.json` and `schemas/instrument-spec.schema.json`
are JSON Schema Draft 2020-12 wire stubs. Python counterparts are in
`vision.core.contracts`. These contracts are provisional pending the full frozen spec.

Use synthetic fixtures. Prices/increments/contract size must be positive finite
decimals; quantities may be zero. Floats, NaN, and infinity are rejected by the
Python constructors. Decimal wire values are strings, including scientific notation.
UTC timestamps include a `Z` suffix in serialized output. Event sequence is a
nonnegative integer; its source scope is a future connector design concern.

Event kinds currently cover trade, quote, and bar only. Envelope event type must
match its typed payload. Bar open/close fall between low/high. Crossed quotes
are rejected by this initial stub; future raw-feed/quarantine behavior is unspecified.
JSON Schema validates wire structure and numeric string syntax. Python constructors
also validate semantic numeric relationships; JSON Schema alone is insufficient
for positivity/OHLC/crossed-quote validation. Deserialization is not implemented.

The exact event bus, state reducer, risk policy, and order lifecycle remain unimplemented.
