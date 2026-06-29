"""tools/ — crypto wallet utilities, browser automation, and HITL webhooks.

Decoupled side-effect drivers the swarm graph calls into. Each module isolates
external interfaces (on-chain rails, Web2 bridges, notification webhooks) behind
small, individually testable functions.
"""
