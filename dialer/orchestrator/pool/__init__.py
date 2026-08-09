"""Caller-ID number pool: selection (`selector`) and health/benching (`health`).

The pool lives in Postgres (`numbers` + `number_usage`); Redis never holds
pool state. Every lease is taken inside one row-locking transaction so
concurrent dial workers can never oversubscribe a number's daily cap, ramp
allowance, or cooldown — number reputation is the biggest operational risk
(SPEC §3-§4) and the existing JustCall numbers are already spam-flagged.
"""
