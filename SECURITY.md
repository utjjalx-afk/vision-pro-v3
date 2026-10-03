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

Live trading and MT5 execution are deliberately unimplemented. Phase 1 startup
rejects attempts to enable either. An environment flag cannot make this foundation
eligible for live trading. Future execution requires a separate design and review.
