# Next-Day Gas Price Forecast

A self-updating web page that predicts tomorrow's **AAA national average, regular unleaded**.

Every morning a GitHub Action fetches the new AAA number, appends it to the history file,
recomputes the forecast, and republishes the page. No laptop, no server, no subscription —
it runs on GitHub's machines and costs nothing.

---

# Part 1 — Getting it online

You need a free GitHub account. Total time: about ten minutes, no terminal required.

### 1. Make a GitHub account

Go to **[github.com/signup](https://github.com/signup)**. Email, password, username, done.
Your username becomes part of your page's address, so pick something you don't mind seeing.

### 2. Create the repository

Click the **+** in the top right → **New repository**.

- **Repository name:** `gas-forecast`
- **Public** ← this matters. GitHub Pages requires a public repo on the free plan.
  There is nothing private in here — no keys, no personal data.
- Leave everything else alone. Click **Create repository**.

### 3. Upload the files

On the empty repo page, click **uploading an existing file**.

Drag in **everything** from the `gas-forecast` folder: `index.html`, `template.html`,
`build.py`, `test_build.py`, `history.csv`, `requirements.txt`, and `README.md`. All files
sit at the top level except one — `.github/workflows/update.yml` — which has to land at
exactly that path, so create it separately via **Add file → Create new file** and type the
full path as the filename (the slashes create the folders).

Scroll down, click **Commit changes**.

### 4. Turn the page on

**Settings** → **Pages** in the left sidebar.

- Under *Build and deployment*, set **Source** to **Deploy from a branch**
- **Branch:** `main`, folder: `/ (root)` → **Save**

Wait a minute or two, then reload the Settings → Pages screen. Your URL appears at the top:

```
https://YOUR-USERNAME.github.io/gas-forecast/
```

That's the link. Open it anywhere.

### 5. Let the robot write to the repo

**Settings** → **Actions** → **General** → scroll to *Workflow permissions* → select
**Read and write permissions** → **Save**.

Without this the daily job can fetch the new price but can't commit it, and every run
fails at the last step.

### 6. Prove it works

**Actions** tab → **Update forecast** in the left sidebar → **Run workflow** →
**Run workflow**.

Give it about a minute. Green check = the whole pipeline works: it fetched AAA, updated
`history.csv`, rebuilt `index.html`, and pushed. Your page will show the new numbers
within a minute or so.

Red X = click into the run and read the log. The script prints exactly what it parsed and
why it refused to continue.

### 7. Put it on your phone

Open the URL in Safari → **Share** → **Add to Home Screen**. It gets its own icon and
opens without browser chrome, like an app. Chrome on Android: **⋮** → **Add to Home screen**.

---

# Part 2 — How it keeps itself current

`.github/workflows/update.yml` runs `build.py` twice every day, at about 7:20am and
12:20pm Eastern. Two runs because AAA publishes overnight and GitHub's scheduler is often
10–20 minutes late; the second run is a free retry. Running twice is harmless — if nothing
changed, nothing gets committed.

Each run:

1. Fetches `gasprices.aaa.com` and parses the national-average table
2. Sanity-checks the number (see below), then writes it to `history.csv`
3. Also records *yesterday's* value from the same page — so one missed run self-heals
4. Fetches EIA wholesale prices for context (optional; a failure here is not fatal)
5. Regenerates `index.html` from `template.html`
6. Commits and pushes, which republishes the page

### It refuses to publish a number it doesn't trust

Scrapers break silently, and a forecast built on a misparsed number is worse than no
forecast. So `build.py` fails loudly instead:

- Parse failure → exits non-zero, the Action goes red, GitHub emails you. The page keeps
  showing the last good data.
- Price outside $1.00–$12.00 → rejected. That's not a national average, that's a parsing bug.
- Price more than $0.25 from the last recorded value → rejected. A 120,000-station rolling
  survey cannot move that fast. (If it ever legitimately does, raise `MAX_DAILY_JUMP`.)
- Page goes more than a day stale → an orange banner appears at the top saying so, rather
  than quietly presenting an old forecast as today's.

Run the test suite any time with `python test_build.py` — it exercises the parser against
several markup shapes and confirms every one of those guards actually fires.

### Two things worth knowing

**GitHub pauses cron jobs on quiet repos.** Scheduled workflows stop after 60 days without
repository activity. GitHub emails you first, and re-enabling is one click in the Actions
tab.

**AAA could restructure their page.** The parser keys off the row labels ("Current Avg.",
"Yesterday Avg.") rather than the HTML structure, which has been stable for years, and
there's a positional fallback behind it. If it ever does break, you'll get a red-X email
the same morning rather than discovering it weeks later.

---

# Part 3 — The model

```
change_tomorrow = drift + 0.871 × (change_today − drift)
price_tomorrow  = price_today + change_tomorrow
```

where `drift = (price_today − price_week_ago) / 7`.

Daily AAA changes behave like an AR(1) with φ ≈ 0.871: **tomorrow repeats about 87% of
today's move**, pulled slightly toward the week's pace. High persistence is exactly what
you'd expect from a ~120,000-station rolling survey — the number is heavily smoothed, so
it drifts in runs rather than jumping.

### Where 0.871 came from

There is no free bulk source for daily AAA history, so φ was derived from 36 years of
*weekly* EIA retail prices (1,862 observations, 1990–2026):

1. Lag-1 autocorrelation of weekly changes = **0.548** — and remarkably stable by era
   (0.532 for 2021+, 0.538 for 2015+, 0.547 for 2005+).
2. A weekly change is the sum of 7 daily changes. Inverting the AR(1) temporal-aggregation
   formula for that sum gives daily **φ = 0.871**. Across all four eras: 0.864–0.871.
3. Monte Carlo check on 1.4M simulated daily steps reproduced the weekly autocorrelation
   to within 0.003.

### Does it work?

Walk-forward on the weekly series — 2-year burn-in, expanding window, coefficients
re-estimated at every step, **1,756 out-of-sample weeks** (1992–2026):

| | Persistence model | Naive no-change |
|---|---|---|
| MAE | **$0.0279** | $0.0339 |
| RMSE | **$0.0450** | $0.0534 |
| Direction hit rate | **68.6%** | — |

That's 17.8% better than assuming no change. At *daily* frequency the edge should be larger,
because φ is 0.871 daily versus 0.548 weekly — for a Gaussian AR(1) the expected MAE ratio
is √(1−φ²) ≈ **0.49**, roughly half the error. That figure stays theoretical until enough
daily history accumulates to measure it directly, which is what the daily logger is for.

### "How close will it land?"

The page shows exact-hit and near-miss probabilities:

```
P(published value rounds to v at d decimals) = Φ((v+h−μ)/σ) − Φ((v−h−μ)/σ),  h = ½·10⁻ᵈ
```

Exact-to-four-decimals is always a tiny number (~0.4%) — that measures AAA's publishing
precision, not the model's skill, and a perfect forecaster would score the same. The
"within 1¢" figure is the one that tracks whether the model is actually working.

### Known limits

1. **φ is inferred, not directly fit.** Backed out from weekly data. Tight band, solid
   reasoning, still an inference until daily history confirms it.
2. **The ± band is provisional.** σ = $0.0103 came from only 9 observed daily changes.
   Treat the intervals as approximate for now. This tightens on its own as the log grows.
3. **Pure momentum.** The model does not read the news. It will be wrong on the day a
   hurricane, refinery outage, or geopolitical shock hits — precisely the days momentum
   breaks.
4. **Wholesale is context only.** WTI/RBOB are displayed but not in the equation. Adding
   crude to the weekly model was tested over 1,121 out-of-sample weeks and made it slightly
   *worse* — weekly retail momentum already absorbs the wholesale signal. Whether it helps
   at a one-day horizon is an open question that needs daily history to answer.

---

# Part 4 — The Market Flow page (`edge.html`)

A second page comparing the model against real money, at
`https://YOUR-USERNAME.github.io/gas-forecast/edge.html`. Rebuilt every 30 minutes by
`.github/workflows/flow.yml`.

### What "whale tracking" can and cannot mean here

**Kalshi publishes no trader identity.** Not an account, not a wallet, not a pseudonym —
the public trade feed carries only size, side, price and time. So you cannot follow a
named trader on this market, and any product claiming otherwise is guessing. Polymarket
*does* expose wallet-level data (it is on-chain), but it lists no gasoline markets at all.

What you get instead:

1. **Model vs. market at every strike.** Kalshi's daily ladder (`KXAAAGASD`) is a full
   implied probability distribution. Overlaying the model's on top of it shows exactly
   where the two disagree, and by how much.
2. **Large prints.** Every trade above the size threshold, plus every block trade. Whale
   footprints without the whale's name.
3. **CFTC concentration.** The genuine big-money dataset: the net position of the largest
   4 and largest 8 reportable traders in RBOB Gasoline and WTI, as a share of open
   interest, plus hedge-fund ("managed money") net length and its weekly change. Weekly,
   official, free.
4. **A scoreboard.** Every strike is logged with both probabilities to `market_log.csv`
   and scored with a Brier score once AAA publishes.

### Read the edge honestly

The first live comparison had the market pricing "above $4.1050" at 25¢ while the model
said 49% — a 24-point gap. That is **not** free money. It almost certainly means the
model's σ ($0.0103) is too wide: the market's implied distribution is far tighter than
the model's, and the market has real money behind it every single day.

This is exactly what the scoreboard is for. If after a few months the model's Brier score
cannot beat the market's, the model is not finding an edge no matter how good the
backtest looked. The page says so on its face rather than flattering the model.

**None of this is investment advice, and the tool places no trades.**

---

## Files

| File | What it is |
|---|---|
| `index.html` | The published page. Generated — don't edit by hand. |
| `template.html` | The real source. Edit this. |
| `build.py` | Fetches, validates, updates history, renders `index.html` + `snapshot.json`. |
| `model.py` | The forecast equation, shared by both pages so they can never disagree. |
| `flow.py` | Fetches Kalshi + CFTC, scores the model against the market, renders `edge.html`. |
| `edge_template.html` | Source for the flow page. Edit this, not `edge.html`. |
| `market_log.csv` | Every logged strike with model and market probabilities, settled once AAA publishes. |
| `test_build.py` | Offline tests for the parser and its guards. `python test_build.py` |
| `test_flow.py` | Offline tests for the Kalshi/CFTC parsing and the scoreboard. |
| `history.csv` | The accumulating daily AAA series. The asset that makes this better over time. |

## Data sources

- [AAA Fuel Gauge Report](https://gasprices.aaa.com/) — daily national average, no key required
- [EIA Today in Energy: Daily Prices](https://www.eia.gov/todayinenergy/prices.php) — WTI, Brent, RBOB spot
