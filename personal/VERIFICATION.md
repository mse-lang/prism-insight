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
