# Flight Deck — web frontend

A premium "flight-deck instrument cluster" for the trading harness: Vite + React
+ TypeScript, Framer Motion, custom fonts. It talks to the FastAPI layer in
`agentic_trading_bot/api/`.

## Run it (two terminals)

**1. API** (from `agentic_trading_bot/`):

```
pip install -r requirements.txt
export API_TOKEN=dev-token-change-me        # or set in .env; if unset, an ephemeral one is logged
uvicorn api.server:app --host 127.0.0.1 --port 8000
```

**2. Frontend** (from `web/`):

```
npm install
cp .env.example .env                        # set VITE_API_TOKEN to the SAME value as API_TOKEN
npm run dev
```

Open http://localhost:5173. The dev server proxies `/api` to the API on :8000.

The token is required on every request. `/api/health` is open; everything else
returns 401 without the matching token, with the fix named in the UI.

## What is built

- **Design system**: tokens (deep ink + gold identity; green/red only for money
  and risk), Space Grotesk / Geist Sans / Geist Mono + JetBrains Mono with
  tabular numerals everywhere a number lives, glass cards with hairlines and a
  soft top highlight, gold focus ring and active-nav wash.
- **Motion** (Framer Motion, reduced-motion respected): 150ms route cross-fade +
  8px rise; values flash their semantic color once and count up over ~400ms; the
  cluster animates only when the regime or a meter changes; the kill switch
  pulses once on change. No looping glow.
- **Command page** with the **Instrument Cluster** as the hero: a regime dial
  (gold ring + needle), risk meters (gross exposure, daily/weekly drawdown),
  day P&L, circuit-breaker and kill state, plus net-liquidation, queue, holdout,
  positions, and a live activity feed.
- The remaining routes are styled stubs sharing the system (next pass).

## Build

```
npm run build      # tsc typecheck + vite production build
```
