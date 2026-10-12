"""Pure helpers for the registration flow (kept separate from GTK/WebKit so they
are unit-testable). The webview and SMS watcher call into these.
"""
from __future__ import annotations
import os
import re
import subprocess

_CAPTCHA_URLS = {
    "live": "https://signalcaptchas.org/registration/generate.html",
    "staging": "https://signalcaptchas.org/staging/registration/generate.html",
}


def service_environment(environ=None) -> str:
    """"live" (default) or "staging". Developers set LINAL_SERVICE_ENV=staging
    together with a daemon started with `--service-environment staging` and
    separate XDG data/state folders, to test against Signal's staging service."""
    env = (environ if environ is not None else os.environ).get("LINAL_SERVICE_ENV", "")
    return "staging" if env.strip().lower() == "staging" else "live"


CAPTCHA_URL = _CAPTCHA_URLS[service_environment()]
_CAPTCHA_SCHEME = "signalcaptcha://"
# Signal verification SMS look like: "SIGNAL code: 282337. Do not share..."
_CODE_RE = re.compile(r"(\d{3})[-\s]?(\d{3})")


def is_captcha_url(url: str) -> bool:
    return isinstance(url, str) and url.strip().startswith(_CAPTCHA_SCHEME)


def captcha_token(url: str) -> str:
    """Return the token signal-cli expects. signal-cli accepts the full
    `signalcaptcha://…` string, so we pass it through once validated.

    The link reaches us via GIO (browser hand-off -> Gio.File), which
    normalises it with a trailing '/'. That slash is not part of the token and
    makes Signal reject the captcha (HTTP 403), so it is stripped here."""
    if not is_captcha_url(url):
        raise ValueError("not a signalcaptcha:// url")
    token = url.strip().rstrip("/")
    if len(token) <= len(_CAPTCHA_SCHEME):
        raise ValueError("empty captcha token")
    return token


def parse_sms_code(body: str) -> str | None:
    """Extract a 6-digit Signal verification code from an SMS body.
    Returns the code as 6 plain digits, or None."""
    if not body or "ignal" not in body and "code" not in body.lower():
        # still try, but require the SIGNAL context to avoid false positives
        if "ignal" not in body:
            return None
    m = _CODE_RE.search(body)
    if not m:
        return None
    return m.group(1) + m.group(2)


def friendly_error(error: str) -> str:
    """Turn signal-cli/Signal server errors into something actionable."""
    e = (error or "").lower()
    if "403" in e or "authorization failed" in e:
        return ("Signal didn't accept the captcha. Captchas solved on the Librem 5 "
                "itself are often refused: try again and choose “On another device”.")
    if "captcha" in e:
        return ("Signal didn't accept the captcha. Each try needs a fresh one — "
                "solve it again.")
    if "429" in e or "rate limit" in e or "too many" in e:
        return ("Too many attempts for this number. Wait a while (Signal limits "
                "retries, sometimes for hours) and try again.")
    if "invalid" in e and "code" in e or "incorrect" in e or "verification code" in e:
        return "That code wasn't right. Check the SMS and try again."
    if "timeout" in e or "connect" in e or "network" in e:
        return "Couldn't reach Signal. Check the phone's internet connection."
    return error


def needs_pin(error: str) -> bool:
    """Does a verify error mean the account has a registration lock (PIN)?"""
    e = (error or "").lower()
    return "pin" in e or "registration lock" in e or "locked" in e


def normalize_number(raw: str) -> str:
    """Normalize a user-entered number to E.164 (assume US +1 if 10 digits)."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("+"):
        return digits
    digits = re.sub(r"\D", "", digits)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def device_number() -> str:
    """Best-effort: read this SIM's own number from ModemManager, normalized.
    Returns "" if unavailable (the field is often empty on some SIMs)."""
    try:
        out = subprocess.run(["mmcli", "-m", "any", "-K"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""
    for line in out.splitlines():
        if "own-numbers.value" in line and ":" in line:
            raw = line.split(":", 1)[1].strip()
            if raw and raw != "--":
                return normalize_number(raw)
    return ""
