"""tools/integrations/ — adapters to ARIA Capital's external services.

Every adapter reads its credentials through ``secrets.get_secret`` (env-only,
never logged), and declares its required env-var *names* in ``secrets.REQUIRED``.
No secret values live in code, chat, or git — only names. Live adapters
(PropStream/Sheets/Gmail/Twilio/PandaDoc/RelayFi) land in later increments.
"""
