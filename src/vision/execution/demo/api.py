"""Exact authenticated DTOs; raw native request dictionaries are never accepted."""

from vision.analysis.synthesizer.contracts import Direction
from vision.broker.codec import decimal, dto
from vision.broker.models import SizeRequest, SizingPolicy
from vision.core.codec import utc_string
from vision.execution.demo.models import RiskContext
from vision.execution.demo.parity import parity
from vision.intents.models import TradeIntent


def dispatch(gateway, route, value):
    if type(value) is not dict:
        raise ValueError("Demo envelope required")
    expected = {
        "/v1/demo/arm": {"account_identity", "operator_confirmation"},
        "/v1/demo/disarm": set(),
        "/v1/demo/halt": set(),
        "/v1/demo/monitor": {"client_order_id"},
        "/v1/demo/close": {"client_order_id", "operator_confirmation"},
        "/v1/demo/execute": {
            "client_order_id",
            "intent",
            "request",
            "sizing_policy",
            "sizing_audit",
            "sized_at",
            "risk_context",
            "tp",
        },
    }
    if route not in expected or set(value) != expected[route]:
        raise ValueError("Exact supported demo fields required")
    data = dict(value)
    if route.endswith("/execute"):
        intent = dict(data["intent"])
        intent["direction"] = Direction(intent["direction"])
        for key in (
            "thesis_ids",
            "evidence_hashes",
            "reliability_revisions",
            "invalidation_conditions",
        ):
            if type(intent[key]) is not list or len(intent[key]) > 256:
                raise ValueError("Bounded intent lineage array required")
            intent[key] = tuple(intent[key])
        data["intent"] = dto(TradeIntent, intent)
        data["request"] = dto(SizeRequest, data["request"])
        data["sizing_policy"] = dto(SizingPolicy, data["sizing_policy"])
        data["risk_context"] = dto(RiskContext, data["risk_context"])
        data["sized_at"] = utc_string(data["sized_at"])
        data["tp"] = None if data["tp"] is None else decimal(data["tp"])
        head = gateway.execute(**data)
    elif route.endswith("/monitor"):
        head = gateway.monitor(**data)
    elif route.endswith("/close"):
        head = gateway.close(**data)
    else:
        getattr(gateway, route.rsplit("/", 1)[1])(**data)
        return {"state": str(gateway.state), "live_enabled": False}
    return {
        "order": head,
        "parity": parity(head) if "request" in head["details"] else None,
        "live_enabled": False,
    }
