# Personal desk verification — 2026-10-09

Baseline: upstream `53afbbb`. Extension: `personal-kr-paper`.

This is a separate, manual Korean paper ledger. Upstream screening, strategies,
BUY/SELL prompts, broker execution, production services and scheduled batches are
unchanged. Original automated-strategy compatibility and profitability are not
claims of this extension. No live order or channel message was used to test it.

The input and consumer path is public NAVER JSON/daily bars → `Market` validated
quotes → local HTTP API → `DeskStore` transactions → Korean desktop/mobile UI.
Missing prices remain unknown; synthetic demo prices are an explicit separate mode
and separate database. Optional PRISM reports run through a bounded subprocess
only after explicit activation and a UI request. Worker alerts are disabled.

Verified locally with Python 3.12:

- 31 standard-library tests passed. Accounting reconciles fees, weighted costs,
  partial/full sells, cash, realized and unrealized P&L. An interleaved committed
  order proves cash/holdings/ledger reads use one SQLite snapshot.
- Eight concurrent requests with one identifier produce one order. Changed
  economic payload is rejected; repeat requests retain the original memo.
- Cash and aggregate position limits, unknown held prices, invalid inputs,
  provider isolation, and persistence after reopening the database passed.
- HTTP tests exercised account, orders, reports, journal, static files and CSV,
  including same-origin/CSRF checks and spreadsheet-formula escaping.
- The optional AI bridge's success/failure/timeout/shutdown paths passed using
  offline fake workers; actual AI prerequisites were unavailable and paid AI
  execution remains unverified.
- Read-only NAVER smoke retrieved six valid default-watchlist quotes and 120
  Samsung daily bars. Quote basis was 2026-10-08; 2026-10-09 was the observation
  date. Closed-market snapshots are displayed with their source timestamp.
- Headless Edge browser smoke at 1440px and 390px passed buy, partial sell,
  stock search/addition, journal, settings persistence after reload, technical
  report, daily chart after account refresh and mobile page-width checks.
  Browser tests used an isolated synthetic account; the personal NAVER account
  starts at KRW 10,000,000 with no positions or seeded trades.

The built-in computer-control process could not initialize, so browser evidence
was obtained from the separately launched test browser and inspected locally.

The personal CI workflow runs the offline suite on Windows/Linux and Python
3.10/3.12. Remote CI status must be checked separately from these local results.
No original production-server deployment or first scheduled batch was performed.

## Automation extension — 2026-10-09

The personal page now has a separate explicitly activated automation lane. Manual
desk orders still use the local paper ledger. KIS paper/live automation uses broker
quotes, orderability, exact order identities and confirmed fills with account
quantity reconciliation. The original PRISM AI BUY/SELL pipeline is unchanged.
The strategy adoption record and source-path comparison are in
`AUTOTRADE_CONTRACT.md` and `AUTOTRADE_REVIEW.md`.

Pre-deployment verification with bundled Python 3.12:

- 78 offline standard-library tests passed, including the original personal suite.
- New worker/ledger tests cover completed-bar signals, current-price continuation,
  stale/closed quotes, buy-only daily limits, exits despite exhausted buy limits,
  manual holding isolation, stop/restart, independent background execution and
  duplicate servers. Initial daily equity and signal attempts survive restart.
- 13 broker-controller integration tests use a strict fake broker for accepted,
  partial, canceled, unknown and filled orders. Ownership is committed only after
  exact cumulative fill and account quantity checks. Unknown HTTP results survive
  restart and never submit again; exact user-supplied order IDs require matching
  account identity. In-flight stop and start/stop races are exercised.
- 23 KIS transport fixtures verify hosts/TR IDs, token concurrency, pagination,
  missing/ambiguous balances, no-margin orderability, holiday and actual trade-time
  proof, and submission-time freshness/09:00–15:20 checks. Authentication and
  order traffic are injected offline. No external KIS authentication/order was run.
- Live confirmation binds the saved configuration, strategy, position cap and
  account namespace. Missing/stale review tokens and non-boolean confirmations
  are rejected. Same-account locks cover different data directories for one OS user.
- Windows DPAPI storage round-trip and absence of a plaintext fake secret passed.
  On Unix the secret file is restricted to 0600. Credentials are excluded from GET,
  request logs, events and Git. Local databases were backed up before runtime changes.
- Headless Edge at 1440px and 390px passed the previous manual order/report/journal
  flows and the new settings persistence, read-only check, local start/stop, blocked
  unconnected-live start and explicit live review. Broker connection/live start
  were browser mocks; the server never received them. Stop remained clickable during
  a delayed read-only check. JavaScript syntax and page width checks passed.

Broker authentication, actual KIS paper/live fills, forward scheduled operation,
strategy holdout and profitability remain unverified without user credentials.
Local isolated first worker execution is observed; it is not a KIS scheduled-run
result. The current personal account will stay stopped on the updated server.
Exact-head remote CI and post-restart smoke are recorded separately in the delivery.
