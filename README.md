# my-pokemons-web agent

Automatically feeds, plays with, and cares for your Pokémon on
[my-pokemons-web](https://my-pokemons-web.vercel.app). Runs on a schedule via
GitHub Actions — no server, no dependencies.

## What it does, each run

1. Logs in (NextAuth credentials) → session cookie.
2. Discovers the current server-action IDs from the app's JS (self-healing — see
   below).
3. Reads `GET /api/collection` and `GET /api/me`.
4. For every Pokémon: **feeds** it if its feed cooldown has passed, **plays**
   with it if its play cooldown has passed, and **revives** it if fainted.
5. **Claims the daily gift** when available.
6. Prints a one-line summary and exits non-zero if any action truly failed.

## How the game works (reverse-engineered)

| Thing | How |
|---|---|
| Auth | NextAuth: `POST /api/auth/callback/credentials` → `__Secure-authjs.session-token` (~30-day) |
| Read all pets | `GET /api/collection` — fullness, mood, heart, cooldowns, isFainted |
| Read account | `GET /api/me` — coins, daily gift status |
| Feed / play / revive / gift | React **Server Actions**: `POST /collection/{id}` with header `Next-Action: <id>`, body `["<pokemonId>"]` |
| Cooldowns | feed **30 min**, play **20 min** (both refill the stat to 100) |
| Action result | flight body contains `{"ok":true}` or `{"ok":false,"error":{"code":"COOLDOWN",...}}` |

### Self-healing action IDs

The `Next-Action` IDs are content hashes that **change every time the game is
redeployed**. Rather than hardcode them, the agent scrapes the current IDs out
of the page's JS chunks on each run (`discover_actions()`), matching them by
function name (`feedAction`, `playAction`, …). If scraping ever fails it falls
back to the last-known IDs in `FALLBACK_ACTIONS`. This is the main reason the
agent needs almost no maintenance.

## Run locally

```bash
export PKMN_EMAIL="you@example.com"
export PKMN_PASSWORD="your-password"

python3 agent.py            # do it for real
DRY_RUN=1 python3 agent.py  # just print the plan, change nothing
```

No packages to install — Python 3 standard library only.

## Deploy on GitHub Actions (recommended)

1. Create a **private** GitHub repo and push these files:
   ```bash
   git init && git add . && git commit -m "pokemon care agent"
   git branch -M main
   git remote add origin git@github.com:<you>/my-pokemons-web-agent.git
   git push -u origin main
   ```
2. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**, add:
   - `PKMN_EMAIL`
   - `PKMN_PASSWORD`
3. Done. `.github/workflows/play.yml` runs every 10 minutes. Trigger a test run
   any time from the **Actions** tab → *play-pokemons* → *Run workflow*.

**Failure alerts:** GitHub emails you automatically when a scheduled workflow
fails, so you'll hear about it only when something actually breaks.

**Notes**
- Use a **private** repo (the workflow logs your Pokémon names/stats; secrets are
  redacted regardless).
- Scheduled Actions can be delayed a few minutes under load — harmless here
  because cooldowns are 20–30 min and the next run always catches up.
- Cost: each run is ~15 s; ~144 runs/day stays well within the free Actions
  minutes for a private repo (public repos are unlimited).

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `PKMN_EMAIL` | — | account email (required) |
| `PKMN_PASSWORD` | — | account password (required) |
| `PKMN_BASE_URL` | `https://my-pokemons-web.vercel.app` | game origin |
| `DRY_RUN` | — | `1` = plan only, perform nothing |
