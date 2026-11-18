"""Places on this device's models: how many local jobs may be before or in
their model step at once.

The launcher counts the places from total VRAM (or, on the CPU, from cores
and RAM), as the most jobs the device holds, and starts twice as many
Dramatiq threads. A job takes a place before it is claimed and gives it back
as soon as its model step ends, so its last steps (the tempo map, the MIDI
file, the upload, and billing) run beside the next job's model step, in a
thread of its own.

On a GPU, a place is also memory: a job takes one only while the VRAM that
other programs leave (app.transcription.device.GpuRoom) holds its memory
beside the memory of every job that holds a place. So the device takes more
jobs as other programs free VRAM, and fewer as they use it, without a restart.

A thread whose job finds no free place does not claim it: the job stays
queued, and app.worker.tasks hands it to a device with a free place as soon
as there is one. Places are handed out in order of arrival.

It does not import torch, so the rules can be tested anywhere.
"""

import logging
import os
import threading
from collections import deque
from typing import Callable


logger = logging.getLogger(__name__)

# The number of places, set by the launcher in Dramatiq's environment.
PLACES_VARIABLE = "MODEL_PLACES"
# Dramatiq threads per place on a GPU: one for a job before or in its model
# step, one for a job past it.
GPU_THREADS_PER_PLACE = 2


class Place:
    """One place, held by one job; giving it back twice gives it back once."""

    def __init__(self, places: "Places", memory_gib: float = 0.0):
        self._places = places
        self.memory_gib = memory_gib
        self._held = True
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if not self._held:
                return
            self._held = False
        self._places._give_back(self.memory_gib)


class Places:
    def __init__(
        self,
        capacity: int,
        on_change: Callable[[], None] | None = None,
        room: Callable[[], float] | None = None,
    ):
        self.capacity = max(1, capacity)
        # Called, outside the lock, after a place is taken or given back.
        self.on_change = on_change
        # GiB the jobs may use now (GpuRoom.budget_gib); None: no memory limit.
        self.room = room
        self._taken = 0
        self._memory = 0.0
        self._waiting: deque[object] = deque()
        self._condition = threading.Condition()

    @property
    def in_use(self) -> int:
        with self._condition:
            return self._taken

    @property
    def memory_gib(self) -> float:
        """The memory of the jobs that hold a place."""
        with self._condition:
            return self._memory

    def usable(self, job_memory_gib: float) -> int:
        """The places that can be held now: those held, and as many more
        jobs of job_memory_gib as fit in the room now, up to the capacity."""
        budget = self._budget(None)
        with self._condition:
            if budget is None or job_memory_gib <= 0:
                return self.capacity
            more = int(max(0.0, budget - self._memory) / job_memory_gib + 1e-9)
            return max(self._taken, min(self.capacity, self._taken + more))

    def _free(self, ticket: object, memory_gib: float, budget: float | None) -> bool:
        """Whether ticket may take a place now; under the lock."""
        if self._waiting[0] is not ticket or self._taken >= self.capacity:
            return False
        return budget is None or self._memory + memory_gib <= budget + 1e-9

    @property
    def waiting(self) -> int:
        with self._condition:
            return len(self._waiting)

    def take(self, give_up: Callable[[], bool], interval: float = 2.0, memory_gib: float = 0.0) -> Place | None:
        """A place, once one is free, memory_gib fits in the room beside the
        jobs that hold one, and every job that came earlier has one. give_up
        is asked every interval seconds while waiting, and the wait ends
        without a place once it returns True."""
        ticket = object()
        # Read outside the lock: reading the GPUs takes a moment. The room
        # does not count the jobs that hold places, so a reading taken before
        # another job took one is still right.
        budget = self._budget(None)
        with self._condition:
            self._waiting.append(ticket)
            try:
                while not self._free(ticket, memory_gib, budget):
                    # Outside the lock: give_up may take its time, as reading Redis.
                    self._condition.release()
                    try:
                        stop = give_up()
                        budget = self._budget(budget)
                    finally:
                        self._condition.acquire()
                    if stop:
                        return None
                    if not self._free(ticket, memory_gib, budget):
                        self._condition.wait(timeout=interval)
            finally:
                self._waiting.remove(ticket)
                # The next in line may have a place now, or be first.
                self._condition.notify_all()
            self._taken += 1
            self._memory += memory_gib
        self.notify_change()
        return Place(self, memory_gib)

    def _budget(self, last: float | None) -> float | None:
        """The room now, or the last reading if it cannot be read: a job
        waits rather than fails, and without a reading at all, it may take
        a place as before the room was read."""
        if self.room is None:
            return None
        try:
            return self.room()
        except Exception:
            logger.exception("Could not read the room for jobs")
            return last

    def _give_back(self, memory_gib: float = 0.0) -> None:
        with self._condition:
            self._taken = max(0, self._taken - 1)
            self._memory = max(0.0, self._memory - memory_gib) if self._taken else 0.0
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
