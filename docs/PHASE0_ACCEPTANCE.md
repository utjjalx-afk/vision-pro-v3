# Phase-0 acceptance record

This branch adds a tracked-file disclosure audit to CI and records the foundation's
review boundary. It does not start market-data ingestion or Phase-1 work.

## Foundation delivered on main

- Original README, complete Apache-2.0 LICENSE, security/contribution policies,
  third-party inventory, ignore rules, and placeholder-only environment example.
- Independent Python package/app boundaries, instrument/event contract stubs,
  and public JSON Schemas. No old source or history was imported.
- Native Windows/Linux setup/run/stop/check scripts; Linux scripts have executable
  Git modes and LF line endings.
- API/worker/dashboard container skeletons and Compose/development skeletons.
- Python lint, format, tests, wheel packaging, cross-platform script checks, and
  container validity/smoke checks in GitHub Actions.

## Verified locally before publication

- Ruff lint and formatting passed; 48 tests passed on Windows/Python 3.11.
- Offline native diagnostic/check/stop scripts passed.
- Wheel build and isolated wheel import/diagnostic passed outside the checkout.
- Tracked-file content audit found no high-confidence tokens, private keys,
  personal absolute paths, private-network URLs, credential-bearing URLs,
  private/generated tracked directories, or non-placeholder `.env.example` values.
- Both execution flags are false by default; enabled/ambiguous values fail closed.
- No executable broker or MT5 code, feed adapter, strategy, paper fills, or UI exists.

The pattern audit complements manual review and is not a comprehensive secret
detector. It prints file names and finding categories without echoing matched content.
CI adds Windows/Linux Python 3.11/3.12 and container evidence; results belong to
the checks attached to the current commit, rather than a static claim in this document.

## Outstanding baseline source

The complete frozen Architecture Spec v1 was absent from both the bounded
conversation preview and retrieved history. The architecture file records the
recoverable excerpt, audit decisions, platform requirement, and provisional
mapping explicitly. Full-spec preservation and exact folder/schema alignment
remain blocked pending the complete source text. No new architecture freeze is claimed.

Keep this PR in draft. Do not implement live/MT5 execution or promote to Phase-1
as a workaround for the missing source.
