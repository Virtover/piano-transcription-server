import random
import threading
import time

import pytest

from app.transcription.budget import DeviceBudget, StepCancelled, device_budget


def run_steps(budget, sizes, hold=0.02):
    """Run one thread per step size; return the most memory in use at once,
    the order the steps started in, and errors."""
    lock = threading.Lock()
    state = {"in_use": 0.0, "peak": 0.0, "running": 0, "started": [], "errors": []}

    def step(index, gib):
        try:
            with budget.use(gib):
                with lock:
                    state["in_use"] += gib
                    state["running"] += 1
                    state["peak"] = max(state["peak"], state["in_use"])
                    state["started"].append(index)
                time.sleep(hold)
                with lock:
                    state["in_use"] -= gib
                    state["running"] -= 1
        except Exception as error:
            state["errors"].append(error)

    threads = [threading.Thread(target=step, args=(index, gib)) for index, gib in enumerate(sizes)]
    for thread in threads:
        thread.start()
        # In order of arrival.
        time.sleep(0.002)
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "a step never ran"
    return state


def test_steps_that_fit_run_together():
    budget = DeviceBudget(4.0)
    entered = threading.Barrier(3, timeout=5)

    def step():
        with budget.use(1.0):
            entered.wait()

    threads = [threading.Thread(target=step) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert budget.in_use_gib == 0


def test_a_step_waits_for_memory_instead_of_exceeding_the_budget():
    state = run_steps(DeviceBudget(2.0), [1.1, 1.1, 1.1, 0.8, 0.8])

    assert state["peak"] <= 2.0 + 1e-9
    assert sorted(state["started"]) == [0, 1, 2, 3, 4]


def test_steps_start_in_order_of_arrival_so_a_large_one_is_not_starved():
    budget = DeviceBudget(3.0)
    release_first = threading.Event()
    started = []

    def first():
        with budget.use(1.0):
            started.append("first")
            release_first.wait(5)

    def step(name, gib):
        with budget.use(gib):
            started.append(name)

    threads = [threading.Thread(target=first)]
    threads[0].start()
    while not started:
        time.sleep(0.001)
    # The large step cannot fit beside the first; the small ones after it could.
    for name, gib in [("large", 2.5), ("small-1", 0.5), ("small-2", 0.5)]:
        thread = threading.Thread(target=step, args=(name, gib))
        thread.start()
        threads.append(thread)
        time.sleep(0.02)
    assert started == ["first"]
    release_first.set()
    for thread in threads:
        thread.join(timeout=10)
    assert started[:2] == ["first", "large"]


def test_a_step_larger_than_the_budget_runs_alone():
    state = run_steps(DeviceBudget(1.0), [0.5, 5.0, 0.5])

    assert sorted(state["started"]) == [0, 1, 2]
    assert state["peak"] == 5.0


def test_a_zero_budget_still_runs_every_step_one_at_a_time():
    state = run_steps(DeviceBudget(0.0), [1.0] * 5)

    assert state["peak"] == 1.0
    assert len(state["started"]) == 5


def test_exclusive_budget_runs_one_step_at_a_time():
    state = run_steps(DeviceBudget(100.0, exclusive=True), [0.1] * 8)

    assert state["peak"] == pytest.approx(0.1)
    assert len(state["started"]) == 8


def test_a_failing_step_gives_its_memory_back():
    budget = DeviceBudget(2.0)

    with pytest.raises(RuntimeError):
        with budget.use(1.5):
            raise RuntimeError("out of memory (fake)")

    assert budget.in_use_gib == 0
    with budget.use(2.0):
        assert budget.in_use_gib == 2.0


def test_a_cancelled_wait_leaves_the_queue_and_lets_the_next_step_run():
    budget = DeviceBudget(1.0)
    holding = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    outcome = {}

    def holder():
        with budget.use(1.0):
            holding.set()
            release.wait(5)

    def waiter():
        try:
            with budget.use(1.0, cancelled.is_set):
                outcome["waiter"] = "ran"
        except StepCancelled:
            outcome["waiter"] = "cancelled"

    def later():
        with budget.use(0.5):
            outcome["later"] = "ran"

    threads = [threading.Thread(target=holder)]
    threads[0].start()
    holding.wait(5)
    threads.append(threading.Thread(target=waiter))
    threads[-1].start()
    time.sleep(0.05)
    threads.append(threading.Thread(target=later))
    threads[-1].start()
    cancelled.set()
    time.sleep(0.7)
    release.set()
    for thread in threads:
        thread.join(timeout=10)

    assert outcome == {"waiter": "cancelled", "later": "ran"}
    assert budget.in_use_gib == 0


def test_many_steps_of_two_models_never_exceed_the_budget():
    random.seed(7)
    # Transkun steps of 1 to 4 jobs, arrangement windows, as on an 8 GB card.
    sizes = [random.choice([1.1, 2.2, 3.3, 4.4, 0.8, 0.8, 0.8]) for _ in range(120)]

    state = run_steps(DeviceBudget(4.5), sizes, hold=0.005)

    assert not state["errors"]
    assert len(state["started"]) == len(sizes)
    assert state["peak"] <= 4.5 + 1e-9


def test_every_batcher_of_a_device_shares_one_budget():
    first = device_budget("cuda:7", lambda: 3.0)
    second = device_budget("cuda:7", lambda: 99.0)
    cpu = device_budget("cpu-test", lambda: 99.0)

    assert first is second and first.total_gib == 3.0 and not first.exclusive
    assert cpu.exclusive
