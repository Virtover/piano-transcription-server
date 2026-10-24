"""Helpers for running transcription requests in batches.

They do not import torch, so the batching rules can be tested anywhere.
A request is any object with `result` (a Future), `progress_callback`,
`cancellation_callback`, and `reported_progress` attributes.
"""

import logging
from typing import Any, Callable, Sequence

from app.errors import UserFacingError


logger = logging.getLogger(__name__)


class GpuOutOfMemory(UserFacingError, RuntimeError):
    """A single request does not fit in GPU memory, even on its own."""

    def __init__(self) -> None:
        super().__init__("The GPU ran out of memory for this transcription")


def fail(request: Any, error: BaseException) -> None:
    """Fail a request unless it already has a result. A Future is never resolved twice."""
    if not request.result.done():
        request.result.set_exception(error)


def resolve(request: Any, value: Any) -> None:
    if not request.result.done():
        request.result.set_result(value)


def is_cancelled(request: Any) -> bool:
    """True if the request was cancelled. A failing callback fails only this request."""
    if request.cancellation_callback is None:
        return False
    try:
        return bool(request.cancellation_callback())
    except Exception as error:
        fail(request, error)
        return True


def report_progress(request: Any, value: float) -> bool:
    """Report progress that never goes backwards, so a retried request does
    not appear to restart. Returns False, and fails only this request, if the
    callback raises (for example because its job was cancelled)."""
    if request.result.done():
        return False
    if request.progress_callback is None or value <= request.reported_progress:
        return True
    try:
        request.progress_callback(value)
    except Exception as error:
        fail(request, error)
        return False
    request.reported_progress = value
    return True


def run_splitting_on_out_of_memory(
    requests: Sequence[Any],
    transcribe: Callable[[list[Any]], None],
    is_out_of_memory: Callable[[BaseException], bool],
    free_memory: Callable[[], None],
    lower_batch_size: Callable[[int], None],
    on_single_out_of_memory: Callable[[Any], None] | None = None,
) -> None:
    """Run `transcribe` on the requests. On GPU out-of-memory, free memory,
    split the unfinished requests in half, and retry each half from the start.

    Requests that already finished or failed are not retried. A single
    request that still does not fit is passed to `on_single_out_of_memory`,
    or failed with GpuOutOfMemory.
    """
    try:
        transcribe(list(requests))
        return
    except Exception as error:
        if not is_out_of_memory(error):
            raise

    free_memory()
    pending = [request for request in requests if not request.result.done()]
    if not pending:
        return
    if len(pending) == 1:
        if on_single_out_of_memory is not None:
            on_single_out_of_memory(pending[0])
        else:
            fail(pending[0], GpuOutOfMemory())
        return

    half = len(pending) // 2
    lower_batch_size(half)
    for part in (pending[:half], pending[half:]):
        run_splitting_on_out_of_memory(
            part,
            transcribe,
            is_out_of_memory,
            free_memory,
            lower_batch_size,
            on_single_out_of_memory,
        )
