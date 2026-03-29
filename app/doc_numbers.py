"""Prefix nomor dokumen memakai tanggal kalender zona waktu tenant."""

from .models import PurchaseOrder, SalesReturn, Transaction
from .timezones import local_yyyymmdd_for_tenant_id


def generate_nomor_transaksi(tenant_id, branch_id):
    ymd = local_yyyymmdd_for_tenant_id(tenant_id)
    prefix = f'TRX-{ymd}-{branch_id:04d}'
    last = (
        Transaction.query.filter(Transaction.nomor.like(f'{prefix}%'))
        .order_by(Transaction.id.desc())
        .first()
    )
    if last:
        last_num = int(last.nomor.split('-')[-1]) + 1
    else:
        last_num = 1
    return f'{prefix}-{last_num:04d}'


def generate_nomor_retur(tenant_id, branch_id):
    ymd = local_yyyymmdd_for_tenant_id(tenant_id)
    prefix = f'RET-{ymd}-{branch_id:04d}'
    last = (
        SalesReturn.query.filter(
            SalesReturn.tenant_id == tenant_id,
            SalesReturn.nomor.like(f'{prefix}%'),
        )
        .order_by(SalesReturn.id.desc())
        .first()
    )
    if last:
        last_num = int(last.nomor.split('-')[-1]) + 1
    else:
        last_num = 1
    return f'{prefix}-{last_num:04d}'


def generate_po_number(tenant_id, branch_id):
    ymd = local_yyyymmdd_for_tenant_id(tenant_id)
    prefix = f'PO-{ymd}-{branch_id:04d}'
    last = (
        PurchaseOrder.query.filter(PurchaseOrder.nomor.like(f'{prefix}%'))
        .order_by(PurchaseOrder.id.desc())
        .first()
    )
    if last:
        last_num = int(last.nomor.split('-')[-1]) + 1
    else:
        last_num = 1
    return f'{prefix}-{last_num:04d}'
