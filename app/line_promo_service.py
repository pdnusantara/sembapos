"""Promo baris kasir otomatis (per produk / kategori, terjadwal)."""
from __future__ import annotations

import calendar
from datetime import datetime
from typing import Any, List, TYPE_CHECKING

from .models import PosLinePromoRule, Product

if TYPE_CHECKING:
    pass


def _rule_discount_rupiah(rule: PosLinePromoRule, line_gross: float) -> float:
    g = max(0.0, float(line_gross or 0))
    if g <= 0:
        return 0.0
    if rule.discount_type == 'percent':
        d = g * (float(rule.discount_value or 0) / 100.0)
        if rule.max_discount is not None:
            d = min(d, float(rule.max_discount))
    else:
        d = float(rule.discount_value or 0)
    return max(0.0, min(d, g))


def best_auto_line_discount_rupiah(
    tenant_id: int,
    product: Product,
    qty: float,
    line_gross: float,
    *,
    now: datetime | None = None,
) -> float:
    """Diskon maksimal (rupiah) dari semua rule yang cocok pada waktu now."""
    now = now or datetime.utcnow()
    rules = (
        PosLinePromoRule.query.filter_by(tenant_id=tenant_id, aktif=True)
        .filter(PosLinePromoRule.start_at <= now, PosLinePromoRule.end_at >= now)
        .order_by(PosLinePromoRule.priority.desc(), PosLinePromoRule.id.desc())
        .all()
    )
    best = 0.0
    q = float(qty or 0)
    pid = int(product.id)
    cid = int(product.category_id or 0)
    g = float(line_gross or 0)
    for r in rules:
        if q + 1e-9 < float(r.min_qty or 1):
            continue
        ok = False
        if r.scope == 'product' and r.product_id and int(r.product_id) == pid:
            ok = True
        elif r.scope == 'category' and r.category_id and int(r.category_id) == cid:
            ok = True
        if not ok:
            continue
        d = _rule_discount_rupiah(r, g)
        if d > best:
            best = d
    return best


def enforce_line_promo_floor(line_diskon: float, auto_min: float, line_gross: float) -> float:
    """Minimal diskon = promo otomatis; kasir boleh menambah di atasnya."""
    g = max(0.0, float(line_gross or 0))
    d = max(float(line_diskon or 0), float(auto_min or 0))
    return min(d, g)


def line_promo_rules_for_pos_client(tenant_id: int) -> List[dict[str, Any]]:
    """JSON untuk POS: rule aktif & dalam periode (server time UTC)."""
    now = datetime.utcnow()
    rules = (
        PosLinePromoRule.query.filter_by(tenant_id=tenant_id, aktif=True)
        .filter(PosLinePromoRule.start_at <= now, PosLinePromoRule.end_at >= now)
        .order_by(PosLinePromoRule.priority.desc(), PosLinePromoRule.id.desc())
        .all()
    )
    out: List[dict[str, Any]] = []
    for r in rules:
        sa = r.start_at
        ea = r.end_at
        start_ts = int(calendar.timegm(sa.timetuple()) * 1000) if sa else 0
        end_ts = int(calendar.timegm(ea.timetuple()) * 1000) if ea else 0
        out.append(
            {
                'scope': r.scope,
                'product_id': int(r.product_id) if r.product_id else None,
                'category_id': int(r.category_id) if r.category_id else 0,
                'discount_type': r.discount_type,
                'discount_value': float(r.discount_value or 0),
                'max_discount': float(r.max_discount) if r.max_discount is not None else None,
                'min_qty': float(r.min_qty or 1),
                'start_ts': start_ts,
                'end_ts': end_ts,
            }
        )
    return out
