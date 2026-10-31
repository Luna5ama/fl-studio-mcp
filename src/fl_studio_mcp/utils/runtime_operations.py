"""In-process load progress and cooperative cancellation, independent of the MIDI lock."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict


class LoadOperations:
    def __init__(self, limit=64):
        self._limit = limit
        self._lock = threading.RLock()
        self._items = OrderedDict()
        self._cancel = {}

    def begin(self, operation_id, **details):
        with self._lock:
            self._items[operation_id] = dict(
                operation_id=operation_id, status="loading", started_at=time.time(), **details
            )
            self._cancel[operation_id] = threading.Event()
            while len(self._items) > self._limit:
                oldest = next(iter(self._items))
                if self._items[oldest]["status"] == "loading":
                    break
                self._items.pop(oldest)
                self._cancel.pop(oldest, None)

    def update(self, result):
        with self._lock:
            key = result.get("operation_id")
            if key in self._items:
                self._items[key].update(result)
                self._items[key]["updated_at"] = time.time()

    def get(self, operation_id):
        with self._lock:
            value = self._items.get(operation_id)
            return dict(value) if value is not None else None

    def list(self, limit=20):
        if not 1 <= limit <= self._limit:
            raise ValueError(f"limit must be between 1 and {self._limit}")
        with self._lock:
            return [dict(item) for item in list(self._items.values())[-limit:]][::-1]

    def cancel(self, operation_id=None):
        with self._lock:
            if operation_id is None:
                active = [key for key, value in self._items.items() if value["status"] == "loading"]
                if len(active) != 1:
                    raise ValueError("Specify the id of exactly one active loading operation")
                operation_id = active[0]
            if operation_id not in self._items:
                raise ValueError("Unknown loading operation in this server session")
            state = self._items[operation_id]
            if state["status"] != "loading":
                return dict(state, cancellation_requested=False)
            self._cancel[operation_id].set()
            return {
                "success": True,
                "operation_id": operation_id,
                "status": "cancellation_requested",
                "cancellation_requested": True,
                "detail": "Activation already issued cannot be undone; inspect final status.",
            }

    def cancelled(self, operation_id):
        with self._lock:
            event = self._cancel.get(operation_id)
            return event is not None and event.is_set()


load_operations = LoadOperations()

preset_operations = LoadOperations()
