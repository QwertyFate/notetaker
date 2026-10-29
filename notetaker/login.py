"""Sign the bot's Google account in once and save the session for the bot to reuse.

    python -m notetaker.login            # writes storage_state.json in the project folder

A Chrome window opens on Google's sign-in page. Sign in as the bot account; the session is
saved as soon as Google sets its login cookies, and the window closes by itself.

Uses the installed Google Chrome with Playwright's automation markers removed, because
Google refuses sign-ins from an obviously automated browser ("This browser or app may not
be secure"). meeto's own `meeto-auth` command runs into exactly that.
"""

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from notetaker.config import PROJECT_ROOT

STEALTH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Save the bot's Google login for Notetaker.")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "storage_state.json"))
    parser.add_argument("--profile", default=str(PROJECT_ROOT / ".bot-chrome-profile"),
                        help="Chrome profile folder used only for this sign-in")
    parser.add_argument("--timeout-minutes", type=int, default=15)
    args = parser.parse_args()
    out = Path(args.output)

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                args.profile, headless=False, channel="chrome", args=STEALTH_ARGS,
                ignore_default_args=["--enable-automation"], viewport=None,
            )
        except PlaywrightError as err:
            if "chrome" in str(err).lower():
                print("Google Chrome is not installed. Install it from https://www.google.com/chrome/ "
                      "and run this again (Google blocks sign-ins from Playwright's own Chromium).",
                      file=sys.stderr)
                return 1
            raise
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.google.com/signin")
        print("A Chrome window is open. Sign in as the bot's Google account there.", flush=True)

        deadline = time.time() + args.timeout_minutes * 60
        while time.time() < deadline:
            time.sleep(3)
            try:
                names = {c["name"] for c in ctx.cookies("https://google.com")}
            except PlaywrightError:
                print("The Chrome window was closed before sign-in finished.", file=sys.stderr)
                return 1
            if {"SID", "SAPISID"} <= names:
                page.goto("https://meet.google.com/", wait_until="domcontentloaded")
                time.sleep(3)
                ctx.storage_state(path=str(out))
                out.chmod(0o600)  # a live Google session: readable by this user only
                print(f"Signed in. Saved {out}", flush=True)
                ctx.close()
                return 0
        print(f"No sign-in within {args.timeout_minutes} minutes.", file=sys.stderr)
        ctx.close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
