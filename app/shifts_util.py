"""Helper shift kasir (cabang efektif & shift terbuka) — dipakai POS dan modul shifts."""
from .models import Branch, CashierShift


def effective_branch_id(tenant_id, user):
    if getattr(user, 'branch_id', None):
        return user.branch_id
    b = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).first()
    return b.id if b else None


def get_open_shift(tenant_id, branch_id, user_id):
    if not branch_id or not tenant_id or not user_id:
        return None
    return CashierShift.query.filter_by(
        tenant_id=tenant_id,
        branch_id=branch_id,
        user_id=user_id,
        status='open',
    ).first()
