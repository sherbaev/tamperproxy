"""Paused calls (breakpoints): the proxy waits on them, the panel decides what happens.

The addon awaits `pause()` on mitmproxy's event loop; the panel thread calls `decide()`,
which hands the decision back to that loop. Nothing here touches the flow itself.
"""

import asyncio
import threading
import time
import uuid

# Past the app's own read timeout (40 s) the call is lost anyway; continue unchanged then.
AUTO_CONTINUE_SECONDS = 120
ACTIONS = ("continue", "abort", "respond")


class Breakpoints:
    def __init__(self):
        self._paused = {}
        self._lock = threading.Lock()

    def list(self):
        with self._lock:
            entries = list(self._paused.values())
        return [entry["public"] for entry in sorted(entries, key=lambda e: e["public"]["paused_at"])]

    async def pause(self, phase, rule_id, snapshot):
        """Wait until the panel decides (or the auto-continue timeout). Returns the decision dict."""
        pause_id = uuid.uuid4().hex[:10]
        entry = {
            "loop": asyncio.get_running_loop(),
            "event": asyncio.Event(),
            "decision": {"action": "continue"},
            "public": {"id": pause_id, "phase": phase, "rule": rule_id, "paused_at": time.time(),
                       "auto_continue_s": AUTO_CONTINUE_SECONDS, **snapshot},
        }
        with self._lock:
            self._paused[pause_id] = entry
        try:
            await asyncio.wait_for(entry["event"].wait(), timeout=AUTO_CONTINUE_SECONDS)
        except asyncio.TimeoutError:
            pass
        finally:
            with self._lock:
                self._paused.pop(pause_id, None)
        return entry["decision"]

    def decide(self, pause_id, decision):
        """Called from any thread. False when the call is no longer paused."""
        if decision.get("action") not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        with self._lock:
            entry = self._paused.get(pause_id)
            if entry is None:
                return False
            entry["decision"] = decision
        entry["loop"].call_soon_threadsafe(entry["event"].set)
        return True
