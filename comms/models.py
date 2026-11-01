"""
comms — app 10 in CLAUDE.md §1.

Holds: EmailThread, EmailMessage, ChatChannel, ChatMessage, Announcement, Meeting.
Phase 2–3. No notification model before Phase 3 (§4 Notifications).

May import from: core … postaward. Never from apps below it.
Every model holding business data inherits core.models.Auditable (R7).
"""
