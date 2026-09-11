"""Spend guards for the paid Odds API.

The Odds API is the only source that costs money, and the UI refreshes it
continuously — there is no on/off switch to forget. At that cadence a tab left
open overnight would spend tens of thousands of credits, so the spending is
tied to a *heartbeat* that has to keep proving someone is watching:

  * a dead-man switch — the UI beats every two seconds, and only while its tab
    is visible AND someone has touched the page within AUTO_IDLE_STOP_S. Miss
    AUTO_HEARTBEAT_TIMEOUT_S of beats and the server stops spending. Walking
    away, hiding the tab, closing the laptop, killing the browser and crashing
    the renderer all stop the beats, so none of them can keep spending.
    Coming back re-arms on the very next beat, with nothing to click.
  * rate and volume ceilings — per minute and per day.
  * active hours    — no spending outside them at all.

Every check lives here, on the server, so a runaway or hand-rolled client
cannot spend past them; the browser's idle timer is what makes the common case
polite, not what makes it safe.

The day's total is persisted, so restarting the server does not hand you a
fresh daily allowance.
"""
import json
import threading
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config

LEDGER = config.DATA_DIR / "credit_ledger.json"

_lock = threading.Lock()

# rolling window of (monotonic_ts, credits) for the per-minute rate ceiling
_recent = deque()

_state = {
    "armed": False,
    "armed_at": 0.0,       # time.time()
    "last_beat": 0.0,
    "sports": [],
    "run_credits": 0,
    "stop_reason": None,   # why the last armed run ended
}

_ledger = {"day": None, "credits": 0, "last_used": None,
           "remaining": None}


class BudgetError(RuntimeError):
    """A paid call was refused by a spend guard."""


# ---------------- ledger ----------------

def _today() -> str:
    return datetime.now(ZoneInfo(config.LOCAL_TZ)).strftime("%Y-%m-%d")


def _load():
    global _ledger
    if _ledger["day"] == _today():
        return _ledger
    try:
        saved = json.loads(LEDGER.read_text())
    except Exception:
        saved = {}
    if saved.get("day") == _today():
        _ledger = {"day": saved["day"], "credits": int(saved.get("credits") or 0),
                   "last_used": saved.get("last_used"),
                   "remaining": saved.get("remaining")}
    else:
        # new local day: the count resets, but keep last_used/remaining so the
        # first scan of the day still measures an exact delta from the headers
        _ledger = {"day": _today(), "credits": 0,
                   "last_used": saved.get("last_used"),
                   "remaining": saved.get("remaining")}
    return _ledger


def _save():
    try:
        config.DATA_DIR.mkdir(exist_ok=True)
        LEDGER.write_text(json.dumps(_ledger))
    except Exception:
        pass


def note_spend(quota: dict, estimate: int = 1):
    """Record what a scan actually cost.

    The Odds API's own x-requests-used header is ground truth (tennis can cost
    more than one credit per sport, since each live tournament is its own
    key), so the delta is used whenever it looks sane; the caller's estimate is
    only a fallback for a response whose headers were missing or reset.
    """
    with _lock:
        led = _load()
        used = quota.get("used") if isinstance(quota, dict) else None
        spent = estimate
        if used is not None:
            prev = led.get("last_used")
            if prev is not None and 0 <= used - prev <= 100:
                spent = used - prev
            led["last_used"] = used
        if isinstance(quota, dict) and quota.get("remaining") is not None:
            led["remaining"] = quota["remaining"]
        led["credits"] += max(0, int(spent))
        _state["run_credits"] += max(0, int(spent))
        _recent.append((time.monotonic(), max(0, int(spent))))
        _save()
        return spent


def _rate_per_min():
    cutoff = time.monotonic() - 60.0
    while _recent and _recent[0][0] < cutoff:
        _recent.popleft()
    return sum(c for _, c in _recent)


# ---------------- arming ----------------

def _in_active_hours() -> bool:
    lo, hi = config.AUTO_ACTIVE_HOURS_LOCAL
    return lo <= datetime.now(ZoneInfo(config.LOCAL_TZ)).hour < hi


def _expire_locked():
    """Stop spending if the heartbeat has lapsed. Caller holds _lock.

    There is no run-length or per-run credit limit any more: with the client
    arming itself on every beat, either would disarm and then immediately
    re-arm, which is not a guard, just churn. The heartbeat and the daily cap
    are the real limits.
    """
    if not _state["armed"]:
        return
    if time.time() - _state["last_beat"] > config.AUTO_HEARTBEAT_TIMEOUT_S:
        _disarm_locked("no heartbeat — idle, hidden, asleep or closed")
    elif not _in_active_hours():
        _disarm_locked("outside active hours")


def _disarm_locked(reason):
    if _state["armed"]:
        print(f"  [budget] auto-refresh OFF: {reason} "
              f"({_state['run_credits']} credits this run)")
    _state["armed"] = False
    _state["stop_reason"] = reason


def _blocker_locked():
    """Why spending is not allowed right now, or None. Caller holds _lock."""
    led = _load()
    if not _in_active_hours():
        lo, hi = config.AUTO_ACTIVE_HOURS_LOCAL
        return f"outside active hours ({lo:02d}:00–{hi:02d}:00 {config.LOCAL_TZ})"
    if led["credits"] >= config.AUTO_DAILY_CREDIT_CAP:
        return f"today's {config.AUTO_DAILY_CREDIT_CAP} credit cap is spent"
    left = led.get("remaining")
    if left is not None and left <= config.AUTO_MIN_PLAN_REMAINING:
        return (f"only {left} credits left on the plan (reserve "
                f"{config.AUTO_MIN_PLAN_REMAINING}) — use Refresh instead")
    return None


def _arm_locked(sports):
    now = time.time()
    if not _state["armed"]:
        print(f"  [budget] spending ON: {', '.join(sports or []) or 'all'}")
        _state.update({"armed": True, "armed_at": now, "run_credits": 0})
    _state.update({"last_beat": now, "stop_reason": None})
    if sports:
        _state["sports"] = list(sports)


def disarm(reason="stopped"):
    with _lock:
        _disarm_locked(reason)
        return _status_locked()


def beat(sports=None):
    """One heartbeat from a visible, recently-touched tab.

    This is both the liveness proof AND the thing that turns spending on: the
    client sends it only while someone is demonstrably there, so there is
    nothing to arm by hand and nothing to forget to switch off. A beat that
    arrives while a ceiling is hit arms nothing and reports why.
    """
    with _lock:
        blocked = _blocker_locked()
        if blocked:
            _disarm_locked(blocked)
        else:
            _arm_locked(sports)
        return _status_locked()


def gate(estimate: int = 1, auto: bool = False):
    """Raise BudgetError unless this paid scan is allowed to happen."""
    with _lock:
        led = _load()
        if led["credits"] + estimate > config.AUTO_DAILY_CREDIT_CAP:
            raise BudgetError(
                f"daily cap reached: {led['credits']}/"
                f"{config.AUTO_DAILY_CREDIT_CAP} credits spent today")
        # The reserve exists to stop the 1/second loop from eating the plan.
        # It must never block a deliberate press of Refresh — that is what the
        # reserve is being kept FOR.
        left = led.get("remaining")
        if auto and left is not None and left - estimate < config.AUTO_MIN_PLAN_REMAINING:
            _disarm_locked(f"plan reserve: only {left} credits left on the "
                           f"key (floor {config.AUTO_MIN_PLAN_REMAINING})")
            raise BudgetError(
                f"only {left} Odds API credits left on the plan; auto-refresh "
                f"keeps {config.AUTO_MIN_PLAN_REMAINING} in reserve")
        if _rate_per_min() + estimate > config.AUTO_MAX_CREDITS_PER_MIN:
            raise BudgetError(
                f"this scan needs {estimate} credits and the ceiling is "
                f"{config.AUTO_MAX_CREDITS_PER_MIN}/min "
                f"({_rate_per_min()} used in the last minute)")
        if auto:
            _expire_locked()
            if not _state["armed"]:
                raise BudgetError(_state["stop_reason"] or "not updating")


def _status_locked():
    led = _load()
    return {
        "armed": _state["armed"],
        "sports": _state["sports"],
        "stop_reason": _state["stop_reason"],
        "run_credits": _state["run_credits"],
        "blocked_by": _blocker_locked(),
        "today": led["credits"],
        "plan_remaining": led.get("remaining"),
        "plan_reserve": config.AUTO_MIN_PLAN_REMAINING,
        "daily_cap": config.AUTO_DAILY_CREDIT_CAP,
        "per_min": _rate_per_min(),
        "per_min_cap": config.AUTO_MAX_CREDITS_PER_MIN,
        "live_interval_s": config.AUTO_LIVE_INTERVAL_S,
        "idle_interval_s": config.AUTO_IDLE_INTERVAL_S,
        "idle_stop_s": config.AUTO_IDLE_STOP_S,
        "active_hours": list(config.AUTO_ACTIVE_HOURS_LOCAL),
        "in_active_hours": _in_active_hours(),
    }


def status():
    with _lock:
        _expire_locked()
        return _status_locked()


def watchdog(period=2.0):
    """Expire an armed run even when nothing is calling in.

    gate() alone would be enough to stop *spending* (no requests, no credits),
    but a run that silently stays 'armed' would resume the moment a stray
    request arrived. This makes the disarm real and logs it while it happens.
    """
    def loop():
        while True:
            try:
                with _lock:
                    _expire_locked()
            except Exception:
                pass
            time.sleep(period)
    threading.Thread(target=loop, daemon=True).start()
