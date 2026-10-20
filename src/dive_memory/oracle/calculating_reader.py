"""Opt-in typed calculation route, with original indexed-reader fallback.

Only visible question wording routes a request. The planner sees the same
budget-visible evidence, not the benchmark type, answer or marked source turns.
"""
import json
import re
from dataclasses import replace
from decimal import InvalidOperation
from hashlib import sha256
from time import perf_counter

from ..answering import AnswerStatus, GroundedAnswerDecision
from ..token_budget import ApproximateTokenCounter
from .cited_calculation import calculate
from .indexed_reader import RULES as BASE_RULES
from .indexed_reader import VERSION as BASE_VERSION
from .indexed_reader import IndexedEvidenceReader, parse_indexed
from .reader import (
    TokenUsage,
    assert_prompt_is_clean,
    map_decision,
    reader_policy,
    render_reader_context,
)

VERSION = "oracle-cited-calculation-reader-v4"
RULES = BASE_RULES.split("Return only JSON:")[0] + """
Use a deterministic calculator for arithmetic or date ordering when ALL required operands are
supported. Do not calculate a final total yourself. Choose the operation and copy the operands.
Return only one JSON object:
{"status":"CALCULATE","operation":"sum|mean|difference|product|ratio|count|elapsed_days|elapsed_weeks|elapsed_months|earliest|latest|date_order",
 "operands":[{"index":integer,"quote":"exact evidence passage","label":"exact short event/item name in that quote","value":"exact number or date phrase in that quote"}],"unit":"short unit or empty"}.
Each operand quote must be a contiguous substring of the indexed visible evidence. The label and
value must also appear exactly in that quote. Use one distinct operand per required event/item;
exclude repeated summaries, unrelated items, and components outside the requested time/scope.
Copy the label word for word; never paraphrase it ("raised", not "amount raised" unless those
exact words occur). Numeric currency values such as "$250" are supported without conversion.
Example evidence: [0] "I have 7 books and 6 records." A supported total plan is
{"status":"CALCULATE","operation":"sum","operands":[{"index":0,"quote":"I have 7 books and 6 records.","label":"books","value":"7"},{"index":0,"quote":"I have 7 books and 6 records.","label":"records","value":"6"}],"unit":"items"}.
Do not turn an assistant's hypothetical example or recommendation into a personal event.
For count, omit value: each distinct cited entity/event counts once. A directly stated numeric
count needs sum with that number, not count with one operand. For difference/ratio/product use
exactly two operands in the intended order. Mean uses equally weighted input numbers.
For elapsed time provide START, END; END may instead be {"question_date":true} when the question
asks how long ago. elapsed_months means calendar-month index difference, not days divided by 30.
Date values supported: YYYY-MM-DD, full month/day/year, month/year, today, yesterday, last month,
or N days/weeks/months/years ago. Only literal 'ago' is anchored to the displayed statement date;
do not use this for 'before my trip' or other event-relative offsets. Approximate expressions,
missing years and overlapping ranges cannot be silently converted into exact dates.
If calculation cannot express the supported answer, return {"status":"DELEGATE"}; the original
reader will answer from the same evidence. Do not delegate to obtain missing personal facts.
Return DELEGATE for a non-computational answer, recommendation, ambiguous operand or unsupported
expression. Do not return ANSWER or manually compute a result in this planning stage.
For genuinely missing necessary facts use:
{"status":"ABSTAIN","answer":null,"citations":[],"missing":["missing premise"]}.
"""


def routes_to_calculator(question):
    # Whole words avoid routing "account", "breakfast" or "border" merely
    # because the general evidence classifier matches a substring.
    return bool(re.search(
        r"\b(?:how (?:many|much|long|often)|number of|count|total|sum|average|mean|"
        r"difference|ratio|product|duration|elapsed|earlier|later|before|after|"
        r"first|last|latest|earliest|order|most recent)\b|"
        r"多少|几天|几周|几个月|几年|总共|总数|合计|平均|相差|最早|最晚|先后",
        str(question), re.I))


class CalculatingReader:
    def __init__(self,client,spec,*,policy=None):
        if spec.prompt_version != VERSION:
            raise ValueError("calculating reader requires its own version")
        self.client,self.spec,self.policy = client,spec,policy
        self.base = IndexedEvidenceReader(client,replace(spec,prompt_version=BASE_VERSION,max_tokens=512,
                                                       timeout_seconds=60.0),
                                           policy=reader_policy("indexed_evidence"))

    def generate(self,request):
        if request.context_text != render_reader_context(request):
            raise ValueError("calculator requires exact budget-visible evidence")
        if not routes_to_calculator(request.question):
            return self.base.generate(request)
        started = perf_counter()
        payload = {"question":request.question,"question_date":request.question_date,
                   "as_of":request.as_of,"context":request.context_text}
        assert_prompt_is_clean(payload)
        messages = [{"role":"system","content":RULES},
                    {"role":"user","content":json.dumps(payload,ensure_ascii=False,sort_keys=True)}]
        signature = json.dumps(messages,ensure_ascii=False,sort_keys=True)
        try:
            completion = self.client.complete(model=self.spec.model,messages=messages,temperature=self.spec.temperature,
                max_tokens=self.spec.max_tokens,timeout_seconds=self.spec.timeout_seconds,response_format={"type":"json_object"})
        except Exception as exc:  # noqa: BLE001 - provider boundary preserves transport errors separately from abstention
            d = GroundedAnswerDecision(AnswerStatus.ERROR,None,
                "READER_TIMEOUT" if isinstance(exc,TimeoutError) else "READER_FAILURE",
                missing_requirements=(type(exc).__name__,))
            return map_decision(d,usage=TokenUsage(0,0,"provider_observed_only"),
                latency_ms=(perf_counter()-started)*1000,calls=1)
        counter = ApproximateTokenCounter()
        known = completion.input_tokens is not None and completion.output_tokens is not None
        usage = TokenUsage(completion.input_tokens if known else counter.count(signature),
            completion.output_tokens if known else counter.count(completion.text),"provider" if known else "estimate")
        error,plan = None,None
        try:
            plan = json.loads(completion.text)
            if isinstance(plan,dict) and plan.get("status") == "CALCULATE":
                decision = calculate(plan,request)
            elif plan == {"status":"DELEGATE"}:
                error = "Planner delegated unsupported expression to original reader."
            elif isinstance(plan, dict) and plan.get("status") == "ABSTAIN":
                decision = parse_indexed(completion.text,request.items)
                if decision.reason_code == "UNSUPPORTED_READER_OUTPUT":
                    error = "; ".join(decision.missing_requirements)
            else:
                error = "Planner must calculate, delegate or identify a missing premise."
        except (ValueError,TypeError,KeyError,OverflowError,InvalidOperation) as exc:
            error = str(exc)
        if error is not None:
            fallback = self.base.generate(request)
            return replace(fallback,
                usage=TokenUsage(usage.input_tokens+fallback.usage.input_tokens,
                    usage.output_tokens+fallback.usage.output_tokens,
                    "provider" if known and fallback.usage.source == "provider" else "mixed_or_estimated"),
                latency_ms=(perf_counter()-started)*1000,calls=1+fallback.calls,
                prompt_sha256=sha256((signature+(fallback.prompt_sha256 or "")).encode()).hexdigest(),
                detail=json.dumps({"calculation_plan":plan,"fallback_reason":error},ensure_ascii=False),
                attempt_errors=({"reason_code":"CALCULATION_FALLBACK","detail":error,
                    "response_sha256":sha256(completion.text.encode()).hexdigest()},*fallback.attempt_errors))
        result = map_decision(decision,usage=usage,latency_ms=(perf_counter()-started)*1000,
            prompt_sha256=sha256(signature.encode()).hexdigest(),response_sha256=sha256(completion.text.encode()).hexdigest(),calls=1)
        return replace(result,detail=json.dumps({"calculation_plan":plan,"computed_answer":decision.answer},ensure_ascii=False))
