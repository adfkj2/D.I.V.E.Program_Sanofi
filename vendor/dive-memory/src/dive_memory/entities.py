from __future__ import annotations

import re

# The subject key every first-person fact is stored under. Anything else names
# a third party and must stay out of the *user* profile.
USER_SUBJECT = "user"


def is_user_subject(subject: object) -> bool:
    """True when a structured subject refers to the user themselves.

    A missing subject predates the subject field and is treated as the user,
    which is what every pre-v2 memory implicitly meant.
    """
    if subject is None:
        return True
    return str(subject).strip().casefold() in {USER_SUBJECT, "self", "me", "i"}


def normalize_entity(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip().lower())
    return value.strip(".,，。!?！？")


def entity_candidates(content: str, structured: dict) -> list[tuple[str, str]]:
    predicate = str(structured.get("predicate", "statement"))
    value = str(structured.get("value", content))
    candidates: list[tuple[str, str]] = []
    subject = structured.get("subject")
    if not is_user_subject(subject):
        # A third-party fact is *about* that person, so the person is the
        # primary entity and the value is what is asserted about them.
        entity_type = "person"
        candidates.append((normalize_entity(str(subject)), entity_type))
    if not value:
        return candidates
    entity_type = "location" if predicate in {"residence", "lives_in"} else "concept"
    candidates.append((normalize_entity(value), entity_type))
    return candidates
