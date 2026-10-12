from dataclasses import replace
from datetime import date
from decimal import Decimal
import json

import pytest

from dive_memory.models import Memory, RetrievalItem, EvidenceState
from dive_memory.oracle.cited_calculation import calculate, number, date_range
from dive_memory.oracle.calculating_reader import CalculatingReader, VERSION, routes_to_calculator
from dive_memory.oracle.indexed_reader import RULES as BASE_RULES
from dive_memory.oracle.providers import ScriptedChatClient
from dive_memory.oracle.reader import OracleReaderRequest, ReaderSpec, reader_policy, render_reader_context


def item(text, i=0, *, anchor="2024-03-15", observed=None, refs=None):
    return RetrievalItem(Memory(str(i),"u","episode",EvidenceState.OBSERVATION,text,{"role":"user"},
        valid_from=anchor,observed_at=observed or anchor),1,source_refs=refs or ["e"+str(i)])


def request(texts, question="How many items do I have in total?", question_date="2024-04-15"):
    items = tuple(t if isinstance(t,RetrievalItem) else item(t,i) for i,t in enumerate(texts))
    r = OracleReaderRequest(question,question_date,question_date,items,"",token_budget=6000,
                            policy=reader_policy("cited_calculation"))
    return replace(r,context_text=render_reader_context(r))


def operand(index,quote,label,value=None):
    o = {"index":index,"quote":quote,"label":label}
    if value is not None:
        o["value"] = value
    return o


def plan(op,operands,unit=""):
    return {"status":"CALCULATE","operation":op,"operands":operands,"unit":unit}


@pytest.mark.parametrize("text,expected", [
    ("0.1", ".1"),("-4","-4"),("1,500.25","1500.25"),("four","4"),
    ("twenty-five","25"),("one hundred and six","106"),("two thousand three hundred forty five","2345"),
    ("half",".5"),
])
def test_supported_numbers_are_exact(text,expected):
    assert number(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["nan","1,2","and","one two","twenty nineteen","about five","one+two","__import__('os')","1e9"])
def test_ambiguous_or_executable_number_text_is_rejected(text):
    with pytest.raises(ValueError):
        number(text)


def test_decimal_arithmetic_and_sentence_final_operand_keep_exact_sources():
    r = request(["I paid 0.1 for a stamp.","I paid 0.2 for a card."])
    p = plan("sum",[operand(0,r.items[0].memory.content,"stamp","0.1"),
                    operand(1,r.items[1].memory.content,"card","0.2")],"dollars")
    d = calculate(p,r)
    assert d.answer == "0.3 dollars" and len(d.citations) == 2
    assert d.citations[1].source_refs == ("e1",)
    r = request(["I have books, total 17."])
    assert calculate(plan("sum",[operand(0,r.items[0].memory.content,"books","17")]),r).answer == "17"


def test_mean_difference_and_ratio_are_deterministic():
    r = request(["The fare was 12 dollars.","The discount was 3 dollars."],
                question="What is the average, difference and ratio of the fare and discount?")
    os = [operand(0,r.items[0].memory.content,"fare","12"),operand(1,r.items[1].memory.content,"discount","3")]
    assert calculate(plan("mean",os),r).answer == "7.5"
    assert calculate(plan("difference",os),r).answer == "9"
    assert calculate(plan("ratio",os),r).answer == "4"


@pytest.mark.parametrize("bad", [
    operand(True,"I bought 30 books.","books","30"),
    operand(1,"I bought 30 books.","books","30"),
    operand(0,"I bought 3 books.","books","3"),
    operand(0,"I bought 30 books.","books","3"),
    operand(0,"I bought 30 books.","coins","30"),
])
def test_operand_requires_visible_quote_entity_and_complete_number(bad):
    with pytest.raises(ValueError):
        calculate(plan("sum",[bad]),request(["I bought 30 books."]))


def test_duplicate_sources_and_count_aliases_cannot_double_count():
    q = "I bought 3 books."
    r = request([item(q,0,refs=["same"]),item(q,1,refs=["same"])])
    with pytest.raises(ValueError,match="repeated"):
        calculate(plan("sum",[operand(0,q,"books","3"),operand(1,q,"books","3")]),r)
    q = "I use a green bicycle and a helmet."
    r = request([q])
    assert calculate(plan("count",[operand(0,q,"green bicycle"),operand(0,q,"helmet")],"items"),r).answer == "2 items"
    with pytest.raises(ValueError,match="overlapping"):
        calculate(plan("count",[operand(0,q,"green bicycle"),operand(0,q,"bicycle")]),r)


def test_relative_dates_use_statement_anchor_and_calendar_leap_rules():
    assert date_range("two weeks ago",anchor="2024-03-15").lo == date(2024,3,1)
    assert date_range("one month ago",anchor="2024-03-31").lo == date(2024,2,29)
    assert date_range("last month",anchor="2024-03-31").hi == date(2024,2,29)
    assert date_range("February 29, 2024").lo == date(2024,2,29)
    r = request(["I repaired the fence three weeks ago.","I trimmed the hedge two weeks ago."],
                question="Which did I finish first?")
    d = calculate(plan("earliest",[operand(0,r.items[0].memory.content,"fence","three weeks ago"),
                                 operand(1,r.items[1].memory.content,"hedge","two weeks ago")]),r)
    assert d.answer == "fence"


def test_elapsed_days_uses_question_date_and_does_not_hide_range_uncertainty():
    r = request(["I visited the museum on 2024-03-01."],question_date="2024-03-15")
    p = plan("elapsed_days",[operand(0,r.items[0].memory.content,"museum","2024-03-01"),{"question_date":True}])
    assert calculate(p,r).answer == "14 days"
    r = request(["I visited the museum in February 2024."],question_date="2024-03-15")
    p["operands"][0] = operand(0,r.items[0].memory.content,"museum","February 2024")
    with pytest.raises(ValueError,match="precise"):
        calculate(p,r)
    p["operation"] = "elapsed_months"
    assert calculate(p,r).answer == "1 calendar months"


@pytest.mark.parametrize("value",["about two weeks ago","three months before my trip","May 12","last week"])
def test_unsupported_dates_are_not_invented(value):
    with pytest.raises(ValueError):
        date_range(value,anchor="2024-03-15")


def test_hidden_observation_date_cannot_replace_visible_validity_anchor():
    q = "I went to the museum yesterday."
    r = request([item(q,anchor="2024-03-01",observed="2024-03-15")])
    with pytest.raises(ValueError):
        calculate(plan("elapsed_days",[operand(0,q,"museum","yesterday"),{"question_date":True}]),r)


def reader(client):
    return CalculatingReader(client,ReaderSpec("test","test",prompt_version=VERSION,max_tokens=1024))


def test_calculation_result_is_computed_without_a_second_model_answer():
    q = "I have 7 books and 6 records."
    p = plan("sum",[operand(0,q,"books","7"),operand(0,q,"records","6")],"items")
    c = ScriptedChatClient(responses=[json.dumps(p)],input_tokens_per_call=100,output_tokens_per_call=50)
    d = reader(c).generate(request([q]))
    assert d.hypothesis == "13 items" and d.calls == 1
    assert json.loads(d.detail)["calculation_plan"] == p


def test_unsupported_plan_falls_back_with_recorded_cost_and_exact_old_prompt():
    q = "I have 7 books."
    old = {"status":"ANSWER","answer":"7 books","citations":[{"index":0,"quote":q}],"missing":[]}
    c = ScriptedChatClient(responses=[json.dumps(plan("sum",[operand(0,q,"books","99")])),json.dumps(old)],
                           input_tokens_per_call=100,output_tokens_per_call=50)
    d = reader(c).generate(request([q]))
    assert d.hypothesis == "7 books" and d.calls == 2 and d.usage.input_tokens == 200
    assert d.attempt_errors[0]["reason_code"] == "CALCULATION_FALLBACK"
    assert BASE_RULES in c.prompts[1]


def test_non_calculation_route_and_genuine_refusal_do_not_add_a_model_call():
    q = "I have 7 books."
    raw = json.dumps({"status":"ABSTAIN","answer":None,"citations":[],"missing":["destination"]})
    c = ScriptedChatClient(responses=[raw])
    assert reader(c).generate(request([q],question="Where did I travel?")).calls == 1
    assert BASE_RULES in c.prompts[0]
    c = ScriptedChatClient(responses=[raw])
    assert reader(c).generate(request([q],question="How many trips did I take?")).calls == 1
    assert routes_to_calculator("How many items did I buy?")


def test_calculator_rejects_non_visible_context():
    with pytest.raises(ValueError,match="budget-visible"):
        reader(ScriptedChatClient()).generate(replace(request(["I have 7 books."]),context_text="hidden archive"))


@pytest.mark.parametrize("literal,value", [("3,000", "3"), ("-3", "3"),
    ("four hundred", "four"), ("twenty-five", "five"), ("30.5", "30")])
def test_partial_numeric_expressions_cannot_supply_an_operand(literal, value):
    quote = f"I have {literal} books."
    with pytest.raises(ValueError):
        calculate(plan("sum", [operand(0, quote, "books", value)]), request([quote]))


def test_equal_numbers_for_distinct_categories_do_not_collapse():
    quote = "I have 7 books and 7 records."
    result = calculate(plan("sum", [operand(0, quote, "books", "7"),
                                   operand(0, quote, "records", "7")]), request([quote]))
    assert result.answer == "14"


def test_duplicate_operand_cannot_hide_in_different_quote_or_source_subset():
    quote = "Yesterday I bought 3 books."
    r = request([item(quote, 0, refs=["same"]), item(quote, 1, refs=["same", "another"])])
    with pytest.raises(ValueError, match="repeated"):
        calculate(plan("sum", [operand(0, quote, "books", "3"),
            operand(1, "I bought 3 books.", "books", "3")]), r)
    with pytest.raises(ValueError, match="repeated|overlapping"):
        calculate(plan("count", [operand(0, quote, "books"),
            operand(1, "I bought 3 books.", "books")]), r)


def test_tiny_decimals_are_preserved_and_nonterminating_ratios_fall_back():
    quote = "I paid 0.0000001 dollars."
    assert calculate(plan("sum", [operand(0, quote, "dollars", "0.0000001")]), request([quote])).answer == "0.0000001"
    quote = "I have 2 books and 3 records."
    with pytest.raises(ValueError, match="nonterminating"):
        calculate(plan("ratio", [operand(0, quote, "books", "2"),
            operand(0, quote, "records", "3")]), request([quote], question="What is the ratio of books to records?"))


@pytest.mark.parametrize("text", ["one and two", "twenty zero", "two thousand three million", "one thousand two thousand"])
def test_ambiguous_number_word_grammar_is_rejected(text):
    with pytest.raises(ValueError):
        number(text)


@pytest.mark.parametrize("question", ["What is my account name?", "What did I eat for breakfast?", "Which border did I cross?"])
def test_calculation_route_uses_words_not_substrings(question):
    assert not routes_to_calculator(question)


def test_aggregate_wording_routes_without_benchmark_labels():
    assert routes_to_calculator("What is the average fare I paid?")
    assert routes_to_calculator("我总共买了多少本书？")


def test_spelled_counts_after_conjunction_and_half_remain_supported():
    quote = "I have seven books and six records."
    assert calculate(plan("sum", [operand(0, quote, "books", "seven"),
        operand(0, quote, "records", "six")]), request([quote])).answer == "13"
    quote = "I walked a half mile."
    assert calculate(plan("sum", [operand(0, quote, "mile", "a half")]), request([quote])).answer == "0.5"


def test_duplicate_numeric_value_cannot_use_spelled_alias_to_bypass_check():
    quote = "I bought three books, 3 books in total."
    with pytest.raises(ValueError, match="repeated"):
        calculate(plan("sum", [operand(0, quote, "books", "three"),
            operand(0, quote, "books", "3")]), request([quote]))


def test_currency_operands_are_exact_and_conversion_is_rejected():
    quote = "I raised $250; my goal was $200."
    operands = [operand(0, quote, "raised", "$250"), operand(0, quote, "goal", "$200")]
    r = request([quote], question="How much more did I raise than my goal?")
    assert calculate(plan("difference", operands, "dollars"), r).answer == "50 dollars"
    with pytest.raises(ValueError, match="unit"):
        calculate(plan("difference", operands, "euros"), r)
    quote = "I have $250 and €200."
    with pytest.raises(ValueError, match="conversion"):
        calculate(plan("sum", [operand(0, quote, "$250", "$250"),
            operand(0, quote, "€200", "€200")]), request([quote]))


def test_manual_planner_answer_uses_the_original_reader_instead():
    quote = "I have 7 books and 6 records."
    raw = {"status":"ANSWER", "answer":"14 items",
           "citations":[{"index":0,"quote":quote}], "missing":[]}
    supported = {**raw, "answer":"13 items"}
    client = ScriptedChatClient(responses=[json.dumps(raw), json.dumps(supported)])
    result = reader(client).generate(request([quote]))
    assert result.calls == 2 and result.hypothesis == "13 items"
    assert result.attempt_errors[0]["reason_code"] == "CALCULATION_FALLBACK"


def test_current_quantity_is_not_a_delta_between_updates():
    before, after = "I'm currently on page 200.", "I'm now on page 220."
    operands = [operand(1, after, "page", "220"), operand(0, before, "page", "200")]
    with pytest.raises(ValueError, match="operation"):
        calculate(plan("difference", operands, "pages"), request([before, after],
            question="How many pages have I read so far?"))
    assert calculate(plan("difference", operands, "pages"), request([before, after],
        question="How many more pages have I read since the earlier update?")).answer == "20 pages"


@pytest.mark.parametrize("prefix", ["about", "at least", "more than", "roughly", "between"])
def test_cropping_qualifier_cannot_create_an_exact_count(prefix):
    text = f"I bought {prefix} 5 books."
    with pytest.raises(ValueError, match="approximate"):
        calculate(plan("sum", [operand(0, "5 books.", "books", "5")]), request([text]))


def test_cropping_approximate_relative_date_is_rejected():
    text = "I visited the museum about two weeks ago."
    with pytest.raises(ValueError, match="approximate"):
        calculate(plan("elapsed_days", [operand(0, "museum about two weeks ago.", "museum", "two weeks ago"),
            {"question_date": True}]), request([text], question="How many days since I visited?"))


@pytest.mark.parametrize("text,value", [("I bought 3-5 books.", "3"),
    ("I bought 3–5 books.", "5"), ("I used 1/2 cups.", "1"),
    ("The balance is minus five dollars.", "five")])
def test_cropped_range_fraction_or_word_sign_is_not_an_exact_number(text, value):
    label = "books" if "books" in text else "cups" if "cups" in text else "dollars"
    with pytest.raises(ValueError):
        calculate(plan("sum", [operand(0, text, label, value)]), request([text]))
