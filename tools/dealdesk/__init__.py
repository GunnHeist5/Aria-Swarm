"""tools/dealdesk/ — the live pricing endpoint the AI receptionist calls.

When a seller calls, the voice agent (Trillet) hits this service with the
property address/APN. It looks the property up in the PropStream data, runs the
swarm's *own* deterministic offer formula (tools/wholesaling/deals.py), and
returns a negotiation band — an opening offer and a hard ceiling the agent must
never exceed. Anything it can't price cleanly (no data, listed with an agent,
encumbered, or not land) comes back flagged to escalate to a human.

The pricing is genome — the same WholesalingConfig the swarm evolves — so the
receptionist and the swarm always negotiate the identical strategy.
"""
