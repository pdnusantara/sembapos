from datetime import datetime, timedelta
from sqlalchemy import func

from . import db
from .models import MemberTier, Transaction, TransactionItem, ProductCategory, Product


DEFAULT_ROLLING_DAYS = 365


def ensure_default_tiers(tenant_id):
    """Create default Silver/Gold/Platinum tiers once per tenant."""
    if not tenant_id:
        return
    exists = MemberTier.query.filter_by(tenant_id=tenant_id).count()
    if exists:
        return
    db.session.add_all(
        [
            MemberTier(
                tenant_id=tenant_id,
                kode='silver',
                nama='Silver',
                min_spend=0,
                benefit_discount_pct=0,
                sort_order=10,
                aktif=True,
            ),
            MemberTier(
                tenant_id=tenant_id,
                kode='gold',
                nama='Gold',
                min_spend=5_000_000,
                benefit_discount_pct=2,
                sort_order=20,
                aktif=True,
            ),
            MemberTier(
                tenant_id=tenant_id,
                kode='platinum',
                nama='Platinum',
                min_spend=15_000_000,
                benefit_discount_pct=5,
                sort_order=30,
                aktif=True,
            ),
        ]
    )
    db.session.flush()


def active_tiers(tenant_id):
    return (
        MemberTier.query.filter_by(tenant_id=tenant_id, aktif=True)
        .order_by(MemberTier.min_spend.asc(), MemberTier.sort_order.asc(), MemberTier.id.asc())
        .all()
    )


def evaluate_member_tier(member, rolling_days=DEFAULT_ROLLING_DAYS, commit=False):
    """Evaluate rolling spend and assign best tier for member."""
    if not member:
        return None
    tenant_id = member.tenant_id
    start = datetime.utcnow() - timedelta(days=max(1, int(rolling_days or DEFAULT_ROLLING_DAYS)))
    q = Transaction.query.filter(
        Transaction.tenant_id == tenant_id,
        Transaction.member_id == member.id,
        Transaction.status == 'selesai',
        Transaction.created_at >= start,
    )
    rolling_spend = float(q.with_entities(func.coalesce(func.sum(Transaction.total), 0)).scalar() or 0)
    rolling_tx_count = int(q.with_entities(func.count(Transaction.id)).scalar() or 0)
    last_tx = q.order_by(Transaction.created_at.desc()).first()
    tiers = active_tiers(tenant_id)
    picked = None
    for t in tiers:
        if rolling_spend >= float(t.min_spend or 0):
            picked = t
    if not picked and tiers:
        picked = tiers[0]

    member.rolling_spend = rolling_spend
    member.rolling_tx_count = rolling_tx_count
    member.rolling_last_days = int(rolling_days or DEFAULT_ROLLING_DAYS)
    member.last_transaction_at = last_tx.created_at if last_tx else member.last_transaction_at
    member.tier_id = picked.id if picked else None
    member.tier_evaluated_at = datetime.utcnow()
    if commit:
        db.session.commit()
    return picked


def member_tier_discount_pct(member):
    if not member or not member.tier or not member.tier.aktif:
        return 0.0
    return max(0.0, float(member.tier.benefit_discount_pct or 0))


def member_top_products(member_id, tenant_id, limit=5):
    rows = (
        db.session.query(
            TransactionItem.product_id,
            func.coalesce(func.sum(TransactionItem.qty), 0).label('qty_total'),
            func.coalesce(func.sum(TransactionItem.subtotal), 0).label('omzet_total'),
            func.max(TransactionItem.nama_produk).label('nama_produk'),
        )
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .filter(
            Transaction.tenant_id == tenant_id,
            Transaction.member_id == member_id,
            Transaction.status == 'selesai',
        )
        .group_by(TransactionItem.product_id)
        .order_by(func.sum(TransactionItem.subtotal).desc())
        .limit(limit)
        .all()
    )
    return rows


def member_top_categories(member_id, tenant_id, limit=5):
    rows = (
        db.session.query(
            ProductCategory.nama.label('nama_kategori'),
            func.coalesce(func.sum(TransactionItem.subtotal), 0).label('omzet_total'),
            func.coalesce(func.sum(TransactionItem.qty), 0).label('qty_total'),
        )
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .join(Product, Product.id == TransactionItem.product_id, isouter=True)
        .join(ProductCategory, ProductCategory.id == Product.category_id, isouter=True)
        .filter(
            Transaction.tenant_id == tenant_id,
            Transaction.member_id == member_id,
            Transaction.status == 'selesai',
        )
        .group_by(ProductCategory.nama)
        .order_by(func.sum(TransactionItem.subtotal).desc())
        .limit(limit)
        .all()
    )
    return rows
