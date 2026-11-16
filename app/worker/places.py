"""Places on this device's models: how many local jobs may be before or in
their model step at once.

The launcher counts the places from VRAM (or, on the CPU, from cores and
RAM), as the job capacity, and starts twice as many Dramatiq threads. A job
takes a place before it is claimed and gives it back as soon as its model
step ends, so its last steps (the tempo map, the MIDI file, the upload, and
billing) run beside the next job's model step, in a thread of its own,
without ever putting more jobs on the GPU than fit in its VRAM.

A thread whose job finds no free place does not claim it: the job stays
queued, and app.worker.tasks hands it to a device with a free place as soon
as there is one. Places are handed out in order of arrival.

It does not import torch, so the rules can be tested anywhere.
"""

import os
import threading
from collections import deque
from typing import Callable


# The number of places, set by the launcher in Dramatiq's environment.
PLACES_VARIABLE = "MODEL_PLACES"
# Dramatiq threads per place on a GPU: one for a job before or in its model
# step, one for a job past it.
GPU_THREADS_PER_PLACE = 2


class Place:
    """One place, held by one job; giving it back twice gives it back once."""

    def __init__(self, places: "Places"):
        self._places = places
        self._held = True
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if not self._held:
                return
            self._held = False
        self._places._give_back()


class Places:
    def __init__(self, capacity: int, on_change: Callable[[], None] | None = None):
        self.capacity = max(1, capacity)
        # Called, outside the lock, after a place is taken or given back.
        self.on_change = on_change
        self._taken = 0
        self._waiting: deque[object] = deque()
        self._condition = threading.Condition()

    @property
    def in_use(self) -> int:
        with self._condition:
            return self._taken

    @property
    def waiting(self) -> int:
        with self._condition:
            return len(self._waiting)

    def take(self, give_up: Callable[[], bool], interval: float = 2.0) -> Place | None:
        """A place, once one is free and every job that came earlier has
        one. give_up is asked every interval seconds while waiting, and the
        wait ends without a place once it returns True."""
        ticket = object()
        with self._condition:
            self._waiting.append(ticket)
            try:
                while self._waiting[0] is not ticket or self._taken >= self.capacity:
                    # Outside the lock: give_up may take its time, as reading Redis.
                    self._condition.release()
                    try:
                        stop = give_up()
                    finally:
                        self._condition.acquire()
                    if stop:
                        return None
                    if self._waiting[0] is not ticket or self._taken >= self.capacity:
                        self._condition.wait(timeout=interval)
            finally:
                self._waiting.remove(ticket)
                # The next in line may have a place now, or be first.
                self._condition.notify_all()
            self._taken += 1
        self.notify_change()
        return Place(self)

    def _give_back(self) -> None:
        with self._condition:
            self._taken = max(0, self._taken - 1)
            self._condition.notify_all()
        self.notify_change()

    def notify_change(self) -> None:
        if self.on_change is not None:
            self.on_change()


def configured(default: int) -> int:
    """The places the launcher gave this process, or default without them."""
    try:
        value = int(os.environ.get(PLACES_VARIABLE) or 0)
    except ValueError:
        value = 0
    return value if value >= 1 else default
