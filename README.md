# Investor Helper

A Windows desktop app that reads market news, SEC filings, IPO and earnings
calendars, X posts and your Trading212 account, then tells you which stocks
look worth buying over the next few days and why.

**It never buys or sells anything.** There is no order code in the app, the
Trading212 client refuses any endpoint outside a read-only allow-list, the
browser tab it controls blocks any order request, and a test fails the build if
order or click code is ever added. You make every decision.

## What it does

| Area | How |
|---|---|
| News | Official outlets' RSS feeds (Reuters, CNBC, WSJ, MarketWatch, FT, BBC, SEC, Fed, BLS...) |
| X | Reads official news accounts and your Following timeline live through your own logged-in Chrome. Optional X API token as a fallback. |
| SEC EDGAR | Latest 8-K, 10-Q, 10-K, Form 4, 13D/13G, S-1 and 424B4 filings; 8-K item codes are scored (bankruptcy, delisting, restatement...). Revenue and net-income growth from XBRL "company facts". |
| IPOs | Nasdaq IPO calendar (upcoming, priced, filed) plus EDGAR S-1/424B4 filings |
| Events | Earnings dates for the next 5 days, dates found in news and X posts ("earnings call on Nov 19", "Fed meeting tomorrow"), watched pages (FOMC calendar, BLS schedule, any page you add) and your own events. You get a Windows notification 24 hours before, and whenever a watched page changes. |
| Trading212 | Account value, cash, invested, P/L and positions, read from the Trading212 web app in your Chrome (or the official API with a read-only key). History is charted. |
| Predictions | Per ticker: news sentiment x source reliability x recency, plus price momentum and fundamentals. Gives a predicted move and price over 1 to 10 days (default 3) with a confidence score. |
| Learning | After the horizon passes, each prediction is graded against the real price. Every source and factor that pointed the right way gains weight, wrong ones lose it. Sources that stay wrong are switched off and replaced by a vetted alternative, and X accounts your best sources keep citing are suggested to you. |
| Report | A numbered "thread" report: buy ideas with predicted prices, caution list, your account, upcoming events, IPOs, notable filings, source scoreboard. Saved in the app and as Markdown files. |

## Install

**Build it yourself (no GitHub needed):** unzip the source, double-click
`BUILD_EXE.bat`. It installs Python 3.12 if missing (via winget), runs the
tests and leaves `InvestorHelper.exe` in the same folder.

**Or from GitHub:**

1. Download `InvestorHelper.exe` from the latest GitHub Actions run (Actions tab,
   "Build Windows EXE", Artifacts) or from Releases.
2. Put it anywhere and double-click it. Windows SmartScreen may warn because the
   EXE is unsigned: click "More info", then "Run anyway".
3. It lives in the system tray. Closing the window keeps it running; right-click
   the tray icon to quit.

## Connect your Chrome (X and Trading212)

The app reads X and Trading212 as you, through your normal Chrome profile.

1. Close every Chrome window.
2. Run `packaging/Start Chrome for Investor Helper.bat`, or start Chrome with
   `--remote-debugging-port=9222 --profile-directory=Default`.
3. Make sure you are logged in to x.com and app.trading212.com in that Chrome.
4. In Investor Helper, open Settings and press **Test Chrome connection**.

The app opens one background tab, reads, and closes it. It does not touch your
own tabs, never clicks, and never types.

> Chrome 136 and later ignore the debug port on the default profile for
> security reasons. If you update Chrome past 135, start it with a separate
> profile folder instead, e.g. `--user-data-dir="%LOCALAPPDATA%\InvestorHelperChrome"`,
> and log in to X and Trading212 once in that window.

## Optional keys (Settings tab)

All keys are stored only in `%APPDATA%\InvestorHelper\settings.json` on your PC.

* **Trading212 API key**: Trading212 app, Settings, API (Beta). Invest account,
  read permissions only (Account data, Portfolio). If set, it is used instead of
  Chrome for the account value. Paste the secret too if Trading212 gave you one.
* **X API bearer token**: only used if Chrome is not reachable.
* **SEC User-Agent**: put your name and email; the SEC asks for this.

## Using the report

* **Buy ideas** are ranked by predicted move times confidence. Columns show the
  news, momentum and fundamentals parts so you can see why.
* Confidence is low until the app has graded its first predictions (after the
  horizon, 3 days by default). The track record is at the top of each report.
* This is statistics on public information, not financial advice. Expect many
  calls to be wrong; the point is to surface things worth your own research.

## Your data

Everything is in `%APPDATA%\InvestorHelper`: the SQLite database, `sources.json`
(edit sources in the Sources tab), saved reports, `rotation_log.json` (why a
source was swapped) and `investor_helper.log`.

## Developing

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m investor_helper            # open the window
python -m investor_helper --once     # one headless refresh, prints the report
pyinstaller packaging/InvestorHelper.spec   # build the EXE (on Windows)
```

Pushing to `main` builds the EXE on a Windows runner; pushing a `v*` tag also
publishes a release.

Layout: `investor_helper/sources` (fetchers, all read-only),
`investor_helper/analysis` (sentiment, tickers, scoring, learner, market hours),
`engine.py` (one refresh cycle), `report.py`, `gui/app.py`.
