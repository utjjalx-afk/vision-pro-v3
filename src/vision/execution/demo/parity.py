"""Audit broker facts against declared expectations without inventing CFD paper economics."""

from decimal import Decimal


def parity(head):
    d = head["details"]
    req = d.get("request")
    if req is None:
        raise ValueError("Prechecked demo request required")
    fill = d.get("fill_price")
    expected = Decimal(d["expected_entry"])
    slippage = (
        None if fill is None else (Decimal(fill) - expected) * (1 if req["side"] == "LONG" else -1)
    )
    return {
        "version": "phase12-parity-v1",
        "client_order_id": head["client_order_id"],
        "intent_id": head["lineage"]["intent_id"],
        "sizing_receipt_id": head["lineage"]["sizing_receipt_id"],
        "currency": d["currency"],
        "expected_entry": str(expected),
        "actual_fill": fill,
        "spread": d["spread"],
        "slippage_price": str(slippage) if slippage is not None else None,
        "requested_volume": req["volume"],
        "requested_sl": req["stop"],
        "native_expected_risk": d["risk"],
        "native_expected_margin": d["margin"],
        "native_actual_fill_risk": d.get("actual_fill_risk"),
        "native_actual_fill_margin": d.get("actual_fill_margin"),
        "broker_realized_pnl": d.get("realized_pnl"),
        "broker_facts": d.get("facts"),
        "paper_cfd_pnl": None,
        "paper_cfd_margin": None,
        "paper_cfd_fees": None,
        "paper_cfd_status": "UNAVAILABLE_SPOT_ONLY_PAPER_BROKER",
        "reliability_update": False,
    }
