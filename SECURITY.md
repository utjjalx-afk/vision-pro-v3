# Security policy

The data foundation has no supported production execution deployment or live execution path.
Only the latest foundation commit is maintained while the project evolves.

Report vulnerabilities privately through GitHub's **Security -> Report a vulnerability**
when private reporting is enabled for this repository. If unavailable, request a
private reporting channel in a public issue without disclosing exploit details,
credentials, account identifiers, or sensitive logs. No private contact is embedded here.

Do not submit broker accounts, API tokens, database credentials, `.env` files,
terminal snapshots containing secrets, or production datasets. Use synthetic fixtures.
If a credential is exposed, revoke it at its provider before discussing the incident.

Live trading, MT5 and autonomous agents are deliberately unimplemented. Phase 10
startup rejects attempts to enable them. Portfolio inputs are caller-supplied
research records; READY never authorizes live execution. The explicit paper API
uses local synthetic/supplied data only. Checkpoint digests are integrity checks,
not signatures; untrusted journals must not be treated as authenticated evidence. An environment flag cannot make this foundation
eligible for live trading. Future execution requires a separate design and review.

Phase-10 OANDA credentials remain runtime-only; GET endpoints are restricted to
market data on fixed provider hosts, with redirects rejected and errors redacted.
Do not publish account identifiers, authorized raw responses or tokens. Calendars
and verified InstrumentRecords are caller attestations, not authenticated broker
truth. Market closure does not make old quotes fresh, and unknown coverage blocks
admission. Synthetic FX/metals fixtures and failover stubs are not real providers
or execution permission. The adapter performs no account-state or order requests.

Phase-5 lanes have no order submission or LLM control path. Macro/Narrative signals
are labelled replay fixtures with zero predictive confidence, not verified news.
Canonical context hashes check consistency, not authenticity or trading edge.

The durable ResearchJournal verifies prospective registration timing and pinned
evidence with committed local SQLite receipts. This does not authenticate provider
data, the host clock, or a database/export replaced by its filesystem owner.
Hash chains and append-only SQL triggers are consistency controls, not signatures.
Exports can contain supplied research metadata; review them before publishing.
Fixture outcomes cannot enable reliability.
TradeIntent is unsized and candidate-only; strategy eligibility and risk validation
are still required before any future execution path. Directional synthesis is
research plumbing and does not demonstrate trading edge.

Backtests use an explicit offline frozen rule and synthetic execution quotes.
Bars, regime labels and trial-universe declarations remain supplied inputs.
Audits are bounded consistency checks, not authentication or an arbitrary-code
sandbox. PASS authorizes no execution/promotion. Result replay never imports
historical profit into prospective reliability.

Strategy DSL accepts structured JSON only; it never evaluates code or calls a model.
Schema validity is separate from supported compilation, preview confirmation and
research eligibility. GENERATED_DRAFT cannot self-approve. Preview confirmations
are local attestations bound to artifact/dataset/compiler, not authenticated human
identity or deployment authority. Higher lifecycle labels cannot enable execution.
