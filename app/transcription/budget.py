"""The memory one device lends to model steps, shared by every model on it.

Transkun and the arrangement model run in their own batcher threads but on
the same GPU, and each step takes its working memory for a moment: a
Transkun step jobs x WORKER_MEMORY_PER_JOB_GIB, an arrangement step
WORKER_ARRANGEMENT_MEMORY_GIB. A step waits until its memory is free instead
of running out of it beside the other model's step. Steps are served in
order of arrival, so a stream of small steps cannot starve a large one.

On the CPU, steps run one at a time: each already uses every PyTorch thread,
and two at once only slow each other down.

It does not import torch, so the rules can be tested anywhere.
"""

import threading
from collections import deque
from contextlib import contextmanager
from typing import Callable, Iterator


class DeviceBudget:
    def __init__(self, total_gib: float, exclusive: bool = False):
        # A step larger than the whole budget still runs, alone, so that a
        # job never waits forever; the batcher's out-of-memory handling is
        # what remains if it does not fit.
        self.total_gib = max(0.0, total_gib)
        self.exclusive = exclusive
        self._in_use = 0.0
        self._steps = 0
        self._waiting: deque[object] = deque()
        self._condition = threading.Condition()

    @property
    def in_use_gib(self) -> float:
        with self._condition:
            return self._in_use

    def _fits(self, gib: float) -> bool:
        if self._steps == 0:
            return True
        if self.exclusive:
            return False
        return self._in_use + gib <= self.total_gib + 1e-9

    @contextmanager
    def use(self, gib: float, cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
        """Hold gib for the duration of a step. With cancelled, the wait is
        given up, raising StepCancelled, once it returns True."""
        gib = max(0.0, gib)
        ticket = object()
        with self._condition:
            self._waiting.append(ticket)
            try:
                while self._waiting[0] is not ticket or not self._fits(gib):
                    if cancelled is not None and cancelled():
                        raise StepCancelled()
                    # A timeout, so that a cancellation is noticed while waiting.
                    self._condition.wait(timeout=0.5 if cancelled else None)
            except BaseException:
                self._waiting.remove(ticket)
                self._condition.notify_all()
                raise
            self._waiting.popleft()
            self._in_use += gib
            self._steps += 1
            # The next in line may fit beside this step.
            self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._in_use -= gib
                self._steps -= 1
                if self._steps == 0:
                    self._in_use = 0.0
                self._condition.notify_all()


class StepCancelled(Exception):
    """Every request of a step was cancelled while it waited for memory."""


_budgets: dict[str, DeviceBudget] = {}
_budgets_lock = threading.Lock()


def device_budget(device: str, total_gib: Callable[[], float]) -> DeviceBudget:
    """The process-wide budget of device, sized by total_gib on first use.
    Every batcher on the device must share it."""
    with _budgets_lock:
        budget = _budgets.get(device)
        if budget is None:
            budget = DeviceBudget(total_gib(), exclusive=not device.startswith("cuda:"))
            _budgets[device] = budget
        return budget
