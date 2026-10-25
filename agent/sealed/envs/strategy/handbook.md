# SOL/USDC strategy research: domain notes

- **Data.** Real daily SOL/USD closes from Kraken's public OHLC API, 2024-11-04 to 2026-10-24.
  Each dev seed is a 60-day trading window that starts on that day index of the series. All dev
  windows lie in 2025. Rules may use up to 60 days of history before the window starts.
- **Book.** The book starts with $100 in USDC. Each day the rule sets a target SOL weight. The
  book trades only when its weight drifts more than `rebalance_band` from the target, and every
  swap pays 10 bp in fees plus 10 bp in slippage on the traded value. The staked share of the SOL
  leg earns 7% a year. Idle USDC earns nothing.
- **Score.** The end value of the $100 minus half the maximum drawdown in dollars, averaged over
  windows. Higher is better. Sharpe, return, drawdown, fees, trade count and average SOL weight
  are reported alongside.
- **Rules.** `hold` keeps the risk-on weight. `momentum` goes risk-on after the price has risen by
  more than the threshold over the lookback and risk-off after it has fallen by more.
  `mean_reversion` goes risk-on when the price is more than the threshold below its lookback
  average and risk-off when it is more than the threshold above it. Between the thresholds the
  previous target is kept. `stop_loss_pct` forces the risk-off weight while SOL sits that far
  below its lookback high.
- **Known pitfalls.** Eleven overlapping windows are a small sample. A rule with many tuned knobs
  (a threshold, a stop and a band, each fitted to the 2025 path) can top the dev table and still
  fail on a later period, after a crash or when fees rise. Staking carry and a low-turnover rule
  are cheap and robust. All-in/all-out switching pays fees on every flip and leaves the book with
  no buffer against a gap move.
- **Sealed.** The final judgement uses windows and stress scenarios you never see. Prefer rules
  that you expect to work on a later market, not only on these windows.
