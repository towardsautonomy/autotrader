"""Tool schemas shared by scout and specialist agents.

``propose_structure`` is the specialist's terminal tool — it captures an
actionable options setup rather than just a directional bias. The
decision agent reads these as inputs, picks the strongest, and still
emits a ``propose_trade`` call for the existing risk engine.

``emit_candidates`` is the scout's terminal tool — it filters a
candidate pool down to the tickers genuinely worth deep research this
cycle.
"""

from __future__ import annotations

PROPOSE_STRUCTURE_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_structure",
        "description": (
            "Commit a concrete options structure for this ticker. Be specific: "
            "what structure, which legs, what direction, what you're being paid "
            "or paying, what the max loss is. If the IV regime or liquidity "
            "kills the idea, set structure='skip' and explain in rationale."
        ),
        "parameters": {
            "type": "object",
            "required": [
                "symbol",
                "direction",
                "structure",
                "confidence",
                "rationale",
            ],
            "properties": {
                "symbol": {"type": "string"},
                "direction": {
                    "type": "string",
                    "enum": ["bullish", "bearish", "neutral", "avoid"],
                },
                "structure": {
                    "type": "string",
                    "description": (
                        "One of: long_call, long_put, debit_call_spread, "
                        "debit_put_spread, credit_call_spread, credit_put_spread, "
                        "iron_condor, covered_call, cash_secured_put, "
                        "stock_long, skip"
                    ),
                },
                "legs": {
                    "type": "array",
                    "description": (
                        "Each leg = {side: 'buy'|'sell', right: 'call'|'put'|'stock', "
                        "strike: number, expiry: 'YYYY-MM-DD', quantity: int}"
                    ),
                    "items": {
                        "type": "object",
                        "required": ["side"],
                        "properties": {
                            "side": {
                                "type": "string",
                                "enum": ["buy", "sell"],
                            },
                            "right": {
                                "type": "string",
                                "enum": ["call", "put", "stock"],
                            },
                            "strike": {"type": "number"},
                            "expiry": {"type": "string"},
                            "quantity": {"type": "integer"},
                        },
                    },
                },
                "max_loss_usd": {
                    "type": "number",
                    "description": (
                        "Worst-case dollar loss per structure. Undefined/unlimited "
                        "structures (naked short calls) should be refused — prefer "
                        "defined-risk spreads."
                    ),
                },
                "max_profit_usd": {
                    "type": "number",
                    "description": (
                        "Best-case dollar profit per structure; use a large number "
                        "for effectively unlimited (long calls)."
                    ),
                },
                "entry_price_estimate": {
                    "type": "number",
                    "description": (
                        "Net debit paid (positive) or credit received (negative) "
                        "per structure at expected mid."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "catalyst": {"type": "string"},
                "risks": {"type": "string"},
                "rationale": {
                    "type": "string",
                    "description": "Two to four sentences the decision agent reads.",
                },
            },
        },
    },
}


EMIT_CANDIDATES_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_candidates",
        "description": (
            "Commit the filtered list of tickers worth deep research this "
            "cycle. Keep the list short (3-8 names). Every pick needs a "
            "one-line reason a specialist can act on."
        ),
        "parameters": {
            "type": "object",
            "required": ["picks"],
            "properties": {
                "picks": {
                    "type": "array",
                    "minItems": 0,
                    "items": {
                        "type": "object",
                        "required": ["symbol", "reason"],
                        "properties": {
                            "symbol": {"type": "string"},
                            "reason": {
                                "type": "string",
                                "description": (
                                    "One line, specific. 'hot IV + earnings "
                                    "tomorrow' beats 'looks interesting'."
                                ),
                            },
                            "score": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                                "description": (
                                    "Optional — your own 0-1 confidence this "
                                    "ticker has real edge this cycle."
                                ),
                            },
                        },
                    },
                },
                "notes": {
                    "type": "string",
                    "description": "Optional overall market read.",
                },
            },
        },
    },
}


__all__ = ["PROPOSE_STRUCTURE_TOOL", "EMIT_CANDIDATES_TOOL"]
