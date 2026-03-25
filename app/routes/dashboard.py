from flask import Blueprint, render_template, jsonify
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from sqlalchemy import func
from .. import db
from ..models import Transaction, TransactionItem, Product, Branch

dashboard_bp = Blueprint('dashboard', __name__)


def get_tenant_id():
    return current_user.tenant_id if not current_user.is_superadmin else None


@dashboard_bp.route('/')
@login_required
def index():
    tenant_id = get_tenant_id()
    today = datetime.utcnow().date()
    start_today = datetime.combine(today, datetime.min.time())
    end_today = datetime.combine(today, datetime.max.time())

    # Query base
    q = Transaction.query
    if tenant_id:
        q = q.filter_by(tenant_id=tenant_id)
    if current_user.role == 'kasir' and current_user.branch_id:
        q = q.filter_by(branch_id=current_user.branch_id)

    # Stats hari ini
    total_penjualan_hari_ini = q.filter(
        Transaction.created_at.between(start_today, end_today),
        Transaction.status == 'selesai'
    ).with_entities(func.sum(Transaction.total)).scalar() or 0

    jumlah_transaksi_hari_ini = q.filter(
        Transaction.created_at.between(start_today, end_today),
        Transaction.status == 'selesai'
    ).count()

    # Produk menipis
    pq = Product.query
    if tenant_id:
        pq = pq.filter_by(tenant_id=tenant_id, aktif=True)
    produk_menipis = pq.filter(Product.stok <= Product.stok_minimum).count()

    # Transaksi 7 hari
    chart_labels = []
    chart_data = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        start = datetime.combine(day, datetime.min.time())
        end = datetime.combine(day, datetime.max.time())
        dq = Transaction.query
        if tenant_id:
            dq = dq.filter_by(tenant_id=tenant_id)
        if current_user.role == 'kasir' and current_user.branch_id:
            dq = dq.filter_by(branch_id=current_user.branch_id)
        val = dq.filter(
            Transaction.created_at.between(start, end),
            Transaction.status == 'selesai'
        ).with_entities(func.sum(Transaction.total)).scalar() or 0
        chart_labels.append(day.strftime('%d/%m'))
        chart_data.append(float(val))

    # Transaksi terbaru
    tq = Transaction.query
    if tenant_id:
        tq = tq.filter_by(tenant_id=tenant_id)
    if current_user.role == 'kasir' and current_user.branch_id:
        tq = tq.filter_by(branch_id=current_user.branch_id)
    transaksi_terbaru = tq.order_by(Transaction.created_at.desc()).limit(10).all()

    # Total produk
    total_produk = pq.count() if tenant_id else Product.query.count()

    # Cabang (untuk admin)
    total_cabang = 0
    if tenant_id:
        total_cabang = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).count()

    return render_template('dashboard.html',
        total_penjualan_hari_ini=total_penjualan_hari_ini,
        jumlah_transaksi_hari_ini=jumlah_transaksi_hari_ini,
        produk_menipis=produk_menipis,
        total_produk=total_produk,
        total_cabang=total_cabang,
        chart_labels=chart_labels,
        chart_data=chart_data,
        transaksi_terbaru=transaksi_terbaru,
    )
