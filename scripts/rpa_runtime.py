"""Process isolation for a single material RPA request."""

import multiprocessing
import os
import queue


class RpaRuntimeError(RuntimeError):
    pass


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
        process.join(float(timeout_sec))
        if process.is_alive():
            process.terminate()
            process.join(1)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(1)
            raise RpaRuntimeError("rpa_request_timeout: isolated RPA request exceeded %.3f seconds" % float(timeout_sec))
        try:
            kind, payload = result_queue.get(timeout=1)
        except queue.Empty as exc:
            raise RpaRuntimeError("rpa_request_failed: isolated RPA request returned no result") from exc
        if kind == "error":
            name, message, error_type = payload
            error = RpaRuntimeError("%s: %s" % (error_type or name, message))
            error.error_type = error_type or "rpa_gateway_client_error"
            raise error
        return payload
    finally:
        if process.is_alive():
            process.terminate()
            process.join(1)
