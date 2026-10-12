"""Opt-in, bounded verbatim context for personal facts embedded in mixed discourse."""
from dataclasses import dataclass
import re
from .episodic_retention import EpisodicRetentionPolicy, _DETAIL, _PERSON, PREDICATE
from .narrative_retention import _FICTIONAL_SCOPE, _PRECISE_NONFACTUAL, _PROSPECTIVE
from .extraction import Candidate
from .gate import GateDecision, GateAction, _sensitive

VERSION='contextual-personal-fact-v1'
_PAST=re.compile(r'\b(?:I|we)\s+(?:(?:have|had|just|recently|already|also|actually)\s+)*(?:did|used to|swam|played|met|bought|received|earned|[a-z]{3,}ed)\b',re.I)
_SELF_STATE=re.compile(r'\b(?:I|we)\s+(?:have|own|use|prefer|like|live|work)\b',re.I)
_STATE=re.compile(r'\b(?:my|our)\s+(?:[\w\x27-]+[\s,]+){1,10}?(?:is|are|was|were|has|have)\b',re.I)
_RELATIVE=re.compile(r'\bwhich\s+(?:is|are|was|were)\s+(?:usually|normally|always)\b',re.I)
_HABIT=re.compile(r'\b(?:like|as|considering)\s+(?:I|we)\s+(?:(?:usually|normally|always)\s+)*(?:do|go|play|wake|use|like|have)\b',re.I)
_ATTRIBUTE=re.compile(r'\b(?:my|our)\s+(?:[\w\x27-]+\s+){1,6}(?:of|at|with)\s+\d',re.I)
_LOCATION=re.compile(r'\b(?:my|our)\s+(?:[\w\x27-]+\s+){1,4}(?:in|at)\s+(?:[A-Z][\w-]+|(?:the|a|an)\s+[a-z]+)')
_CHANGE=re.compile(r'\b(?:now|currently|again|same|every|usually|normally|always)\b',re.I)
_UNCERTAIN=re.compile(r'\b(?:maybe|perhaps|probably|likely|possibly|hypothetical)\b',re.I)


def has_contextual_fact(sentence):
    if _FICTIONAL_SCOPE.search(sentence) or not _PERSON.search(sentence):
        return False
    normalized=re.sub(r"\bI'm\b",'I am',sentence,flags=re.I)
    for pattern in (_PAST,_SELF_STATE,_STATE,_RELATIVE,_HABIT,_ATTRIBUTE,_LOCATION):
        for match in pattern.finditer(normalized):
            # Preserve appositives in the subject; inspect the predicate until its
            # next discourse boundary so another clause's intention is not inherited.
            tail=re.split(r'[,;?!]|\b(?:but|so|and)\b',normalized[match.end():],maxsplit=1,flags=re.I)[0]
            clause=normalized[match.start():match.end()]+tail
            if _PRECISE_NONFACTUAL.search(clause) or _PROSPECTIVE.search(clause) or _UNCERTAIN.search(clause):
                continue
            if re.search(r'\b(?:have|need)\s+to\b',clause,re.I):
                continue
            # A modal immediately governing a past-looking phrase is not an assertion.
            prefix=normalized[max(0,match.start()-32):match.start()]
            if re.search(r'\b(?:wish(?:ing)?|hop(?:e|ing)|imagine|suppose)\s+(?:that\s+)?$',prefix,re.I):
                continue
            if (pattern is _LOCATION or _DETAIL.search(clause) or _CHANGE.search(clause)
                    or re.search(r'\bused to\b',clause,re.I)
                    or re.search(r'\b[A-Z][a-z]{2,}\b',clause[1:])):
                return True
    return False


@dataclass(frozen=True)
class ContextualFactRetention:
    base: object | None = None
    max_items: int = 4
    max_window_chars: int = 1800
    max_turn_chars: int = 8000

    def __post_init__(self):
        if min(self.max_items,self.max_window_chars,self.max_turn_chars)<=0:
            raise ValueError('contextual retention limits must be positive')

    def candidates(self,text,*,source_type,observed_at,existing):
        prior=self.base.candidates(text,source_type=source_type,observed_at=observed_at,existing=existing) if self.base else []
        if source_type!='user' or len(text)>self.max_turn_chars or _sensitive(text):
            return prior
        sentences=list(EpisodicRetentionPolicy._sentences(text))
        windows=[]
        for i,(start,sentence) in enumerate(sentences):
            if len(sentence)<15 or len(sentence)>self.max_window_chars or not has_contextual_fact(sentence):
                continue
            lo,hi=i,i
            for neighbor in (i-1,i+1):
                if 0<=neighbor<len(sentences):
                    a,b=min(lo,neighbor),max(hi,neighbor)
                    if sentences[b][0]+len(sentences[b][1])-sentences[a][0]<=self.max_window_chars:
                        lo,hi=a,b
            a,b=sentences[lo][0],sentences[hi][0]+len(sentences[hi][1])
            if windows and a<=windows[-1][1] and b-windows[-1][0]<=self.max_window_chars:
                windows[-1]=(windows[-1][0],max(b,windows[-1][1]))
            elif not any(a>=x and b<=y for x,y in windows):
                windows.append((a,b))
        added=[]
        for a,b in windows:
            content=text[a:b]
            if any(content in c.content for c in [*existing,*prior,*added]):
                continue
            gate=GateDecision(True,.55,.8,.6,'medium_term','verbatim context of embedded personal facts',
                GateAction.WRITE,'CONTEXTUAL_FACT_OBSERVATION',{'contextual_fact_retention':1.0},VERSION)
            added.append(Candidate(content,'episode','OBSERVATION',{'subject':'user','predicate':PREDICATE,
                'assertion':'reported_context','role':'user','source_span':[a,b],'retention_policy':VERSION,
                'context_may_include_questions_or_hypotheses':True},gate,observed_at))
            if len(added)>=self.max_items:break
        return [*prior,*added]
