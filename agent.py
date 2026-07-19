#!/usr/bin/env python3
"""
my-pokemons-web autoplayer.

Logs into the game (NextAuth credentials), reads the collection, and:
  - feeds every Pokemon whose feed cooldown has passed
  - plays with every Pokemon whose play cooldown has passed
  - revives any fainted Pokemon
  - claims the daily gift when available

Stdlib only -- no dependencies to keep updated.

The game's feed/play/revive/gift buttons are React Server Actions whose IDs are
content hashes that change every time the game is redeployed. Instead of
hardcoding them, we scrape the current IDs out of the page's JS chunks on every
run, so the agent self-heals across the game's deploys. See discover_actions().

Config via environment variables:
  PKMN_EMAIL     (required)
  PKMN_PASSWORD  (required)
  PKMN_BASE_URL  (optional, default https://my-pokemons-web.vercel.app)
  DRY_RUN        (optional, "1" to plan actions without performing them)
"""

import datetime
import http.cookiejar
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("PKMN_BASE_URL", "https://my-pokemons-web.vercel.app").rstrip("/")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

# Fallback action IDs, used only if runtime discovery fails. These WILL go stale
# when the game is redeployed; discovery is the primary path.
FALLBACK_ACTIONS = {
    "feedAction": "40115f17cb11d2b0ccbcc5580fe6d219a4d8360ed2",
    "playAction": "4088859f69ca19214b325c856b174d972ef702ac71",
    "reviveAction": "40ca9e11e4da0e2835abb417486044dcb005bba0cb",
    "claimDailyGiftAction": "00d65b8e763404c26d064129e65497a9bc7279f886",
}


def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


class Client:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )

    def _open(self, req, retries=2):
        """Open a request, retrying transient network/5xx failures.

        Serverless (Vercel) cold starts and blips occasionally return a 5xx or a
        dropped connection; a couple of quick retries make cron runs reliable.
        """
        last = None
        for attempt in range(retries + 1):
            try:
                resp = self.opener.open(req, timeout=30)
                return resp.getcode(), resp.read().decode("utf-8", "ignore")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")
                if e.code < 500 or attempt == retries:
                    return e.code, body
                last = (e.code, body)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == retries:
                    raise
                last = (0, str(e))
            time.sleep(1 + attempt)
        return last

    def get(self, path):
        return self._open(urllib.request.Request(BASE_URL + path))

    def get_json(self, path):
        """GET and parse JSON, returning (code, obj-or-None)."""
        code, text = self.get(path)
        if code != 200:
            return code, None
        try:
            return code, json.loads(text)
        except json.JSONDecodeError:
            return code, None

    def post_form(self, path, data):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            BASE_URL + path, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._open(req)

    def call_action(self, path, action_id, args):
        """Invoke a React Server Action (POST with Next-Action header).

        Returns (status, detail) where status is one of:
          "ok"       - action succeeded
          "cooldown" - benign: action was on cooldown (race with our own check)
          "error"    - a real failure; detail carries the message
        The action's return value is serialized in the RSC flight body as a line
        like  1:{"ok":true,...}  or  1:{"ok":false,"error":{...}}.
        """
        body = json.dumps(args).encode()
        req = urllib.request.Request(
            BASE_URL + path, data=body,
            headers={
                "Next-Action": action_id,
                "Content-Type": "text/plain;charset=UTF-8",
            },
        )
        http_code, text = self._open(req)
        if http_code != 200:
            return "error", f"HTTP {http_code}"
        result = self._parse_flight_result(text)
        if result is None:
            return "error", "unrecognized response"
        if result.get("ok"):
            return "ok", ""
        err = result.get("error", {}) or {}
        if err.get("code") == "COOLDOWN":
            return "cooldown", ""
        return "error", err.get("message") or json.dumps(result)[:120]

    @staticmethod
    def _parse_flight_result(text):
        """Pull the {"ok":...} object out of an RSC flight response."""
        for line in text.splitlines():
            _, _, payload = line.partition(":")
            payload = payload.strip()
            if payload.startswith("{") and '"ok"' in payload:
                try:
                    return json.loads(payload)
                except json.JSONDecodeError:
                    continue
        return None


def login(c):
    code, obj = c.get_json("/api/auth/csrf")
    if not obj or "csrfToken" not in obj:
        raise RuntimeError(f"csrf failed: HTTP {code}")
    csrf = obj["csrfToken"]
    email = os.environ["PKMN_EMAIL"]
    password = os.environ["PKMN_PASSWORD"]
    c.post_form(
        "/api/auth/callback/credentials",
        {"csrfToken": csrf, "email": email, "password": password, "json": "true"},
    )
    code, obj = c.get_json("/api/auth/session")
    user = (obj or {}).get("user")
    if not user:
        raise RuntimeError(
            f"login failed: no session (HTTP {code}; check PKMN_EMAIL/PKMN_PASSWORD)"
        )
    log(f"logged in as {user.get('name')} <{user.get('email')}>")


def discover_actions(c):
    """Scrape current server-action IDs from the collection page's JS chunks."""
    code, html = c.get("/collection")
    if code != 200:
        log(f"WARN: could not load /collection (HTTP {code}); using fallback IDs")
        return dict(FALLBACK_ACTIONS)
    chunks = sorted(set(re.findall(r"/_next/static/chunks/[^\"\\]+\.js", html)))
    actions = {}
    pat = re.compile(
        r'createServerReference\)?\("([0-9a-f]+)",[^,]+,[^,]+,[^,]+,"([^"]+)"'
    )
    for ch in chunks:
        _, text = c.get(ch)
        for m in pat.finditer(text):
            actions[m.group(2)] = m.group(1)
    # Fill any gaps from the fallback table.
    for name, aid in FALLBACK_ACTIONS.items():
        actions.setdefault(name, aid)
    log("discovered actions: " + ", ".join(sorted(actions)))
    return actions


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def parse_ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def run():
    c = Client()
    login(c)
    actions = discover_actions(c)

    code, collection = c.get_json("/api/collection")
    if not isinstance(collection, list):
        raise RuntimeError(f"collection fetch failed: HTTP {code}")

    _, me = c.get_json("/api/me")
    me = me or {}

    now = now_utc()
    fed = played = revived = failed = 0

    def do(path, action_name, args, label):
        """Run an action, log the outcome, and return 1 on success else 0."""
        nonlocal failed
        if DRY_RUN:
            return 1
        status, detail = c.call_action(path, actions[action_name], args)
        if status == "ok":
            return 1
        if status == "cooldown":
            return 0  # benign: cooldown ticked over between read and write
        failed += 1
        log(f"  {label} failed: {detail}")
        return 0

    for p in collection:
        pid = p["id"]
        name = p.get("nickname") or p.get("pokemon", {}).get("name", pid)

        if p.get("isFainted"):
            log(f"{name}: fainted -> revive")
            revived += do(f"/collection/{pid}", "reviveAction", [pid], "revive")
            continue  # skip feed/play this run; next run handles the revived pet

        if parse_ts(p["feedCooldownEndsAt"]) <= now:
            log(f"{name}: feeding (fullness {p['currentFullness']:.0f})")
            fed += do(f"/collection/{pid}", "feedAction", [pid], "feed")

        if parse_ts(p["playCooldownEndsAt"]) <= now:
            log(f"{name}: playing (mood {p['currentMood']:.0f})")
            played += do(f"/collection/{pid}", "playAction", [pid], "play")

    gift = me.get("dailyGift", {})
    if gift.get("availableNow") and not DRY_RUN:
        log("daily gift available -> claim")
        c.call_action("/collection", actions["claimDailyGiftAction"], [])
        # Verify by state rather than trusting the flight shape, which we haven't
        # observed: if it's still claimable afterwards, the claim really failed.
        _, after = c.get_json("/api/me")
        if (after or {}).get("dailyGift", {}).get("availableNow"):
            failed += 1
            log("  gift claim failed: still available after claim")

    log(
        f"summary: {len(collection)} pokemon | fed {fed} | played {played} | "
        f"revived {revived} | coins {me.get('coins', '?')} | failures {failed}"
        + (" | DRY_RUN" if DRY_RUN else "")
    )
    return failed


def main():
    for var in ("PKMN_EMAIL", "PKMN_PASSWORD"):
        if not os.environ.get(var):
            print(f"ERROR: {var} not set", file=sys.stderr)
            return 2
    try:
        failed = run()
    except Exception as e:  # noqa: BLE001 - top-level guard for cron visibility
        # Print the full traceback so a failed cron run is debuggable from the
        # Actions log without needing to reproduce it.
        print(f"ERROR: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
