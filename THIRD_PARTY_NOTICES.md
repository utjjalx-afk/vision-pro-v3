# Third-party notices

Phase-0 source was authored afresh. No trading engine, strategy, adapter, old
Vision Pro source, or third-party repository history has been copied or vendored.
The architecture draws on general event-driven, intent/risk separation,
connector-boundary, replay, and research-governance ideas discussed in the design.
Referencing these ideas does not imply incorporating those projects' code.

The LICENSE file is the unmodified Apache License 2.0 text from
<https://www.apache.org/licenses/LICENSE-2.0.txt>.

Phase 1 adds `websockets` alongside Python's standard library. Runtime and
development/build tools are installed separately rather than vendored:

| Tool | Upstream | License |
| --- | --- | --- |
| Python | https://www.python.org/ | PSF License |
| websockets | https://github.com/python-websockets/websockets | BSD-3-Clause |
| Hatchling | https://github.com/pypa/hatch | MIT |
| pytest | https://github.com/pytest-dev/pytest | MIT |
| Ruff | https://github.com/astral-sh/ruff | MIT |
| jsonschema | https://github.com/python-jsonschema/jsonschema | MIT |
| tzdata | https://github.com/python/tzdata | Apache-2.0; underlying IANA data public domain |
| MetaTrader5 (optional Windows Python package) | https://www.mql5.com/en/docs/python_metatrader5 | MIT per package metadata; not vendored |
| NumPy (SDK dependency) | https://numpy.org/ | BSD-3-Clause |

The separately installed MetaTrader terminal and broker services retain their own
terms; the optional Python package license does not relicense those products.

CI uses GitHub-maintained checkout/setup-python actions. Container scaffolding
references the official Python image. Those tools and images retain their own
licenses and transitive notices; they are not relicensed by this repository.
Review and extend this inventory before adding or distributing dependencies.

Phase 10 uses packaged IANA timezone rules and independently authored OANDA wire
normalization against official API documentation. No provider market history or
third-party adapter source was copied; FX/metals test fixtures are synthetic.


## Command Center chart library

TradingView Lightweight Charts 5.0.7 is distributed under Apache-2.0.
TradingView Lightweight Charts(TM)
Copyright (c) 2025 TradingView, Inc. https://www.tradingview.com/
The frontend retains library attribution and a TradingView link. The installed
package includes its LICENSE and NOTICE; see the pinned npm lockfile.
React / ReactDOM are MIT licensed. No Vardhan application or Pine code is included.
