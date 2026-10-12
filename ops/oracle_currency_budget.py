"""Sequential peak-price budget bound, including uncertain previous requests.

DeepSeek Flash CNY peak: input 2/M, output 8/M, treating all input as uncached.
The reservation is charged at the more expensive output rate. Account balance
guards remain separate: neither a cached discount nor delayed balance settlement
can be used to grant additional experiment allowance.
"""
from decimal import Decimal
import json
from pathlib import Path

from oracle_low_cost_pilot import PilotStopped


def peak_cost(ledger_paths):
    inputs = outputs = unknown = calls = 0
    for path in dict.fromkeys(Path(p).resolve() for p in ledger_paths):
        if not path.exists():
            continue
        rows = json.loads(path.read_text(encoding="utf-8"))["calls"]
        for row in rows:
            calls += 1
            i, o = row.get("input_tokens"), row.get("output_tokens")
            if i is None or o is None:
                r = row["charged_budget"]
                if type(r) is not int or r < 0:
                    raise PilotStopped("Invalid uncertain reservation; no paid request.")
                unknown += r
            else:
                if any(type(n) is not int or n < 0 for n in (i, o)):
                    raise PilotStopped("Invalid observed usage; no paid request.")
                inputs += i
                outputs += o
    cost = Decimal(2 * inputs + 8 * outputs + 8 * unknown) / Decimal(1_000_000)
    return {"calls": calls, "input_tokens": inputs, "output_tokens": outputs,
            "unknown_reserved_tokens": unknown, "peak_upper_cny": str(cost)}


class CurrencyBudget:
    def __init__(self, ledger_paths, *, limit=Decimal("9.00")):
        self.paths, self.limit = tuple(ledger_paths), Decimal(limit)

    def __call__(self, next_reserved_tokens):
        if type(next_reserved_tokens) is not int or next_reserved_tokens < 0:
            raise PilotStopped("Invalid next-request reservation.")
        spent = Decimal(peak_cost(self.paths)["peak_upper_cny"])
        reserved = Decimal(8 * next_reserved_tokens) / Decimal(1_000_000)
        if spent + reserved >= self.limit:
            raise PilotStopped("Aggregate peak-price budget bound reached; no further request sent.")
