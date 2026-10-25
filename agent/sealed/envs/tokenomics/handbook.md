# Token design stress test: notes for the proposer

You are designing a token launch. 1B tokens launch at $0.10 (FDV $100M) into one constant-product
(x*y=k) pool, seeded by the treasury plus $4M of mercenary outside LP money, and trade daily for 18 months.

**Supply.** Team 18% (cost basis $0.005), investors 17% ($0.02), ecosystem emissions pool 30%,
treasury 15% (tokens for the pool), community float at launch set by you.

**Who sells.**
- Team and investors sell part of every unlock, more when the price is far above their cost
  basis (investors at 5x sell about half). They work off what they plan to sell at about 15% a
  day, so a big cliff or a quarterly lump arrives as a sell wave.
- Farmers sell 60% of emissions at once; emissions also add organic buyers.
- Community holders trade toward fair value, which follows the sector market; once the price
  falls more than 40% below fair value they start panic-selling, which feeds on itself.

**Treasury.** The raise is $6M + $8M x (1 - (investor cliff + investor vest / 2) / 36):
shorter investor locks raise more. It pays $150k a month of opex, the pool depth you choose,
and any buybacks. Buybacks burn the tokens bought; the reserve months are never spent on them.

**Score (0-100, higher is better), mean over seeds.**
35 x (1 - max drawdown against the market) + 15 x stability (annual vol vs 200%)
+ 25 x liquidity (worst pool USD depth vs $3M) + 25 x runway (treasury left vs 72 months).

**Levers and trade-offs.**
- Deeper pools absorb unlocks and shocks but cost runway.
- Daily unlocks smooth sell pressure; cliffs and long cadences concentrate it.
- Buybacks defend drawdowns but can drain the treasury in a prolonged fall; a reserve caps that.
- The dev score is a calm market. A design that only just clears the bar in calm conditions
  may have no margin when conditions change.
