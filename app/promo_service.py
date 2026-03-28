from datetime import datetime
import json
from sqlalchemy import func

from .models import Voucher, VoucherRedemption


def _voucher_scope_category_ids(voucher):
    return {int(x.category_id) for x in (voucher.category_scopes or [])}


def _items_subtotal_for_scope(items, scoped_category_ids):
    if not scoped_category_ids:
        return sum(float(i.get('line_sub') or 0) for i in items)
    scoped = 0.0
    for i in items:
        cat = i.get('category_id')
        if cat in scoped_category_ids:
            scoped += float(i.get('line_sub') or 0)
    return scoped


def validate_voucher(tenant_id, voucher_code, member_id, subtotal, items):
    code = (voucher_code or '').strip().upper()
    if not code:
        return {'ok': False, 'message': 'Kode voucher kosong.'}

    now = datetime.utcnow()
    voucher = Voucher.query.filter(
        Voucher.tenant_id == tenant_id,
        func.upper(Voucher.kode) == code,
    ).first()
    if not voucher or not voucher.active:
        return {'ok': False, 'message': 'Voucher tidak ditemukan atau nonaktif.'}
    if now < voucher.start_at or now > voucher.end_at:
        return {'ok': False, 'message': 'Voucher di luar periode berlaku.'}
    if float(subtotal or 0) < float(voucher.min_spend or 0):
        return {'ok': False, 'message': f'Minimal belanja voucher Rp {voucher.min_spend:,.0f}.'}

    total_used = VoucherRedemption.query.filter_by(voucher_id=voucher.id).count()
    if voucher.max_usage_global and total_used >= int(voucher.max_usage_global):
        return {'ok': False, 'message': 'Kuota voucher habis.'}

    if member_id and voucher.max_usage_per_member:
        member_used = VoucherRedemption.query.filter_by(voucher_id=voucher.id, member_id=member_id).count()
        if member_used >= int(voucher.max_usage_per_member):
            return {'ok': False, 'message': 'Kuota voucher member sudah habis.'}

    scoped_category_ids = _voucher_scope_category_ids(voucher)
    scope_subtotal = _items_subtotal_for_scope(items, scoped_category_ids)
    if scope_subtotal <= 0:
        return {'ok': False, 'message': 'Voucher tidak berlaku untuk item di keranjang.'}

    if voucher.discount_type == 'percent':
        discount = scope_subtotal * (float(voucher.discount_value or 0) / 100.0)
        if voucher.max_discount and discount > float(voucher.max_discount):
            discount = float(voucher.max_discount)
    else:
        discount = float(voucher.discount_value or 0)
    discount = max(0.0, min(discount, scope_subtotal))
    return {
        'ok': True,
        'voucher': voucher,
        'discount': discount,
        'scope_subtotal': scope_subtotal,
        'payload': {
            'voucher_id': voucher.id,
            'voucher_code': voucher.kode,
            'voucher_name': voucher.nama,
            'discount_type': voucher.discount_type,
            'discount_value': float(voucher.discount_value or 0),
            'max_discount': float(voucher.max_discount or 0),
            'scope_subtotal': scope_subtotal,
            'discount': discount,
            'scoped_category_ids': sorted(scoped_category_ids),
        },
    }


def promo_payload_json(payload):
    try:
        return json.dumps(payload, ensure_ascii=True)
    except Exception:
        return '{}'
