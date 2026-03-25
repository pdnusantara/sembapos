import csv
from io import StringIO
from urllib.parse import urlencode

from flask import Blueprint, render_template, request, Response, url_for
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from sqlalchemy import func, desc, asc, or_
from sqlalchemy.orm import joinedload

from .. import db
from ..models import (
    Transaction,
    TransactionItem,
    Product,
    OperationalExpense,
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
    Branch,
    ProductCategory,
    User,
)

reports_bp = Blueprint('reports', __name__, url_prefix='/reports')


def _parse_date(s, end_of_day=False):
    if not s or not str(s).strip():
        return None
    try:
        d = datetime.strptime(str(s).strip()[:10], '%Y-%m-%d')
        if end_of_day:
            return d.replace(hour=23, minute=59, second=59, microsecond=999999)
        return d.replace(hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        return None


def _default_range_days(days=30):
    end = datetime.utcnow().replace(hour=23, minute=59, second=59, microsecond=999999)
    start = (end - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


def _po_filtered_query(tenant_id, date_from, date_to, tanggal_ref, status, supplier_id, branch_id_param):
    q = PurchaseOrder.query
    if tenant_id:
        q = q.filter(PurchaseOrder.tenant_id == tenant_id)
    if current_user.role == 'kasir' and current_user.branch_id:
        q = q.filter(PurchaseOrder.branch_id == current_user.branch_id)
    elif branch_id_param and current_user.role != 'kasir' and tenant_id:
        try:
            bid = int(branch_id_param)
            if Branch.query.filter_by(id=bid, tenant_id=tenant_id).first():
                q = q.filter(PurchaseOrder.branch_id == bid)
        except (ValueError, TypeError):
            pass

    if tanggal_ref == 'terima':
        q = q.filter(
            PurchaseOrder.tanggal_terima.isnot(None),
            PurchaseOrder.tanggal_terima.between(date_from, date_to),
        )
    else:
        q = q.filter(PurchaseOrder.tanggal_pesan.between(date_from, date_to))

    if status in ('draft', 'dipesan', 'diterima', 'batal'):
        q = q.filter(PurchaseOrder.status == status)

    if supplier_id and tenant_id:
        try:
            sid = int(supplier_id)
            if Supplier.query.filter_by(id=sid, tenant_id=tenant_id).first():
                q = q.filter(PurchaseOrder.supplier_id == sid)
        except (ValueError, TypeError):
            pass

    nomor_q = request.args.get('q', '').strip()
    if nomor_q:
        q = q.filter(PurchaseOrder.nomor.ilike(f'%{nomor_q}%'))
    return q


def _user_branch_maps_for_pos(pos_list):
    uids = {p.user_id for p in pos_list}
    bids = {p.branch_id for p in pos_list}
    users_map = {}
    if uids:
        for u in User.query.filter(User.id.in_(uids)).all():
            users_map[u.id] = u
    branches_map = {}
    if bids:
        for b in Branch.query.filter(Branch.id.in_(bids)).all():
            branches_map[b.id] = b
    return users_map, branches_map


def _sales_breakdown_hpp_unit_expr(hpp_mode):
    if hpp_mode == 'snapshot':
        for col_name in ('hpp_snapshot', 'harga_beli_snapshot', 'modal_snapshot'):
            col = getattr(TransactionItem, col_name, None)
            if col is not None:
                return func.coalesce(col, func.coalesce(Product.harga_beli, 0)), 'snapshot', None
        return (
            func.coalesce(Product.harga_beli, 0),
            'master',
            'Mode HPP snapshot belum tersedia karena kolom snapshot belum ada. Sistem pakai harga beli master.',
        )
    return func.coalesce(Product.harga_beli, 0), 'master', None


def _sales_breakdown_sort_expr(sort_by, sort_dir, name_expr, qty_expr, omzet_expr, hpp_total_expr):
    margin_expr = omzet_expr - hpp_total_expr
    if sort_by == 'qty':
        base = qty_expr
    elif sort_by == 'hpp':
        base = hpp_total_expr
    elif sort_by == 'margin':
        base = margin_expr
    elif sort_by == 'nama':
        base = name_expr
    else:
        base = omzet_expr
    return asc(base) if sort_dir == 'asc' else desc(base)


def _sales_breakdown_rows(query_rows, name_key, key_key):
    rows = []
    for i, r in enumerate(query_rows, start=1):
        qty = float(r.qty_total or 0)
        omzet = float(r.omzet_total or 0)
        hpp = float(r.hpp_total or 0)
        margin = omzet - hpp
        margin_pct = ((margin / omzet) * 100.0) if omzet > 0 else 0.0
        rows.append({
            'rank': i,
            name_key: r.nama_key or '-',
            key_key: r.entity_key,
            'qty': qty,
            'omzet': omzet,
            'hpp': hpp,
            'margin': margin,
            'margin_pct': margin_pct,
        })
    return rows


def _sales_breakdown_insights(rows, name_key):
    if not rows:
        return {
            'top': None,
            'bottom': None,
            'best_margin': None,
            'worst_margin': None,
        }
    by_omzet = sorted(rows, key=lambda x: x['omzet'], reverse=True)
    by_margin = sorted(rows, key=lambda x: x['margin'], reverse=True)

    top = by_omzet[0]
    bottom = by_omzet[-1] if len(by_omzet) > 1 and by_omzet[-1]['omzet'] < top['omzet'] else None
    best_margin = by_margin[0]
    worst_margin = by_margin[-1] if len(by_margin) > 1 and by_margin[-1]['margin'] < best_margin['margin'] else None

    return {
        'top': {'label': top.get(name_key, '-'), **top} if top else None,
        'bottom': {'label': bottom.get(name_key, '-'), **bottom} if bottom else None,
        'best_margin': {'label': best_margin.get(name_key, '-'), **best_margin} if best_margin else None,
        'worst_margin': {'label': worst_margin.get(name_key, '-'), **worst_margin} if worst_margin else None,
    }


def _sales_breakdown_summary_alerts(produk_rows, kategori_rows, kategori_prev_map):
    negative_margin_products = [r for r in produk_rows if r['margin'] < 0]
    sharp_drop_rows = []
    for row in kategori_rows:
        key = row.get('kategori_key')
        prev_omzet = float(kategori_prev_map.get(key, 0) or 0)
        if prev_omzet <= 0:
            continue
        change_pct = ((row['omzet'] - prev_omzet) / prev_omzet) * 100.0
        if change_pct <= -30:
            sharp_drop_rows.append({
                'nama_kategori': row.get('nama_kategori', '-'),
                'change_pct': change_pct,
                'current_omzet': row['omzet'],
                'prev_omzet': prev_omzet,
            })
    sharp_drop_rows.sort(key=lambda x: x['change_pct'])
    return {
        'negative_margin_count': len(negative_margin_products),
        'negative_margin_top': negative_margin_products[:3],
        'sharp_drop_count': len(sharp_drop_rows),
        'sharp_drop_top': sharp_drop_rows[:3],
    }


@reports_bp.route('/')
@login_required
def index():
    tenant_id = current_user.tenant_id
    mode = request.args.get('mode', 'harian')
    tanggal = request.args.get('tanggal', datetime.utcnow().strftime('%Y-%m-%d'))
    bulan = request.args.get('bulan', datetime.utcnow().strftime('%Y-%m'))

    try:
        if mode == 'harian':
            tgl = datetime.strptime(tanggal, '%Y-%m-%d')
            start = datetime.combine(tgl, datetime.min.time())
            end = datetime.combine(tgl, datetime.max.time())
        else:
            tgl = datetime.strptime(bulan, '%Y-%m')
            start = tgl.replace(day=1)
            if tgl.month == 12:
                end = tgl.replace(year=tgl.year + 1, month=1, day=1) - timedelta(seconds=1)
            else:
                end = tgl.replace(month=tgl.month + 1, day=1) - timedelta(seconds=1)
    except ValueError:
        tgl = datetime.utcnow()
        start = datetime.combine(tgl.date(), datetime.min.time())
        end = datetime.combine(tgl.date(), datetime.max.time())

    q = Transaction.query.filter(
        Transaction.created_at.between(start, end),
        Transaction.status == 'selesai'
    )
    if tenant_id:
        q = q.filter_by(tenant_id=tenant_id)
    if current_user.role == 'kasir' and current_user.branch_id:
        q = q.filter_by(branch_id=current_user.branch_id)

    transactions = q.order_by(Transaction.created_at.desc()).all()
    total_penjualan = sum(float(t.total or 0) for t in transactions)
    total_transaksi = len(transactions)
    total_omset_kotor = sum(float(t.subtotal or 0) for t in transactions)
    total_diskon_nota = sum(float(t.diskon or 0) for t in transactions)
    payment_summary = {}
    for t in transactions:
        metode = (t.metode_bayar or 'lainnya').strip().lower() or 'lainnya'
        row = payment_summary.setdefault(metode, {'metode': metode, 'count': 0, 'total': 0.0})
        row['count'] += 1
        row['total'] += float(t.total or 0)
    payment_summary_rows = sorted(payment_summary.values(), key=lambda x: x['total'], reverse=True)

    hpp_q = db.session.query(
        func.coalesce(
            func.sum(TransactionItem.qty * func.coalesce(Product.harga_beli, 0)),
            0,
        )
    ).join(Transaction, TransactionItem.transaction_id == Transaction.id).join(
        Product, TransactionItem.product_id == Product.id
    ).filter(
        Transaction.created_at.between(start, end),
        Transaction.status == 'selesai',
    )
    if tenant_id:
        hpp_q = hpp_q.filter(Transaction.tenant_id == tenant_id)
    if current_user.role == 'kasir' and current_user.branch_id:
        hpp_q = hpp_q.filter(Transaction.branch_id == current_user.branch_id)
    total_hpp = float(hpp_q.scalar() or 0)
    total_laba_kotor = total_penjualan - total_hpp

    op_q = OperationalExpense.query.filter(
        OperationalExpense.tanggal.between(start, end)
    )
    if tenant_id:
        op_q = op_q.filter(OperationalExpense.tenant_id == tenant_id)
    if current_user.role == 'kasir' and current_user.branch_id:
        op_q = op_q.filter(OperationalExpense.branch_id == current_user.branch_id)
    total_biaya_operasional = float(
        op_q.with_entities(func.coalesce(func.sum(OperationalExpense.jumlah), 0)).scalar() or 0
    )
    laba_setelah_operasional = total_laba_kotor - total_biaya_operasional

    return render_template(
        'reports/index.html',
        transactions=transactions,
        total_penjualan=total_penjualan,
        total_transaksi=total_transaksi,
        total_omset_kotor=total_omset_kotor,
        total_diskon_nota=total_diskon_nota,
        total_hpp=total_hpp,
        total_laba_kotor=total_laba_kotor,
        total_biaya_operasional=total_biaya_operasional,
        laba_setelah_operasional=laba_setelah_operasional,
        payment_summary_rows=payment_summary_rows,
        mode=mode,
        tanggal=tanggal,
        bulan=bulan,
    )


@reports_bp.route('/penjualan-produk-kategori')
@login_required
def sales_breakdown():
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect, url_for
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id

    date_from = _parse_date(request.args.get('date_from'), end_of_day=False)
    date_to = _parse_date(request.args.get('date_to'), end_of_day=True)
    if not date_from or not date_to:
        date_from, date_to = _default_range_days(30)
    if date_from > date_to:
        date_from, date_to = date_to.replace(hour=0, minute=0, second=0, microsecond=0), date_from.replace(
            hour=23, minute=59, second=59, microsecond=999999
        )

    tab = (request.args.get('tab') or 'produk').strip().lower()
    if tab not in ('produk', 'kategori'):
        tab = 'produk'

    hpp_mode = (request.args.get('hpp_mode') or 'master').strip().lower()
    if hpp_mode not in ('master', 'snapshot'):
        hpp_mode = 'master'
    hpp_unit_expr, hpp_mode_used, hpp_mode_note = _sales_breakdown_hpp_unit_expr(hpp_mode)
    hpp_total_expr = func.sum(TransactionItem.qty * hpp_unit_expr)
    omzet_expr = func.sum(TransactionItem.subtotal)
    qty_expr = func.sum(TransactionItem.qty)

    row_limit_raw = request.args.get('limit', '100').strip()
    try:
        row_limit = int(row_limit_raw)
    except ValueError:
        row_limit = 100
    row_limit = max(20, min(row_limit, 500))

    q_produk = request.args.get('q_produk', '').strip()
    q_kategori = request.args.get('q_kategori', '').strip()
    sort_produk = (request.args.get('sort_produk') or 'omzet').strip().lower()
    sort_kategori = (request.args.get('sort_kategori') or 'omzet').strip().lower()
    dir_produk = (request.args.get('dir_produk') or 'desc').strip().lower()
    dir_kategori = (request.args.get('dir_kategori') or 'desc').strip().lower()
    if sort_produk not in ('nama', 'qty', 'omzet', 'hpp', 'margin'):
        sort_produk = 'omzet'
    if sort_kategori not in ('nama', 'qty', 'omzet', 'hpp', 'margin'):
        sort_kategori = 'omzet'
    if dir_produk not in ('asc', 'desc'):
        dir_produk = 'desc'
    if dir_kategori not in ('asc', 'desc'):
        dir_kategori = 'desc'

    branch_id_param = request.args.get('branch_id', '').strip()
    selected_branch_id = ''

    trx_q = Transaction.query.filter(
        Transaction.tenant_id == tenant_id,
        Transaction.status == 'selesai',
        Transaction.created_at.between(date_from, date_to),
    )
    if current_user.role == 'kasir' and current_user.branch_id:
        trx_q = trx_q.filter(Transaction.branch_id == current_user.branch_id)
        selected_branch_id = str(current_user.branch_id)
    elif branch_id_param:
        try:
            bid = int(branch_id_param)
            if Branch.query.filter_by(id=bid, tenant_id=tenant_id).first():
                trx_q = trx_q.filter(Transaction.branch_id == bid)
                selected_branch_id = str(bid)
        except (TypeError, ValueError):
            selected_branch_id = ''

    trx_ids_q = trx_q.with_entities(Transaction.id)

    produk_name_expr = func.coalesce(TransactionItem.nama_produk, Product.nama, '-')
    kategori_name_expr = func.coalesce(ProductCategory.nama, 'Tanpa Kategori')
    kategori_key_expr = func.coalesce(ProductCategory.id, 0)

    produk_q = (
        db.session.query(
            produk_name_expr.label('nama_key'),
            TransactionItem.product_id.label('entity_key'),
            func.coalesce(qty_expr, 0).label('qty_total'),
            func.coalesce(omzet_expr, 0).label('omzet_total'),
            func.coalesce(hpp_total_expr, 0).label('hpp_total'),
        )
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .outerjoin(Product, TransactionItem.product_id == Product.id)
        .filter(Transaction.id.in_(trx_ids_q))
        .group_by(TransactionItem.product_id, TransactionItem.nama_produk, Product.nama)
    )
    if q_produk:
        produk_q = produk_q.filter(
            or_(
                TransactionItem.nama_produk.ilike(f'%{q_produk}%'),
                Product.nama.ilike(f'%{q_produk}%'),
            )
        )
    produk_q = produk_q.order_by(
        _sales_breakdown_sort_expr(
            sort_produk,
            dir_produk,
            produk_name_expr,
            qty_expr,
            omzet_expr,
            hpp_total_expr,
        )
    )
    produk_q = produk_q.limit(row_limit)

    kategori_q = (
        db.session.query(
            kategori_name_expr.label('nama_key'),
            kategori_key_expr.label('entity_key'),
            func.coalesce(qty_expr, 0).label('qty_total'),
            func.coalesce(omzet_expr, 0).label('omzet_total'),
            func.coalesce(hpp_total_expr, 0).label('hpp_total'),
        )
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .outerjoin(Product, TransactionItem.product_id == Product.id)
        .outerjoin(ProductCategory, Product.category_id == ProductCategory.id)
        .filter(Transaction.id.in_(trx_ids_q))
        .group_by(kategori_key_expr, kategori_name_expr)
    )
    if q_kategori:
        if 'tanpa' in q_kategori.lower():
            kategori_q = kategori_q.filter(
                or_(
                    ProductCategory.nama.ilike(f'%{q_kategori}%'),
                    ProductCategory.id.is_(None),
                )
            )
        else:
            kategori_q = kategori_q.filter(ProductCategory.nama.ilike(f'%{q_kategori}%'))
    kategori_q = kategori_q.order_by(
        _sales_breakdown_sort_expr(
            sort_kategori,
            dir_kategori,
            kategori_name_expr,
            qty_expr,
            omzet_expr,
            hpp_total_expr,
        )
    )
    kategori_q = kategori_q.limit(row_limit)

    produk_rows = _sales_breakdown_rows(produk_q.all(), 'nama_produk', 'produk_key')
    kategori_rows = _sales_breakdown_rows(kategori_q.all(), 'nama_kategori', 'kategori_key')

    period_delta = date_to - date_from
    prev_end = date_from - timedelta(microseconds=1)
    prev_start = prev_end - period_delta
    prev_kategori_rows = (
        db.session.query(
            kategori_key_expr.label('entity_key'),
            func.coalesce(omzet_expr, 0).label('omzet_total'),
        )
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .outerjoin(Product, TransactionItem.product_id == Product.id)
        .outerjoin(ProductCategory, Product.category_id == ProductCategory.id)
        .filter(
            Transaction.tenant_id == tenant_id,
            Transaction.status == 'selesai',
            Transaction.created_at.between(prev_start, prev_end),
        )
        .group_by(kategori_key_expr)
    )
    if selected_branch_id:
        prev_kategori_rows = prev_kategori_rows.filter(Transaction.branch_id == int(selected_branch_id))
    prev_kategori_rows = prev_kategori_rows.all()
    kategori_prev_map = {int(r.entity_key or 0): float(r.omzet_total or 0) for r in prev_kategori_rows}
    alerts = _sales_breakdown_summary_alerts(produk_rows, kategori_rows, kategori_prev_map)

    produk_totals = {
        'qty': sum(r['qty'] for r in produk_rows),
        'omzet': sum(r['omzet'] for r in produk_rows),
        'hpp': sum(r['hpp'] for r in produk_rows),
    }
    produk_totals['margin'] = produk_totals['omzet'] - produk_totals['hpp']
    produk_totals['margin_pct'] = ((produk_totals['margin'] / produk_totals['omzet']) * 100.0) if produk_totals['omzet'] > 0 else 0.0

    kategori_totals = {
        'qty': sum(r['qty'] for r in kategori_rows),
        'omzet': sum(r['omzet'] for r in kategori_rows),
        'hpp': sum(r['hpp'] for r in kategori_rows),
    }
    kategori_totals['margin'] = kategori_totals['omzet'] - kategori_totals['hpp']
    kategori_totals['margin_pct'] = ((kategori_totals['margin'] / kategori_totals['omzet']) * 100.0) if kategori_totals['omzet'] > 0 else 0.0

    produk_insights = _sales_breakdown_insights(produk_rows, 'nama_produk')
    kategori_insights = _sales_breakdown_insights(kategori_rows, 'nama_kategori')
    chart_rows = produk_rows if tab == 'produk' else kategori_rows
    chart_name_key = 'nama_produk' if tab == 'produk' else 'nama_kategori'
    chart_labels = [r.get(chart_name_key, '-') for r in chart_rows[:10]]
    chart_omzet = [float(r.get('omzet') or 0) for r in chart_rows[:10]]
    chart_margin = [float(r.get('margin') or 0) for r in chart_rows[:10]]

    detail_type = (request.args.get('detail_type') or '').strip().lower()
    detail_key_raw = (request.args.get('detail_key') or '').strip()
    detail_rows = []
    detail_title = ''
    if detail_type in ('produk', 'kategori') and detail_key_raw:
        detail_q = (
            db.session.query(
                Transaction.id.label('transaction_id'),
                Transaction.nomor.label('nomor'),
                Transaction.created_at.label('created_at'),
                User.nama.label('kasir_nama'),
                TransactionItem.nama_produk.label('nama_produk'),
                ProductCategory.nama.label('nama_kategori'),
                func.coalesce(func.sum(TransactionItem.qty), 0).label('qty_total'),
                func.coalesce(func.sum(TransactionItem.subtotal), 0).label('omzet_total'),
                func.coalesce(func.sum(TransactionItem.qty * hpp_unit_expr), 0).label('hpp_total'),
            )
            .join(Transaction, TransactionItem.transaction_id == Transaction.id)
            .join(User, Transaction.user_id == User.id)
            .outerjoin(Product, TransactionItem.product_id == Product.id)
            .outerjoin(ProductCategory, Product.category_id == ProductCategory.id)
            .filter(
                Transaction.id.in_(trx_ids_q),
            )
        )
        if detail_type == 'produk':
            try:
                produk_key = int(detail_key_raw)
                detail_q = detail_q.filter(TransactionItem.product_id == produk_key)
                selected_row = next((r for r in produk_rows if int(r.get('produk_key') or 0) == produk_key), None)
                detail_title = f"Detail transaksi produk: {selected_row['nama_produk']}" if selected_row else 'Detail transaksi produk'
            except ValueError:
                detail_q = None
        else:
            try:
                kategori_key = int(detail_key_raw)
                if kategori_key == 0:
                    detail_q = detail_q.filter(Product.category_id.is_(None))
                else:
                    detail_q = detail_q.filter(Product.category_id == kategori_key)
                selected_row = next((r for r in kategori_rows if int(r.get('kategori_key') or 0) == kategori_key), None)
                detail_title = f"Detail transaksi kategori: {selected_row['nama_kategori']}" if selected_row else 'Detail transaksi kategori'
            except ValueError:
                detail_q = None
        if detail_q is not None:
            for r in (
                detail_q.group_by(
                    Transaction.id,
                    Transaction.nomor,
                    Transaction.created_at,
                    User.nama,
                    TransactionItem.nama_produk,
                    ProductCategory.nama,
                )
                .order_by(Transaction.created_at.desc())
                .limit(200)
                .all()
            ):
                omzet_row = float(r.omzet_total or 0)
                hpp_row = float(r.hpp_total or 0)
                detail_rows.append({
                    'transaction_id': r.transaction_id,
                    'nomor': r.nomor,
                    'created_at': r.created_at,
                    'kasir_nama': r.kasir_nama,
                    'nama_produk': r.nama_produk,
                    'nama_kategori': r.nama_kategori or 'Tanpa Kategori',
                    'qty': float(r.qty_total or 0),
                    'omzet': omzet_row,
                    'hpp': hpp_row,
                    'margin': omzet_row - hpp_row,
                })

    branches = []
    if current_user.role != 'kasir':
        branches = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Branch.nama).all()

    date_from_str = date_from.strftime('%Y-%m-%d')
    date_to_str = date_to.strftime('%Y-%m-%d')
    qs_pairs = [
        ('date_from', date_from_str),
        ('date_to', date_to_str),
        ('limit', str(row_limit)),
        ('hpp_mode', hpp_mode),
        ('q_produk', q_produk),
        ('q_kategori', q_kategori),
        ('sort_produk', sort_produk),
        ('sort_kategori', sort_kategori),
        ('dir_produk', dir_produk),
        ('dir_kategori', dir_kategori),
    ]
    if selected_branch_id:
        qs_pairs.append(('branch_id', selected_branch_id))

    tab_produk_qs = urlencode(qs_pairs + [('tab', 'produk')])
    tab_kategori_qs = urlencode(qs_pairs + [('tab', 'kategori')])

    return render_template(
        'reports/sales_breakdown.html',
        tab=tab,
        date_from=date_from_str,
        date_to=date_to_str,
        row_limit=row_limit,
        selected_branch_id=selected_branch_id,
        hpp_mode=hpp_mode,
        hpp_mode_used=hpp_mode_used,
        hpp_mode_note=hpp_mode_note,
        q_produk=q_produk,
        q_kategori=q_kategori,
        sort_produk=sort_produk,
        sort_kategori=sort_kategori,
        dir_produk=dir_produk,
        dir_kategori=dir_kategori,
        branches=branches,
        produk_rows=produk_rows,
        kategori_rows=kategori_rows,
        produk_totals=produk_totals,
        kategori_totals=kategori_totals,
        produk_insights=produk_insights,
        kategori_insights=kategori_insights,
        alerts=alerts,
        detail_type=detail_type,
        detail_key=detail_key_raw,
        detail_rows=detail_rows,
        detail_title=detail_title,
        chart_labels=chart_labels,
        chart_omzet=chart_omzet,
        chart_margin=chart_margin,
        tab_produk_qs=tab_produk_qs,
        tab_kategori_qs=tab_kategori_qs,
    )


@reports_bp.route('/pembelian')
@login_required
def pembelian():
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect, url_for
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id
    date_from = _parse_date(request.args.get('date_from'), end_of_day=False)
    date_to = _parse_date(request.args.get('date_to'), end_of_day=True)
    if not date_from or not date_to:
        date_from, date_to = _default_range_days(30)

    tanggal_ref = request.args.get('tanggal_ref', 'pesan')
    if tanggal_ref not in ('pesan', 'terima'):
        tanggal_ref = 'pesan'
    status = request.args.get('status', '').strip()
    supplier_id = request.args.get('supplier_id', '').strip()
    branch_id_param = request.args.get('branch_id', '').strip()

    base_q = _po_filtered_query(
        tenant_id, date_from, date_to, tanggal_ref,
        status if status in ('draft', 'dipesan', 'diterima', 'batal') else '',
        supplier_id,
        branch_id_param,
    )

    pos = (
        base_q.options(joinedload(PurchaseOrder.supplier))
        .order_by(PurchaseOrder.tanggal_pesan.desc())
        .all()
    )

    users_map, branches_map = _user_branch_maps_for_pos(pos)

    total_nilai = sum(p.total or 0 for p in pos)
    jumlah_po = len(pos)
    rata_po = (total_nilai / jumlah_po) if jumlah_po else 0
    supplier_unik = len({p.supplier_id for p in pos})

    if not pos:
        supplier_rows = []
        po_items_map = {}
        line_items_limit = 1000
        line_items_limited = False
    else:
        id_query = base_q.with_entities(PurchaseOrder.id)
        supplier_rows = (
            db.session.query(
                Supplier.id,
                Supplier.nama,
                func.count(PurchaseOrder.id).label('jumlah_po'),
                func.sum(PurchaseOrder.total).label('total_nilai'),
            )
            .join(PurchaseOrder, Supplier.id == PurchaseOrder.supplier_id)
            .filter(PurchaseOrder.id.in_(id_query))
            .group_by(Supplier.id, Supplier.nama)
            .order_by(func.sum(PurchaseOrder.total).desc())
            .all()
        )

        line_items_limit = 1000
        line_items_total = (
            db.session.query(func.count(PurchaseOrderItem.id))
            .join(PurchaseOrder, PurchaseOrderItem.po_id == PurchaseOrder.id)
            .filter(PurchaseOrder.id.in_(id_query))
            .scalar()
        ) or 0
        line_items_limited = line_items_total > line_items_limit

        line_items = (
            db.session.query(PurchaseOrderItem)
            .join(PurchaseOrder, PurchaseOrderItem.po_id == PurchaseOrder.id)
            .filter(PurchaseOrder.id.in_(id_query))
            .options(
                joinedload(PurchaseOrderItem.purchase_order).joinedload(PurchaseOrder.supplier),
            )
            .order_by(PurchaseOrder.tanggal_pesan.desc(), PurchaseOrder.id.desc())
            .limit(line_items_limit)
            .all()
        )

        po_items_map = {}
        for it in line_items:
            row = {
                'nama_produk': it.nama_produk or '',
                'harga_beli': float(it.harga_beli or 0),
                'qty_pesan': float(it.qty_pesan or 0),
                'qty_terima': float(it.qty_terima or 0),
                'subtotal': float(it.subtotal or 0),
            }
            po_items_map.setdefault(it.po_id, []).append(row)

    suppliers = Supplier.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Supplier.nama).all()
    branches = []
    if current_user.role != 'kasir':
        branches = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Branch.nama).all()

    qs_base = urlencode([(k, v) for k, v in request.args.items(multi=True)])

    return render_template(
        'reports/pembelian.html',
        pos=pos,
        users_map=users_map,
        branches_map=branches_map,
        total_nilai=total_nilai,
        jumlah_po=jumlah_po,
        rata_po=rata_po,
        supplier_unik=supplier_unik,
        supplier_rows=supplier_rows,
        po_items_map=po_items_map,
        line_items_limit=line_items_limit,
        line_items_limited=line_items_limited,
        suppliers=suppliers,
        branches=branches,
        date_from=date_from.strftime('%Y-%m-%d'),
        date_to=date_to.strftime('%Y-%m-%d'),
        tanggal_ref=tanggal_ref,
        status=status,
        supplier_id=supplier_id,
        branch_id_param=branch_id_param,
        q_nomor=request.args.get('q', '').strip(),
        qs_base=qs_base,
    )


@reports_bp.route('/pembelian/export.csv')
@login_required
def pembelian_export_csv():
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect, url_for
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id
    date_from = _parse_date(request.args.get('date_from'), end_of_day=False)
    date_to = _parse_date(request.args.get('date_to'), end_of_day=True)
    if not date_from or not date_to:
        date_from, date_to = _default_range_days(30)

    tanggal_ref = request.args.get('tanggal_ref', 'pesan')
    if tanggal_ref not in ('pesan', 'terima'):
        tanggal_ref = 'pesan'
    status = request.args.get('status', '').strip()
    supplier_id = request.args.get('supplier_id', '').strip()
    branch_id_param = request.args.get('branch_id', '').strip()

    base_q = _po_filtered_query(
        tenant_id, date_from, date_to, tanggal_ref,
        status if status in ('draft', 'dipesan', 'diterima', 'batal') else '',
        supplier_id,
        branch_id_param,
    )
    pos = base_q.order_by(PurchaseOrder.tanggal_pesan.desc()).all()
    users_map, branches_map = _user_branch_maps_for_pos(pos)
    po_by_id = {p.id: p for p in pos}

    id_query = base_q.with_entities(PurchaseOrder.id)
    items = (
        db.session.query(PurchaseOrderItem)
        .join(PurchaseOrder, PurchaseOrderItem.po_id == PurchaseOrder.id)
        .filter(PurchaseOrder.id.in_(id_query))
        .order_by(PurchaseOrder.tanggal_pesan.desc(), PurchaseOrder.id, PurchaseOrderItem.id)
        .all()
    )

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow([
        'nomor_po', 'tanggal_pesan', 'tanggal_terima', 'status', 'supplier', 'cabang', 'user',
        'produk', 'harga_beli', 'qty_pesan', 'qty_terima', 'subtotal_barang', 'total_po', 'catatan_po',
    ])
    for it in items:
        po = po_by_id.get(it.po_id)
        if not po:
            continue
        sup = po.supplier.nama if po.supplier else ''
        br = branches_map.get(po.branch_id)
        cab = br.nama if br else ''
        usr = users_map.get(po.user_id)
        uname = usr.nama if usr else ''
        w.writerow([
            po.nomor,
            po.tanggal_pesan.strftime('%Y-%m-%d %H:%M:%S') if po.tanggal_pesan else '',
            po.tanggal_terima.strftime('%Y-%m-%d %H:%M:%S') if po.tanggal_terima else '',
            po.status,
            sup,
            cab,
            uname,
            it.nama_produk,
            it.harga_beli,
            it.qty_pesan,
            it.qty_terima,
            it.subtotal,
            po.total,
            (po.catatan or '').replace('\n', ' ')[:500],
        ])

    fn = f'laporan-pembelian-{datetime.utcnow().strftime("%Y%m%d-%H%M")}.csv'
    data = '\ufeff' + buf.getvalue()
    return Response(
        data,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={fn}'},
    )


def _stok_product_base(tenant_id):
    return Product.query.filter_by(tenant_id=tenant_id, aktif=True)


def _stok_apply_filters(pq, tenant_id, category_id, stok_q, stok_status):
    if category_id:
        try:
            cid = int(category_id)
            if ProductCategory.query.filter_by(id=cid, tenant_id=tenant_id).first():
                pq = pq.filter(Product.category_id == cid)
        except (ValueError, TypeError):
            pass
    if stok_q:
        term = f'%{stok_q}%'
        pq = pq.filter(or_(Product.nama.ilike(term), Product.barcode.ilike(term)))
    if stok_status == 'habis':
        pq = pq.filter(Product.stok <= 0)
    elif stok_status == 'menipis':
        pq = pq.filter(Product.stok > 0, Product.stok <= Product.stok_minimum)
    elif stok_status == 'aman':
        pq = pq.filter(Product.stok > Product.stok_minimum)
    return pq


def _stok_apply_sort(pq, sort):
    sort = (sort or '').strip().lower()
    if sort == 'stok_asc':
        return pq.order_by(asc(Product.stok), Product.nama)
    if sort == 'stok_desc':
        return pq.order_by(desc(Product.stok), Product.nama)
    if sort == 'nilai_beli_desc':
        return pq.order_by(desc(Product.stok * Product.harga_beli), Product.nama)
    if sort == 'nilai_jual_desc':
        return pq.order_by(desc(Product.stok * Product.harga_jual), Product.nama)
    if sort == 'kategori':
        return (
            pq.outerjoin(ProductCategory, Product.category_id == ProductCategory.id)
            .order_by(func.coalesce(ProductCategory.nama, ''), Product.nama)
        )
    return pq.order_by(Product.nama)


def _tenant_stok_overview(tenant_id):
    b = _stok_product_base(tenant_id)
    total_sku = b.count()
    nilai_beli = (
        db.session.query(func.coalesce(func.sum(Product.stok * Product.harga_beli), 0.0))
        .filter(Product.tenant_id == tenant_id, Product.aktif.is_(True))
        .scalar()
    )
    nilai_jual = (
        db.session.query(func.coalesce(func.sum(Product.stok * Product.harga_jual), 0.0))
        .filter(Product.tenant_id == tenant_id, Product.aktif.is_(True))
        .scalar()
    )
    n_habis = b.filter(Product.stok <= 0).count()
    n_menipis = b.filter(Product.stok > 0, Product.stok <= Product.stok_minimum).count()
    n_aman = b.filter(Product.stok > Product.stok_minimum).count()
    return {
        'total_sku': total_sku,
        'nilai_beli': float(nilai_beli or 0),
        'nilai_jual': float(nilai_jual or 0),
        'n_habis': n_habis,
        'n_menipis': n_menipis,
        'n_aman': n_aman,
    }


@reports_bp.route('/stok')
@login_required
def stok():
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id

    category_id = request.args.get('category_id', '').strip()
    stok_q = request.args.get('q', '').strip()
    stok_status = request.args.get('stok_status', '').strip().lower()
    if request.args.get('menipis') == '1' and stok_status not in ('habis', 'menipis', 'aman'):
        stok_status = 'menipis'
    if stok_status not in ('', 'habis', 'menipis', 'aman'):
        stok_status = ''
    stok_sort = request.args.get('sort', '').strip().lower()
    allowed_sort = ('', 'nama', 'stok_asc', 'stok_desc', 'nilai_beli_desc', 'nilai_jual_desc', 'kategori')
    if stok_sort not in allowed_sort:
        stok_sort = 'nama'
    if stok_sort == '':
        stok_sort = 'nama'

    pq = _stok_apply_filters(_stok_product_base(tenant_id), tenant_id, category_id, stok_q, stok_status)
    pq = _stok_apply_sort(pq, stok_sort)

    products = (
        pq.options(
            joinedload(Product.category),
            joinedload(Product.supplier),
        )
        .all()
    )

    total_nilai_persediaan = sum((p.stok or 0) * (p.harga_beli or 0) for p in products)
    total_potensi_jual = sum((p.stok or 0) * (p.harga_jual or 0) for p in products)
    jumlah_habis = sum(1 for p in products if (p.stok or 0) <= 0)
    jumlah_menipis = sum(1 for p in products if (p.stok or 0) > 0 and p.stok_menipis)
    jumlah_aman = sum(1 for p in products if (p.stok or 0) > p.stok_minimum)

    overview = _tenant_stok_overview(tenant_id)

    by_category = {}
    for p in products:
        key = p.category_id or 0
        label = p.category.nama if p.category else 'Tanpa kategori'
        row = by_category.setdefault(key, {'label': label, 'sku': 0, 'qty': 0.0, 'nilai_beli': 0.0})
        row['sku'] += 1
        st = float(p.stok or 0)
        row['qty'] += st
        row['nilai_beli'] += st * float(p.harga_beli or 0)
    category_rows = sorted(by_category.values(), key=lambda r: (-r['nilai_beli'], r['label']))

    categories = ProductCategory.query.filter_by(tenant_id=tenant_id).order_by(ProductCategory.nama).all()
    qs_base = urlencode([(k, v) for k, v in request.args.items(multi=True)])

    chip_kw = {}
    if category_id:
        try:
            chip_kw['category_id'] = int(category_id)
        except (ValueError, TypeError):
            pass
    if stok_q:
        chip_kw['q'] = stok_q
    if stok_sort and stok_sort != 'nama':
        chip_kw['sort'] = stok_sort

    chip_semua = url_for('reports.stok', **chip_kw)
    chip_habis = url_for('reports.stok', **chip_kw, stok_status='habis')
    chip_menipis = url_for('reports.stok', **chip_kw, stok_status='menipis')
    chip_aman = url_for('reports.stok', **chip_kw, stok_status='aman')

    return render_template(
        'reports/stok.html',
        products=products,
        total_nilai_persediaan=total_nilai_persediaan,
        total_potensi_jual=total_potensi_jual,
        jumlah_habis=jumlah_habis,
        jumlah_menipis=jumlah_menipis,
        jumlah_aman=jumlah_aman,
        overview=overview,
        category_rows=category_rows,
        categories=categories,
        category_id=category_id,
        stok_q=stok_q,
        stok_status=stok_status,
        stok_sort=stok_sort,
        qs_base=qs_base,
        chip_semua=chip_semua,
        chip_habis=chip_habis,
        chip_menipis=chip_menipis,
        chip_aman=chip_aman,
    )


@reports_bp.route('/stok/export-produk.csv')
@login_required
def stok_export_produk():
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect, url_for
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id
    category_id = request.args.get('category_id', '').strip()
    stok_q = request.args.get('q', '').strip()
    stok_status = request.args.get('stok_status', '').strip().lower()
    if request.args.get('menipis') == '1' and stok_status not in ('habis', 'menipis', 'aman'):
        stok_status = 'menipis'
    if stok_status not in ('', 'habis', 'menipis', 'aman'):
        stok_status = ''
    stok_sort = request.args.get('sort', '').strip().lower()
    allowed_sort = ('', 'nama', 'stok_asc', 'stok_desc', 'nilai_beli_desc', 'nilai_jual_desc', 'kategori')
    if stok_sort not in allowed_sort:
        stok_sort = 'nama'
    if stok_sort == '':
        stok_sort = 'nama'

    pq = _stok_apply_filters(_stok_product_base(tenant_id), tenant_id, category_id, stok_q, stok_status)
    pq = _stok_apply_sort(pq, stok_sort)
    rows = pq.options(joinedload(Product.category), joinedload(Product.supplier)).all()

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow([
        'nama', 'barcode', 'kategori', 'supplier', 'satuan', 'stok', 'stok_minimum', 'status_stok',
        'harga_beli', 'harga_jual', 'nilai_persediaan_beli', 'potensi_omzet_ecer',
    ])
    for p in rows:
        cat = p.category.nama if p.category else ''
        sup = p.supplier.nama if p.supplier else ''
        st = float(p.stok or 0)
        if st <= 0:
            status = 'habis'
        elif p.stok_menipis:
            status = 'menipis'
        else:
            status = 'aman'
        nilai_beli = st * float(p.harga_beli or 0)
        nilai_jual = st * float(p.harga_jual or 0)
        w.writerow([
            p.nama,
            p.barcode or '',
            cat,
            sup,
            p.satuan or '',
            p.stok,
            p.stok_minimum,
            status,
            p.harga_beli or 0,
            p.harga_jual or 0,
            nilai_beli,
            nilai_jual,
        ])

    fn = f'laporan-stok-produk-{datetime.utcnow().strftime("%Y%m%d-%H%M")}.csv'
    data = '\ufeff' + buf.getvalue()
    return Response(
        data,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={fn}'},
    )
