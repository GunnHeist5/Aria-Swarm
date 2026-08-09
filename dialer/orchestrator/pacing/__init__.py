"""Pacing: the calls-per-minute limiter (`limiter`).

Concurrency (max simultaneous calls) is deliberately NOT implemented here —
arq's `max_jobs = cfg.effective_concurrency()` already bounds in-flight dial
jobs, and a second semaphore would just be a second thing to get out of sync.
This package only owns the calls-per-minute token counter in Redis.
"""
