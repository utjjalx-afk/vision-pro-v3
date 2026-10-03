# Security policy

Phase-0 has no supported production deployment or live execution path.
Only the latest Phase-0 commit is maintained while the foundation evolves.

Report vulnerabilities privately through GitHub's **Security -> Report a vulnerability**
when private reporting is enabled for this repository. If unavailable, request a
private reporting channel in a public issue without disclosing exploit details,
credentials, account identifiers, or sensitive logs. No private contact is embedded here.

Do not submit broker accounts, API tokens, database credentials, `.env` files,
terminal snapshots containing secrets, or production datasets. Use synthetic fixtures.
If a credential is exposed, revoke it at its provider before discussing the incident.

Live trading and MT5 execution are deliberately unimplemented. Phase-0 startup
rejects attempts to enable either. An environment flag cannot make this foundation
eligible for live trading. Future execution requires a separate design and review.
