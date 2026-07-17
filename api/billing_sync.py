"""Billing data sync from Sisuo NewAPI logs via direct HTTP API.

Polls https://newapi.sisuo.de/api/log/self every 15 seconds,
matches entries to ChatApp bubbles by timestamp + model name,
writes billing data to assistant bubble's 'billing' field.

Matching rule (deterministic, no fallback):
    sisuo_created_at == bubble_created_at + bubble_response_time  (within tolerance)
    AND model_name match
    AND match is unique

API auth: session cookie + New-Api-User header (numeric user ID).
"""
import json
import os
import time
import threading
import requests as _requests

_POLL_INTERVAL_S = 999999  # DISABLED - shares rate limit with AI requests, causes 429
_MATCH_TOLERANCE_S = 8
_API_URL = "https://newapi.sisuo.de/api/log/self"
_USER_ID = "3483"
_COOKIE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sisuo_cookie.txt")


def _get_session_cookie() -> str:
    """Load session cookie from data/sisuo_cookie.txt. Re-read on every so updates take effect without restart."""
    try:
        with open(_COOKIE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _fetch_recent_logs(page: int = 0, per_page: int = 20) -> list:
    """Fetch recent logs from sisuo API. Returns list of raw API entries."""
    try:
        resp = _requests.get(
            _API_URL,
            params={"p": page, "per_page": per_page},
            headers={"New-Api-User": _USER_ID},
            cookies={"session": _get_session_cookie()},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not data.get("success", True):
            return []
        return data.get("data", {}).get("items", [])
    except Exception as e:
        print(f"[BillingSync] Fetch error: {e}")
        return []


def _parse_api_entry(raw: dict) -> dict:
    """Parse a raw API entry into internal billing format.

    API fields of interest:
        created_at: int - Unix timestamp of request completion
        model_name: str - e.g. "按次-kiro-claude-opus-4-6-thinking"
        prompt_tokens: int - non-cached input tokens
        completion_tokens: int - output tokens
        use_time: int - total duration seconds
        quota: int - cost in internal units, 500000 = $1 USD
        other: str - JSON with cache_tokens, cache_write_tokens, frt, model_price
    """
    entry = {
        "sisuo_created_at": raw.get("created_at", 0),
        "model": raw.get("model_name", ""),
        "input_tokens": raw.get("prompt_tokens", 0),
        "output_tokens": raw.get("completion_tokens", 0),
        "use_time_s": raw.get("use_time", 0),
        "sisuo_id": raw.get("id", 0),
        "request_id": raw.get("request_id", ""),
    }

    other_str = raw.get("other", "")
    if other_str:
        try:
            other = json.loads(other_str)
            entry["cache_read"] = other.get("cache_tokens", 0)
            entry["cache_write"] = other.get("cache_write_tokens", 0)
            entry["frt_ms"] = other.get("frt", 0)
            entry["model_price"] = other.get("model_price", 0)
        except (json.JSONDecodeError, TypeError):
            pass

    quota = raw.get("quota", 0)
    entry["cost_usd"] = quota / 500000.0

    return entry


def _match_entry_to_bubble(entry: dict, sessions: dict) -> tuple:
    """Match a sisuo log entry to exactly one ChatApp bubble.

    Returns (sid, msg_id) if unique match found, None otherwise.

    Priority 1: Exact match by request_id (from X-Oneapi-Request-Id header)
    Priority 2: Time-based fuzzy match (for old bubbles without _request_id)
    """
    # Priority 1: request_id exact match
    request_id = entry.get("request_id", "")
    if request_id:
        for sid, session in list(sessions.items()):
            if not isinstance(session, dict) or "conversation_history" not in session:
                continue
            for msg in session.get("conversation_history", []):
                if msg.get("_request_id") == request_id:
                    if msg.get("billing"):
                        return None  # Already matched
                    return (sid, msg.get("id"))

    # No fallback - only request_id exact match is used
    return None


def _apply_billing(api, sid: str, msg_id: int, entry: dict):
    """Write billing data to matched bubble."""
    session = api.sessions.get(sid)
    if not session:
        return
    for msg in session.get("conversation_history", []):
        if msg.get("id") == msg_id:
            msg["billing"] = {
                "input_tokens": entry.get("input_tokens", 0),
                "output_tokens": entry.get("output_tokens", 0),
                "cache_read": entry.get("cache_read", 0),
                "cache_write": entry.get("cache_write", 0),
                "cost_usd": entry.get("cost_usd", 0),
                "frt_ms": entry.get("frt_ms", 0),
            }
            break


class BillingSyncDaemon:
    """Background daemon that polls sisuo API and syncs billing to bubbles."""

    def __init__(self, api):
        self._api = api
        self._running = False
        self._thread = None
        self._processed_ids = set()

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        print(f"[BillingSync] Started, polling every {_POLL_INTERVAL_S}s")

    def stop(self):
        self._running = False

    def _poll_loop(self):
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                print(f"[BillingSync] Error: {e}")
            time.sleep(_POLL_INTERVAL_S)

    def _poll_once(self):
        """Fetch recent logs from API, match to bubbles, write billing data."""
        return  # COMPLETELY DISABLED - do not send any HTTP requests
        # First run: scan 50 pages with 200ms delay to backfill history without 429
        if not hasattr(self, '_first_run_done'):
            self._first_run_done = True
            raw_items = []
            for _p in range(50):
                items = _fetch_recent_logs(page=_p, per_page=100)
                if not items:
                    break
                raw_items.extend(items)
                if _p > 0:
                    time.sleep(0.2)
        else:
            # Normal mode: 1 request per second, only page 0 (latest 10 entries)
            raw_items = _fetch_recent_logs(page=0, per_page=100)
        if not raw_items:
            return

        new_matches = 0
        for raw in raw_items:
            entry = _parse_api_entry(raw)
            # Use request_id as stable dedup key (positional 'id' shifts as new entries arrive)
            dedup_key = entry.get("request_id", "")
            if not dedup_key:
                continue  # Skip entries without request_id
            if dedup_key in self._processed_ids:
                continue

            result = _match_entry_to_bubble(entry, self._api.sessions)
            if result:
                sid, msg_id = result
                _apply_billing(self._api, sid, msg_id, entry)
                self._processed_ids.add(dedup_key)
                new_matches += 1
            else:
                # Only mark as processed if entry is old enough that no bubble will ever match
                # Give 120 seconds for the corresponding bubble to finish streaming
                entry_age = time.time() - entry.get('sisuo_created_at', 0)
                if entry_age > 120:
                    self._processed_ids.add(dedup_key)

        if new_matches > 0:
            self._api.save_sessions(push_update=True)
            print(f"[BillingSync] Matched {new_matches} new entries")

    def manual_sync(self, pages: int = 1) -> int:
        """Manually trigger sync. Returns match count."""
        total_matches = 0
        for page in range(pages):
            raw_items = _fetch_recent_logs(page=page, per_page=50)
            if not raw_items:
                break
            for raw in raw_items:
                entry = _parse_api_entry(raw)
                result = _match_entry_to_bubble(entry, self._api.sessions)
                if result:
                    sid, msg_id = result
                    _apply_billing(self._api, sid, msg_id, entry)
                    total_matches += 1
        if total_matches > 0:
            self._api.save_sessions(push_update=True)
        return total_matches
