"""Process isolation for a single material RPA request."""

import multiprocessing
import os
import queue
import time


class RpaRuntimeError(RuntimeError):
    error_type = "rpa_result_unconfirmed"


def _context():
    return multiprocessing.get_context("spawn" if os.name == "nt" else "fork")


def _entry(worker, params, result_queue):
    try:
        result_queue.put(("result", worker(params)))
    except Exception as exc:
        result_queue.put(("error", (type(exc).__name__, str(exc), getattr(exc, "error_type", None))))


def run_isolated(params, *, timeout_sec, worker):
    context = _context()
    result_queue = context.Queue()
    process = context.Process(target=_entry, args=(worker, params, result_queue))
    process.start()
    try:
        deadline = time.monotonic() + float(timeout_sec)
        outcome = None
        while time.monotonic() < deadline:
            try:
                outcome = result_queue.get_nowait()
                break
            except queue.Empty:
                process.join(0.05)
                if not process.is_alive():
                    break
        if outcome is None and process.is_alive():
            process.terminate()
            process.join(1)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(1)
            raise RpaRuntimeError("rpa_request_timeout: isolated RPA request exceeded %.3f seconds" % float(timeout_sec))
        if outcome is None:
            try:
                outcome = result_queue.get(timeout=1)
            except queue.Empty as exc:
                raise RpaRuntimeError("rpa_request_failed: isolated RPA request returned no result") from exc
        try:
            kind, payload = outcome
        except (TypeError, ValueError) as exc:
            raise RpaRuntimeError("rpa_request_failed: isolated RPA request returned no result") from exc
        if kind == "error":
            name, message, error_type = payload
            error = RpaRuntimeError("%s: %s" % (error_type or name, message))
            error.error_type = error_type or "rpa_result_unconfirmed"
            raise error
        return payload
    finally:
        if process.is_alive():
            process.terminate()
            process.join(1)
