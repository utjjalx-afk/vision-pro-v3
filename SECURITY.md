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

Live trading, MT5 and autonomous agents are deliberately unimplemented. Phase 5
startup rejects attempts to enable them. Portfolio inputs are caller-supplied
research records; READY never authorizes live execution. The explicit paper API
uses local synthetic/supplied data only. Checkpoint digests are integrity checks,
not signatures; untrusted journals must not be treated as authenticated evidence. An environment flag cannot make this foundation
eligible for live trading. Future execution requires a separate design and review.

Phase-5 lanes have no order submission or LLM control path. Macro/Narrative signals
are labelled replay fixtures with zero predictive confidence, not verified news.
Canonical context hashes check consistency, not authenticity or trading edge.
