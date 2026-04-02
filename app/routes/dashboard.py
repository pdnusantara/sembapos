from datetime import datetime, timedelta, timezone

from flask import Blueprint, redirect, render_template, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload, selectinload

from ..models import Branch, Product, Transaction, User, Supplier
from ..timezones import (
    local_today_date,
    resolve_effective_zoneinfo,
    timezone_display_label,
    utc_naive_bounds_for_local_date,
)

dashboard_bp = Blueprint('dashboard', __name__)


def get_tenant_id():
    return current_user.tenant_id if not current_user.is_superadmin else None


def _transaction_base_query():
    q = Transaction.query
    tenant_id = get_tenant_id()
    if tenant_id:
        q = q.filter_by(tenant_id=tenant_id)
    if current_user.role == 'kasir' and current_user.branch_id:
        q = q.filter_by(branch_id=current_user.branch_id)
    return q


def _product_base_query():
    pq = Product.query
    tenant_id = get_tenant_id()
    if tenant_id:
        pq = pq.filter_by(tenant_id=tenant_id, aktif=True)
    return pq


@dashboard_bp.route('/dashboard')
@login_required
def index():
    if current_user.is_superadmin:
        return redirect(url_for('superadmin.sa_dashboard'))
    if current_user.role == 'affiliate':
        return redirect(url_for('affiliate.dashboard'))

    app_tz, tz_id = resolve_effective_zoneinfo(current_user)
    tenant_id = get_tenant_id()
    today_local = local_today_date(tz_id)
    start_today, end_today = utc_naive_bounds_for_local_date(today_local, tz_id)
    yesterday_local = today_local - timedelta(days=1)
    start_yesterday, end_yesterday = utc_naive_bounds_for_local_date(yesterday_local, tz_id)

    q = _transaction_base_query()

    total_penjualan_hari_ini = (
        q.filter(
            Transaction.created_at.between(start_today, end_today),
            Transaction.status == 'selesai',
        ).with_entities(func.sum(Transaction.total)).scalar()
        or 0
    )

    jumlah_transaksi_hari_ini = q.filter(
        Transaction.created_at.between(start_today, end_today),
        Transaction.status == 'selesai',
    ).count()

    total_penjualan_kemarin = (
        q.filter(
            Transaction.created_at.between(start_yesterday, end_yesterday),
            Transaction.status == 'selesai',
        ).with_entities(func.sum(Transaction.total)).scalar()
        or 0
    )

    jumlah_transaksi_kemarin = q.filter(
        Transaction.created_at.between(start_yesterday, end_yesterday),
        Transaction.status == 'selesai',
    ).count()

    pq = _product_base_query()
    produk_menipis = pq.filter(Product.stok <= Product.stok_minimum).count()
    total_produk = pq.count()

    chart_start_local = today_local - timedelta(days=6)
    chart_start_utc, _ = utc_naive_bounds_for_local_date(chart_start_local, tz_id)
    _, chart_end_utc = utc_naive_bounds_for_local_date(today_local, tz_id)

    chart_labels = []
    day_keys = [chart_start_local + timedelta(days=i) for i in range(7)]
    sums_by_day = {d: 0.0 for d in day_keys}

    tq_chart = _transaction_base_query()
    rows = (
        tq_chart.filter(
            Transaction.created_at.between(chart_start_utc, chart_end_utc),
            Transaction.status == 'selesai',
        )
        .with_entities(Transaction.created_at, Transaction.total)
        .all()
    )
    for created_at, total in rows:
        if not created_at:
            continue
        aware = created_at.replace(tzinfo=timezone.utc)
        d = aware.astimezone(app_tz).date()
        if d in sums_by_day:
            sums_by_day[d] += float(total or 0)

    chart_data = []
    for d in day_keys:
        chart_labels.append(d.strftime('%d/%m'))
        chart_data.append(sums_by_day[d])

    tq = _transaction_base_query()
    transaksi_terbaru = (
        tq.options(
            joinedload(Transaction.user),
            joinedload(Transaction.branch),
            selectinload(Transaction.payments),
        )
        .order_by(Transaction.created_at.desc())
        .limit(10)
        .all()
    )

    total_cabang = 0
    if tenant_id:
        total_cabang = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).count()

    onboarding = None
    if tenant_id and current_user.role == 'admin':
        steps = []
        has_products = total_produk > 0
        has_multi_user = User.query.filter_by(tenant_id=tenant_id, aktif=True).count() > 1
        has_transaction = Transaction.query.filter_by(tenant_id=tenant_id, status='selesai').first() is not None
        has_supplier = Supplier.query.filter_by(tenant_id=tenant_id).first() is not None
        has_multi_branch = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).count() > 1

        steps.append({'label': 'Tambahkan produk pertama', 'done': has_products, 'url': '/products/add'})
        steps.append({'label': 'Tambahkan supplier', 'done': has_supplier, 'url': '/suppliers'})
        steps.append({'label': 'Lakukan transaksi pertama', 'done': has_transaction, 'url': '/pos'})
        steps.append({'label': 'Tambahkan user kasir', 'done': has_multi_user, 'url': '/admin/users'})
        steps.append({'label': 'Buat cabang tambahan', 'done': has_multi_branch, 'url': '/admin/branches'})

        done_count = sum(1 for s in steps if s['done'])
        if done_count < len(steps):
            onboarding = {
                'steps': steps,
                'done': done_count,
                'total': len(steps),
                'pct': int(100 * done_count / len(steps)),
            }

    return render_template(
        'dashboard.html',
        total_penjualan_hari_ini=total_penjualan_hari_ini,
        jumlah_transaksi_hari_ini=jumlah_transaksi_hari_ini,
        total_penjualan_kemarin=total_penjualan_kemarin,
        jumlah_transaksi_kemarin=jumlah_transaksi_kemarin,
        produk_menipis=produk_menipis,
        total_produk=total_produk,
        total_cabang=total_cabang,
        chart_labels=chart_labels,
        chart_data=chart_data,
        transaksi_terbaru=transaksi_terbaru,
        dashboard_timezone_label=timezone_display_label(tz_id),
        onboarding=onboarding,
    )
