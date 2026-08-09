"""Reachwell Orchestrator — AI outbound dialer.

JustCall is the contact database, human-rep tool, and event source; Twilio
(behind the swappable VoiceProvider interface in orchestrator/voice) places
every AI call. Postgres is the source of truth; Redis/arq is only transport.

Phases are enforced in code, not convention:
  1 dry run (zero calls) -> 2 live-small (second touch, concurrency <= 2)
  -> 3 full (requires Trust Hub confirmation).
"""

__version__ = "0.1.0"
