# Verified Results — standalone marketing site

A single self-contained `index.html` that displays **live, broker-verified trading
results** (total P&L, today's live P&L, equity curve, daily-P&L calendar, win rate,
profit factor). It is **completely separate** from the KhomaAPI trading app — its
only connection is reading one public, read-only, cached JSON feed:

```
https://app.khomaapi.com/verified/data
```

That feed is capped to ~1 database read every 2 seconds regardless of how many
people view the site, so this page **cannot affect live trading clients** in any way.

## Host it (pick one — all free)

**Cloudflare Pages (recommended — you already use Cloudflare):**
1. Cloudflare dashboard → **Workers & Pages** → **Create** → **Pages** → **Upload assets**.
2. Drag in this `index.html` (and `README.md` if you like).
3. Deploy → you get a `*.pages.dev` URL.
4. Add a **custom domain** (e.g. `results.khomaapi.com` or `verified.khomaapi.com`)
   in the Pages project → Custom domains.

**Or:** Netlify drop (drag the folder to app.netlify.com/drop), GitHub Pages, or any
static host. It's just one HTML file.

## Customize
Open `index.html` and edit the top of the `<script>`:
- `var API = "...";` — the data feed URL (leave as-is unless the app domain changes).

Everything else (display name, colors) comes from the feed / CSS variables at the top
of the `<style>` block (`--green`, `--red`, `--bg`, …).

The **display name** shown at the top is controlled on the server via the
`PUBLIC_TRACK_NAME` env var, and **which account(s)** are published via
`PUBLIC_TRACK_ACCOUNT_IDS` — ask the KhomaAPI admin to set those.
