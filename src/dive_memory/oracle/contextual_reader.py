"""Context-sensitive reading instructions on the existing strict citation path.

These rules guide model interpretation; code validates structure and quotation
provenance, not semantic entailment. Parsing, repair limits and routing are inherited.
"""

from .adjudicated_reader import EvidenceReferee
from .indexed_reader import IndexedEvidenceReader, RULES


VERSION = "oracle-contextual-reader-v1"
REFEREE_VERSION = "oracle-contextual-referee-v1"

CONTEXTUAL_RULES = """
Use the minimum semantic detail needed to answer the question:
1. A uniquely identifying description can answer which object/person; do not require a proper
name or exact year unless the question or derivation needs it. General advice is not personal
history; ownership or a signature does not establish a purchase. Preserve entity and project identity.
2. Recent conversational context together with the statement date can constrain an omitted
time or year when the wording supports that link. Preserve cross-year intervals, explicit dates,
contrary evidence and genuine ambiguity; a record date alone does not date every described event.
3. For the same entity, attribute and event phase, prefer a newer user statement that updates
or explicitly recalls the relevant fact, within the requested time scope. Recency alone does not
resolve unrelated conflicts. Keep different projects, events, plans and completed actions separate;
absence of a statement is not a negative fact.
4. Visible time metadata can anchor relative dates or support a calculation. Citation quotes
must still be exact contiguous body text supporting its premises, never evidence headers, metadata
or a computed result absent from the body. Do not invent a body quote to express the derived date.
"""


class ContextualEvidenceReader(IndexedEvidenceReader):
    prompt_version = VERSION
    rules = RULES + CONTEXTUAL_RULES


class ContextualEvidenceReferee(EvidenceReferee):
    prompt_version = REFEREE_VERSION
    rules = EvidenceReferee.rules + CONTEXTUAL_RULES
