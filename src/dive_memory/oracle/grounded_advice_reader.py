"""Personalized advice grounded in remembered premises, without invented history."""
from .indexed_reader import IndexedEvidenceReader, RULES

VERSION = 'oracle-grounded-advice-v1'


class GroundedAdviceReader(IndexedEvidenceReader):
    prompt_version = VERSION
    rules = RULES + """
This request asks for present advice. Separate two kinds of content:
* Remembered premises: the user's preferences, experiences, existing resources, plans and
  constraints. These must be established by the supplied evidence and cited exactly.
* Proposed advice: a useful action, option or selection criterion based on those premises.
  This is a new suggestion, so it need not appear in the history. Ordinary practical knowledge
  may connect a supported preference or resource to a reasonable proposed action. State the
  action as a suggestion, never as a claim that the user already did it.
Do not demand a previous visit to a new destination or a recorded instruction for an owned
resource before giving tailored advice. Transfer only the relevant preference, not an old
location's establishments or an unrelated person's history.
If exact local availability, an event schedule, a product specification or a diagnosis is
unknown, do not invent it. Offer useful tailored criteria or cautious actions supported by
known premises, and state which specific detail would still need checking. Such uncertainty
does not require withholding all supported advice. For health concerns distinguish a possible
factor from a diagnosis or proven cause.
Use ANSWER when there is relevant evidence supporting useful personalized advice. Cite the
remembered premises, not the novel suggestion. Use ABSTAIN when no relevant personal premise
is supplied or when the request actually requires an unavailable historical fact. Keep the
same JSON schema and exact body-quote requirements above.
"""
