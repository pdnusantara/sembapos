import csv
from io import StringIO
from urllib.parse import urlencode

from flask import Blueprint, render_template, request, Response, url_for
from flask_login import login_required, current_user
from datetime import date, datetime, timedelta, timezone as dt_timezone
from sqlalchemy import func, desc, asc, or_
from sqlalchemy.orm import joinedload

from .. import db
from ..fifo_costing import create_cost_layer
from ..models import (
    Transaction,
    TransactionItem,
    TransactionPayment,
    SalesReturn,
    SalesReturnItem,
    Product,
    OperationalExpense,
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
    Branch,
    ProductCategory,
    User,
    Member,
    VoucherRedemption,
    InventoryCostLayer,
    ProductAuditLog,
)
from ..timezones import (
    format_utc_naive_as_local,
    get_zoneinfo_required,
    local_today_date,
    local_yyyymmdd_for_tenant_id,
    parse_ymd_to_date,
    resolve_effective_timezone_id,
    utc_naive_bounds_for_local_date,
    utc_naive_bounds_for_report_period,
)

reports_bp = Blueprint('reports', __name__, url_prefix='/reports')


def _sql_calendar_date_from_utc_naive(column, tz_id: str):
    """Tanggal kalender lokal untuk kolom UTC-naive (PostgreSQL); fallback UTC di SQLite."""
    if db.engine.dialect.name == 'postgresql':
        return func.date(func.timezone(tz_id, func.timezone('UTC', column)))
    return func.date(column)


def _utc_bounds_local_dates(d_from: date, d_to: date, tz_id: str):
    start_utc, _ = utc_naive_bounds_for_local_date(d_from, tz_id)
    _, end_utc = utc_naive_bounds_for_local_date(d_to, tz_id)
    return start_utc, end_utc


def _default_range_utc_bounds(tz_id: str, days: int = 30):
    """Rentang UTC-naive untuk N hari kalender terakhir di tz_id (inklusif)."""
    today = local_today_date(tz_id)
    d_start = today - timedelta(days=days - 1)
    return _utc_bounds_local_dates(d_start, today, tz_id)


def _coerce_int(raw):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _gross_profit_filters(tenant_id):
    tz_id = resolve_effective_timezone_id(current_user)
    d_from = parse_ymd_to_date(request.args.get('date_from'))
    d_to = parse_ymd_to_date(request.args.get('date_to'))
    today = local_today_date(tz_id)
    if d_from is None or d_to is None:
        d_to = today
        d_from = d_to - timedelta(days=6)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    utc_start, utc_end = _utc_bounds_local_dates(d_from, d_to, tz_id)

    branch_id_param = (request.args.get('branch_id') or '').strip()
    selected_branch_id = ''
    if current_user.role == 'kasir' and current_user.branch_id:
        selected_branch_id = str(current_user.branch_id)
    elif branch_id_param and tenant_id:
        bid = _coerce_int(branch_id_param)
        if bid and Branch.query.filter_by(id=bid, tenant_id=tenant_id, aktif=True).first():
            selected_branch_id = str(bid)

    return utc_start, utc_end, d_from, d_to, selected_branch_id


def _gross_profit_common_queries(tenant_id, date_from, date_to, selected_branch_id):
    trx_q = Transaction.query.filter(
        Transaction.tenant_id == tenant_id,
        Transaction.status == 'selesai',
        Transaction.created_at.between(date_from, date_to),
    )
    if selected_branch_id:
        trx_q = trx_q.filter(Transaction.branch_id == int(selected_branch_id))

    ret_q = SalesReturn.query.filter(
        SalesReturn.tenant_id == tenant_id,
        SalesReturn.created_at.between(date_from, date_to),
    )
    if selected_branch_id:
        ret_q = ret_q.filter(SalesReturn.branch_id == int(selected_branch_id))

    op_q = OperationalExpense.query.filter(
        OperationalExpense.tenant_id == tenant_id,
        OperationalExpense.tanggal.between(date_from, date_to),
    )
    if selected_branch_id:
        op_q = op_q.filter(OperationalExpense.branch_id == int(selected_branch_id))

    return trx_q, ret_q, op_q


def _gross_profit_daily_rows(d_start: date, d_end: date, sales_map, return_map, hpp_map, opex_map):
    rows = []
    cursor = d_start
    end_day = d_end
    while cursor <= end_day:
        key = cursor.isoformat()
        sales = float(sales_map.get(key, 0) or 0)
        returns = float(return_map.get(key, 0) or 0)
        hpp = float(hpp_map.get(key, 0) or 0)
        opex = float(opex_map.get(key, 0) or 0)
        net_sales = sales - returns
        gross_profit = net_sales - hpp
        operating_profit = gross_profit - opex
        gross_margin_pct = ((gross_profit / net_sales) * 100.0) if net_sales > 0 else 0.0
        opex_ratio_pct = ((opex / net_sales) * 100.0) if net_sales > 0 else 0.0
        rows.append(
            {
                'date': key,
                'sales': sales,
                'returns': returns,
                'net_sales': net_sales,
                'hpp': hpp,
                'gross_profit': gross_profit,
                'opex': opex,
                'operating_profit': operating_profit,
                'gross_margin_pct': gross_margin_pct,
                'opex_ratio_pct': opex_ratio_pct,
            }
        )
        cursor += timedelta(days=1)
    return rows


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


def _sales_breakdown_granularity():
    gran = (request.args.get('granularity') or 'daily').strip().lower()
    if gran not in ('daily', 'weekly', 'monthly'):
        gran = 'daily'
    return gran


def _sales_breakdown_prev_period(date_from, date_to):
    delta = date_to - date_from
    prev_end = date_from - timedelta(microseconds=1)
    prev_start = prev_end - delta
    return prev_start, prev_end


def _sales_breakdown_bucket_key(dt_value, granularity, tz_id: str | None = None):
    if not dt_value:
        return None
    if tz_id:
        aware = dt_value.replace(tzinfo=dt_timezone.utc)
        dt_local = aware.astimezone(get_zoneinfo_required(tz_id))
    else:
        dt_local = dt_value
    if granularity == 'monthly':
        return dt_local.strftime('%Y-%m')
    if granularity == 'weekly':
        y, w, _ = dt_local.isocalendar()
        return f'{y}-W{w:02d}'
    return dt_local.strftime('%Y-%m-%d')


def _sales_breakdown_returns_map(trx_ids_q):
    rows = (
        db.session.query(
            SalesReturnItem.product_id.label('product_id'),
            func.coalesce(func.sum(SalesReturnItem.subtotal), 0).label('retur_total'),
        )
        .join(SalesReturn, SalesReturnItem.return_id == SalesReturn.id)
        .filter(SalesReturn.source_transaction_id.in_(trx_ids_q))
        .group_by(SalesReturnItem.product_id)
        .all()
    )
    return {int(r.product_id): float(r.retur_total or 0) for r in rows if r.product_id is not None}


def _sales_breakdown_apply_net_and_abc(produk_rows, return_map):
    rows = []
    for r in produk_rows:
        pid = int(r.get('produk_key') or 0)
        retur = float(return_map.get(pid, 0) or 0)
        net_sales = float(r.get('omzet') or 0) - retur
        net_margin = net_sales - float(r.get('hpp') or 0)
        net_margin_pct = ((net_margin / net_sales) * 100.0) if net_sales > 0 else 0.0
        x = dict(r)
        x['retur'] = retur
        x['net_sales'] = net_sales
        x['net_margin'] = net_margin
        x['net_margin_pct'] = net_margin_pct
        rows.append(x)

    total_net = sum(r['net_sales'] for r in rows) or 0.0
    running = 0.0
    for r in sorted(rows, key=lambda x: x['net_sales'], reverse=True):
        share = (r['net_sales'] / total_net) * 100.0 if total_net > 0 else 0.0
        running += share
        if running <= 80:
            abc = 'A'
        elif running <= 95:
            abc = 'B'
        else:
            abc = 'C'
        r['share_pct'] = share
        r['cum_share_pct'] = running
        r['abc'] = abc
    return rows


def _sales_breakdown_time_series(trx_rows, hpp_unit_lookup, granularity, tz_id: str | None = None):
    buckets = {}
    for row in trx_rows:
        bkey = _sales_breakdown_bucket_key(row.created_at, granularity, tz_id)
        if not bkey:
            continue
        entry = buckets.setdefault(
            bkey,
            {'bucket': bkey, 'omzet': 0.0, 'hpp': 0.0, 'retur': 0.0, 'net_sales': 0.0, 'margin': 0.0, 'trx_count': 0},
        )
        entry['omzet'] += float(row.omzet_total or 0)
        entry['hpp'] += float(row.hpp_total or 0)
        entry['retur'] += float(row.retur_total or 0)
        entry['trx_count'] += int(row.trx_count or 0)

    sorted_rows = [buckets[k] for k in sorted(buckets.keys())]
    for r in sorted_rows:
        r['net_sales'] = r['omzet'] - r['retur']
        r['margin'] = r['net_sales'] - r['hpp']
    labels = [r['bucket'] for r in sorted_rows]
    return {
        'rows': sorted_rows,
        'labels': labels,
        'omzet': [float(r['omzet']) for r in sorted_rows],
        'hpp': [float(r['hpp']) for r in sorted_rows],
        'retur': [float(r['retur']) for r in sorted_rows],
        'margin': [float(r['margin']) for r in sorted_rows],
        'trx_count': [int(r['trx_count']) for r in sorted_rows],
    }


def _sales_breakdown_hpp_confidence(trx_ids_q):
    snapshot_col = None
    for col_name in ('hpp_snapshot', 'harga_beli_snapshot', 'modal_snapshot'):
        col = getattr(TransactionItem, col_name, None)
        if col is not None:
            snapshot_col = col
            break
    total_items = (
        db.session.query(func.count(TransactionItem.id))
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .filter(Transaction.id.in_(trx_ids_q))
        .scalar()
        or 0
    )
    if not snapshot_col or total_items <= 0:
        return {'source': 'master', 'total_items': int(total_items), 'snapshot_items': 0, 'coverage_pct': 0.0}
    snapshot_items = (
        db.session.query(func.count(TransactionItem.id))
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .filter(Transaction.id.in_(trx_ids_q), snapshot_col.isnot(None))
        .scalar()
        or 0
    )
    coverage = (float(snapshot_items) / float(total_items)) * 100.0 if total_items > 0 else 0.0
    return {
        'source': 'snapshot_fallback_master',
        'total_items': int(total_items),
        'snapshot_items': int(snapshot_items),
        'coverage_pct': coverage,
    }


def _report_period_from_request(mode, tanggal, bulan, tz_id: str):
    return utc_naive_bounds_for_report_period(mode, tanggal, bulan, tz_id)


def _apply_reports_index_filters(q, *, tenant_id, start, end, branch_id=None, cashier_id=None, metode_bayar=None, member_scope='all'):
    q = q.filter(
        Transaction.created_at.between(start, end),
        Transaction.status == 'selesai',
    )
    if tenant_id:
        q = q.filter_by(tenant_id=tenant_id)
    if branch_id:
        q = q.filter(Transaction.branch_id == int(branch_id))
    if cashier_id:
        q = q.filter(Transaction.user_id == int(cashier_id))
    if metode_bayar:
        m = str(metode_bayar).strip().lower()
        if m in ('tunai', 'transfer', 'qris', 'kredit'):
            q = q.filter(
                Transaction.id.in_(
                    db.session.query(TransactionPayment.transaction_id).filter(
                        TransactionPayment.method == m
                    )
                )
            )
        else:
            q = q.filter(func.lower(Transaction.metode_bayar) == m)
    if member_scope == 'member':
        q = q.filter(Transaction.member_id.isnot(None))
    elif member_scope == 'non_member':
        q = q.filter(Transaction.member_id.is_(None))
    return q


def _item_hpp_unit(it):
    if getattr(it, 'hpp_snapshot', None) is not None:
        return float(it.hpp_snapshot or 0)
    return float((it.product.harga_beli if it.product else 0) or 0)


def _compute_report_bundle(transactions):
    total_penjualan = sum(float(t.total or 0) for t in transactions)
    total_transaksi = len(transactions)
    total_omset_kotor = sum(float(t.subtotal or 0) for t in transactions)
    total_diskon_nota = sum(float(t.diskon or 0) for t in transactions)
    payment_summary = {}
    product_profit = {}
    trx_profit_rows = []
    fallback_item_count = 0
    discount_high_count = 0

    for t in transactions:
        if t.payments:
            for p in t.payments:
                metode = (p.method or 'lainnya').strip().lower() or 'lainnya'
                row = payment_summary.setdefault(metode, {'metode': metode, 'count': 0, 'total': 0.0})
                row['count'] += 1
                row['total'] += float(p.amount or 0)
        else:
            metode = (t.metode_bayar or 'lainnya').strip().lower() or 'lainnya'
            row = payment_summary.setdefault(metode, {'metode': metode, 'count': 0, 'total': 0.0})
            row['count'] += 1
            row['total'] += float(t.bayar or t.total or 0)

        subtotal_hpp = 0.0
        for it in t.items:
            unit_hpp = _item_hpp_unit(it)
            if getattr(it, 'hpp_snapshot', None) is None:
                fallback_item_count += 1
            line_hpp = float(it.qty or 0) * unit_hpp
            line_profit = float(it.subtotal or 0) - line_hpp
            subtotal_hpp += line_hpp
            pkey = int(it.product_id or 0)
            prow = product_profit.setdefault(pkey, {
                'product_id': pkey,
                'nama_produk': it.nama_produk or '-',
                'qty': 0.0,
                'omzet': 0.0,
                'hpp': 0.0,
                'profit': 0.0,
            })
            prow['qty'] += float(it.qty or 0)
            prow['omzet'] += float(it.subtotal or 0)
            prow['hpp'] += line_hpp
            prow['profit'] += line_profit

        trx_profit = float(t.total or 0) - subtotal_hpp
        trx_profit_rows.append({
            'id': t.id,
            'nomor': t.nomor,
            'total': float(t.total or 0),
            'hpp': subtotal_hpp,
            'profit': trx_profit,
            'diskon': float(t.diskon or 0),
            'subtotal': float(t.subtotal or 0),
            'created_at': t.created_at,
            'kasir': (t.user.nama if t.user else '-'),
        })
        if float(t.subtotal or 0) > 0 and (float(t.diskon or 0) / float(t.subtotal or 1)) >= 0.2:
            discount_high_count += 1

    payment_summary_rows = sorted(payment_summary.values(), key=lambda x: x['total'], reverse=True)
    total_hpp = sum(r['hpp'] for r in trx_profit_rows)
    total_laba_kotor = total_penjualan - total_hpp
    neg_margin_count = sum(1 for r in trx_profit_rows if r['profit'] < 0)

    product_rows = list(product_profit.values())
    top_products = sorted(product_rows, key=lambda x: x['profit'], reverse=True)[:10]
    bottom_products = sorted(product_rows, key=lambda x: x['profit'])[:10]
    top_transactions = sorted(trx_profit_rows, key=lambda x: x['profit'], reverse=True)[:10]
    bottom_transactions = sorted(trx_profit_rows, key=lambda x: x['profit'])[:10]

    return {
        'total_penjualan': total_penjualan,
        'total_transaksi': total_transaksi,
        'total_omset_kotor': total_omset_kotor,
        'total_diskon_nota': total_diskon_nota,
        'payment_summary_rows': payment_summary_rows,
        'total_hpp': total_hpp,
        'total_laba_kotor': total_laba_kotor,
        'fallback_item_count': fallback_item_count,
        'discount_high_count': discount_high_count,
        'neg_margin_count': neg_margin_count,
        'top_products': top_products,
        'bottom_products': bottom_products,
        'top_transactions': top_transactions,
        'bottom_transactions': bottom_transactions,
    }


@reports_bp.route('/')
@login_required
def index():
    tenant_id = current_user.tenant_id
    tz_id = resolve_effective_timezone_id(current_user)
    today = local_today_date(tz_id)
    mode = request.args.get('mode', 'harian')
    tanggal = request.args.get('tanggal') or today.isoformat()
    bulan = request.args.get('bulan') or today.strftime('%Y-%m')
    start, end = _report_period_from_request(mode, tanggal, bulan, tz_id)
    compare_prev = (request.args.get('compare_prev') or '1').strip() != '0'

    selected_branch_id = ''
    selected_cashier_id = ''
    selected_metode = (request.args.get('metode_bayar') or '').strip().lower()
    member_scope = (request.args.get('member_scope') or 'all').strip().lower()
    if member_scope not in ('all', 'member', 'non_member'):
        member_scope = 'all'

    if current_user.role == 'kasir' and current_user.branch_id:
        selected_branch_id = str(current_user.branch_id)
        selected_cashier_id = str(current_user.id)
    else:
        braw = (request.args.get('branch_id') or '').strip()
        uraw = (request.args.get('cashier_id') or '').strip()
        if braw.isdigit():
            selected_branch_id = braw
        if uraw.isdigit():
            selected_cashier_id = uraw

    q = _apply_reports_index_filters(
        Transaction.query,
        tenant_id=tenant_id,
        start=start,
        end=end,
        branch_id=selected_branch_id or None,
        cashier_id=selected_cashier_id or None,
        metode_bayar=selected_metode or None,
        member_scope=member_scope,
    )

    transactions = (
        q.options(
            joinedload(Transaction.user),
            joinedload(Transaction.items).joinedload(TransactionItem.product),
            joinedload(Transaction.payments),
        )
        .order_by(Transaction.created_at.desc())
        .all()
    )
    metrics = _compute_report_bundle(transactions)

    op_q = OperationalExpense.query.filter(
        OperationalExpense.tanggal.between(start, end)
    )
    if tenant_id:
        op_q = op_q.filter(OperationalExpense.tenant_id == tenant_id)
    if selected_branch_id:
        op_q = op_q.filter(OperationalExpense.branch_id == int(selected_branch_id))
    total_biaya_operasional = float(
        op_q.with_entities(func.coalesce(func.sum(OperationalExpense.jumlah), 0)).scalar() or 0
    )
    laba_setelah_operasional = metrics['total_laba_kotor'] - total_biaya_operasional

    prev_metrics = None
    prev_total_biaya_operasional = 0.0
    prev_laba_setelah_operasional = 0.0
    if compare_prev:
        duration = end - start
        prev_end = start - timedelta(seconds=1)
        prev_start = prev_end - duration
        prev_q = _apply_reports_index_filters(
            Transaction.query,
            tenant_id=tenant_id,
            start=prev_start,
            end=prev_end,
            branch_id=selected_branch_id or None,
            cashier_id=selected_cashier_id or None,
            metode_bayar=selected_metode or None,
            member_scope=member_scope,
        )
        prev_transactions = (
            prev_q.options(
                joinedload(Transaction.user),
                joinedload(Transaction.items).joinedload(TransactionItem.product),
                joinedload(Transaction.payments),
            )
            .order_by(Transaction.created_at.desc())
            .all()
        )
        prev_metrics = _compute_report_bundle(prev_transactions)
        prev_op_q = OperationalExpense.query.filter(
            OperationalExpense.tanggal.between(prev_start, prev_end)
        )
        if tenant_id:
            prev_op_q = prev_op_q.filter(OperationalExpense.tenant_id == tenant_id)
        if selected_branch_id:
            prev_op_q = prev_op_q.filter(OperationalExpense.branch_id == int(selected_branch_id))
        prev_total_biaya_operasional = float(
            prev_op_q.with_entities(func.coalesce(func.sum(OperationalExpense.jumlah), 0)).scalar() or 0
        )
        prev_laba_setelah_operasional = prev_metrics['total_laba_kotor'] - prev_total_biaya_operasional

    fallback_events_q = ProductAuditLog.query.filter(
        ProductAuditLog.tenant_id == tenant_id,
        ProductAuditLog.created_at.between(start, end),
        ProductAuditLog.action.in_((
            'fifo_fallback_cost',
            'fifo_stock_out_fallback',
            'fifo_return_fallback_layer',
        )),
    )
    fallback_events = int(fallback_events_q.count())

    zi = get_zoneinfo_required(tz_id)
    trend_end_local = end.replace(tzinfo=dt_timezone.utc).astimezone(zi).date()
    trend_start_local = trend_end_local - timedelta(days=6)
    trend_start, trend_end = _utc_bounds_local_dates(trend_start_local, trend_end_local, tz_id)
    trend_q = _apply_reports_index_filters(
        Transaction.query,
        tenant_id=tenant_id,
        start=trend_start,
        end=trend_end,
        branch_id=selected_branch_id or None,
        cashier_id=selected_cashier_id or None,
        metode_bayar=selected_metode or None,
        member_scope=member_scope,
    )
    trend_transactions = trend_q.options(
        joinedload(Transaction.items).joinedload(TransactionItem.product),
        joinedload(Transaction.payments),
    ).all()
    trend_map = {}
    cursor = trend_start_local
    while cursor <= trend_end_local:
        trend_map[cursor.isoformat()] = {'omzet': 0.0, 'hpp': 0.0, 'profit': 0.0}
        cursor += timedelta(days=1)
    for t in trend_transactions:
        key = t.created_at.replace(tzinfo=dt_timezone.utc).astimezone(zi).date().isoformat()
        if key not in trend_map:
            continue
        trx_hpp = 0.0
        for it in t.items:
            trx_hpp += float(it.qty or 0) * _item_hpp_unit(it)
        trend_map[key]['omzet'] += float(t.total or 0)
        trend_map[key]['hpp'] += trx_hpp
        trend_map[key]['profit'] += float(t.total or 0) - trx_hpp
    trend_labels = sorted(trend_map.keys())
    trend_series = {
        'labels': trend_labels,
        'omzet': [trend_map[k]['omzet'] for k in trend_labels],
        'hpp': [trend_map[k]['hpp'] for k in trend_labels],
        'profit': [trend_map[k]['profit'] for k in trend_labels],
    }

    branches = []
    cashiers = []
    if current_user.role != 'kasir':
        branches = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Branch.nama).all()
        cashiers = User.query.filter(
            User.tenant_id == tenant_id,
            User.role.in_(('kasir', 'admin')),
            User.aktif == True,
        ).order_by(User.nama).all()

    return render_template(
        'reports/index.html',
        transactions=transactions,
        total_penjualan=metrics['total_penjualan'],
        total_transaksi=metrics['total_transaksi'],
        total_omset_kotor=metrics['total_omset_kotor'],
        total_diskon_nota=metrics['total_diskon_nota'],
        total_hpp=metrics['total_hpp'],
        total_laba_kotor=metrics['total_laba_kotor'],
        total_biaya_operasional=total_biaya_operasional,
        laba_setelah_operasional=laba_setelah_operasional,
        payment_summary_rows=metrics['payment_summary_rows'],
        prev_metrics=prev_metrics,
        prev_total_biaya_operasional=prev_total_biaya_operasional,
        prev_laba_setelah_operasional=prev_laba_setelah_operasional,
        compare_prev=compare_prev,
        mode=mode,
        tanggal=tanggal,
        bulan=bulan,
        branches=branches,
        cashiers=cashiers,
        selected_branch_id=selected_branch_id,
        selected_cashier_id=selected_cashier_id,
        selected_metode=selected_metode,
        member_scope=member_scope,
        alerts={
            'neg_margin_count': metrics['neg_margin_count'],
            'discount_high_count': metrics['discount_high_count'],
            'fallback_item_count': metrics['fallback_item_count'],
            'fallback_events': fallback_events,
        },
        top_products=metrics['top_products'],
        bottom_products=metrics['bottom_products'],
        top_transactions=metrics['top_transactions'],
        bottom_transactions=metrics['bottom_transactions'],
        trend_series=trend_series,
    )


@reports_bp.route('/export-item-detail.csv')
@login_required
def export_item_detail():
    tenant_id = current_user.tenant_id
    tz_id = resolve_effective_timezone_id(current_user)
    today = local_today_date(tz_id)
    mode = request.args.get('mode', 'harian')
    tanggal = request.args.get('tanggal') or today.isoformat()
    bulan = request.args.get('bulan') or today.strftime('%Y-%m')
    start, end = _report_period_from_request(mode, tanggal, bulan, tz_id)

    selected_metode = (request.args.get('metode_bayar') or '').strip().lower()
    member_scope = (request.args.get('member_scope') or 'all').strip().lower()
    if member_scope not in ('all', 'member', 'non_member'):
        member_scope = 'all'

    selected_branch_id = ''
    selected_cashier_id = ''
    if current_user.role == 'kasir' and current_user.branch_id:
        selected_branch_id = str(current_user.branch_id)
        selected_cashier_id = str(current_user.id)
    else:
        braw = (request.args.get('branch_id') or '').strip()
        uraw = (request.args.get('cashier_id') or '').strip()
        if braw.isdigit():
            selected_branch_id = braw
        if uraw.isdigit():
            selected_cashier_id = uraw

    q = _apply_reports_index_filters(
        Transaction.query,
        tenant_id=tenant_id,
        start=start,
        end=end,
        branch_id=selected_branch_id or None,
        cashier_id=selected_cashier_id or None,
        metode_bayar=selected_metode or None,
        member_scope=member_scope,
    )
    transactions = q.options(
        joinedload(Transaction.user),
        joinedload(Transaction.items).joinedload(TransactionItem.product),
        joinedload(Transaction.payments),
        joinedload(Transaction.branch),
    ).order_by(Transaction.created_at.desc()).all()

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow([
        'nomor_transaksi', 'waktu', 'kasir', 'cabang', 'metode_bayar',
        'breakdown_metode',
        'member', 'produk', 'qty', 'harga_jual_unit', 'hpp_unit',
        'subtotal_jual', 'subtotal_hpp', 'laba_baris',
    ])
    for t in transactions:
        for it in t.items:
            unit_hpp = _item_hpp_unit(it)
            subtotal_hpp = float(it.qty or 0) * unit_hpp
            laba_baris = float(it.subtotal or 0) - subtotal_hpp
            w.writerow([
                t.nomor,
                format_utc_naive_as_local(t.created_at, tz_id, '%Y-%m-%d %H:%M:%S') if t.created_at else '',
                t.user.nama if t.user else '',
                t.branch.nama if t.branch else '',
                (t.metode_bayar or ''),
                '; '.join([f'{(p.method or "").upper()}={float(p.amount or 0):.0f}' for p in (t.payments or [])]) if t.payments else '',
                'ya' if t.member_id else 'tidak',
                it.nama_produk or '',
                float(it.qty or 0),
                float(it.harga or 0),
                unit_hpp,
                float(it.subtotal or 0),
                subtotal_hpp,
                laba_baris,
            ])
    data = '\ufeff' + buf.getvalue()
    fn = f'laporan-item-detail-{local_yyyymmdd_for_tenant_id(tenant_id)}-{datetime.utcnow().strftime("%H%M")}.csv'
    return Response(
        data,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={fn}'},
    )


@reports_bp.route('/penjualan-produk-kategori')
@login_required
def sales_breakdown():
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id
    tz_id = resolve_effective_timezone_id(current_user)
    d_from = parse_ymd_to_date(request.args.get('date_from'))
    d_to = parse_ymd_to_date(request.args.get('date_to'))
    today = local_today_date(tz_id)
    if d_from is None or d_to is None:
        d_to = today
        d_from = d_to - timedelta(days=29)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    date_from, date_to = _utc_bounds_local_dates(d_from, d_to, tz_id)

    tab = (request.args.get('tab') or 'produk').strip().lower()
    if tab not in ('produk', 'kategori'):
        tab = 'produk'
    can_view_profit = current_user.role != 'kasir'
    granularity = _sales_breakdown_granularity()
    compare_prev = (request.args.get('compare_prev') or '1').strip() != '0'

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
    returns_map = _sales_breakdown_returns_map(trx_ids_q)
    produk_rows = _sales_breakdown_apply_net_and_abc(produk_rows, returns_map)
    hpp_confidence = _sales_breakdown_hpp_confidence(trx_ids_q)

    prev_start, prev_end = _sales_breakdown_prev_period(date_from, date_to)
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
    if produk_rows:
        no_drop = sorted(produk_rows, key=lambda x: x.get('retur', 0), reverse=True)
        alerts['high_returns_count'] = len([r for r in produk_rows if r.get('retur', 0) > 0])
        alerts['high_returns_top'] = no_drop[:3]
        alerts['negative_net_margin_count'] = len([r for r in produk_rows if r.get('net_margin', 0) < 0])
    else:
        alerts['high_returns_count'] = 0
        alerts['high_returns_top'] = []
        alerts['negative_net_margin_count'] = 0

    focus = (request.args.get('focus') or '').strip().lower()
    if focus not in ('', 'margin_negatif', 'retur_tinggi', 'abc_c', 'margin_tipis'):
        focus = ''
    filtered_produk_rows = produk_rows
    if focus == 'margin_negatif':
        filtered_produk_rows = [r for r in produk_rows if r.get('net_margin', 0) < 0]
    elif focus == 'retur_tinggi':
        filtered_produk_rows = [r for r in produk_rows if r.get('retur', 0) > 0 and r.get('omzet', 0) > 0 and ((r.get('retur', 0) / r.get('omzet', 1)) * 100.0) >= 5]
    elif focus == 'abc_c':
        filtered_produk_rows = [r for r in produk_rows if r.get('abc') == 'C']
    elif focus == 'margin_tipis':
        filtered_produk_rows = [r for r in produk_rows if r.get('net_margin_pct', 0) < 10]

    produk_totals = {
        'qty': sum(r['qty'] for r in produk_rows),
        'omzet': sum(r['omzet'] for r in produk_rows),
        'hpp': sum(r['hpp'] for r in produk_rows),
        'retur': sum(r.get('retur', 0) for r in produk_rows),
    }
    produk_totals['margin'] = produk_totals['omzet'] - produk_totals['hpp']
    produk_totals['margin_pct'] = ((produk_totals['margin'] / produk_totals['omzet']) * 100.0) if produk_totals['omzet'] > 0 else 0.0
    produk_totals['net_sales'] = produk_totals['omzet'] - produk_totals['retur']
    produk_totals['net_margin'] = produk_totals['net_sales'] - produk_totals['hpp']
    produk_totals['net_margin_pct'] = (
        (produk_totals['net_margin'] / produk_totals['net_sales']) * 100.0 if produk_totals['net_sales'] > 0 else 0.0
    )

    total_transaksi = (
        trx_q.with_entities(func.count(func.distinct(Transaction.id))).scalar() or 0
    )
    produk_totals['trx_count'] = int(total_transaksi)

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

    prev_produk_totals = {'omzet': 0.0, 'net_sales': 0.0, 'net_margin': 0.0, 'trx_count': 0}
    prev_change = {'omzet_pct': 0.0, 'net_sales_pct': 0.0, 'net_margin_pct': 0.0, 'trx_count_pct': 0.0}
    if compare_prev:
        prev_trx_q = Transaction.query.filter(
            Transaction.tenant_id == tenant_id,
            Transaction.status == 'selesai',
            Transaction.created_at.between(prev_start, prev_end),
        )
        if selected_branch_id:
            prev_trx_q = prev_trx_q.filter(Transaction.branch_id == int(selected_branch_id))
        prev_trx_ids_q = prev_trx_q.with_entities(Transaction.id)
        prev_omzet = (
            prev_trx_q.with_entities(func.coalesce(func.sum(Transaction.total), 0)).scalar() or 0
        )
        prev_hpp = (
            db.session.query(func.coalesce(func.sum(TransactionItem.qty * hpp_unit_expr), 0))
            .join(Transaction, TransactionItem.transaction_id == Transaction.id)
            .outerjoin(Product, TransactionItem.product_id == Product.id)
            .filter(Transaction.id.in_(prev_trx_ids_q))
            .scalar()
            or 0
        )
        prev_retur = (
            db.session.query(func.coalesce(func.sum(SalesReturn.total_retur), 0))
            .filter(SalesReturn.source_transaction_id.in_(prev_trx_ids_q))
            .scalar()
            or 0
        )
        prev_trx_count = prev_trx_q.with_entities(func.count(func.distinct(Transaction.id))).scalar() or 0
        prev_produk_totals['omzet'] = float(prev_omzet)
        prev_produk_totals['net_sales'] = float(prev_omzet) - float(prev_retur)
        prev_produk_totals['net_margin'] = prev_produk_totals['net_sales'] - float(prev_hpp)
        prev_produk_totals['trx_count'] = int(prev_trx_count)
        for key in ('omzet', 'net_sales', 'net_margin', 'trx_count'):
            base = float(prev_produk_totals.get(key) or 0)
            cur = float(produk_totals.get(key) or 0)
            pct = ((cur - base) / base) * 100.0 if base != 0 else 0.0
            prev_change[f'{key}_pct'] = pct

    trx_base_rows = (
        trx_q.with_entities(
            Transaction.id.label('transaction_id'),
            Transaction.created_at.label('created_at'),
            Transaction.total.label('omzet_total'),
        ).all()
    )
    trx_hpp_rows = (
        db.session.query(
            TransactionItem.transaction_id.label('transaction_id'),
            func.coalesce(func.sum(TransactionItem.qty * hpp_unit_expr), 0).label('hpp_total'),
        )
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .outerjoin(Product, TransactionItem.product_id == Product.id)
        .filter(Transaction.id.in_(trx_ids_q))
        .group_by(TransactionItem.transaction_id)
        .all()
    )
    trx_return_rows = (
        db.session.query(
            SalesReturn.source_transaction_id.label('transaction_id'),
            func.coalesce(func.sum(SalesReturn.total_retur), 0).label('retur_total'),
        )
        .filter(SalesReturn.source_transaction_id.in_(trx_ids_q))
        .group_by(SalesReturn.source_transaction_id)
        .all()
    )
    hpp_map_by_trx = {int(r.transaction_id): float(r.hpp_total or 0) for r in trx_hpp_rows}
    retur_map_by_trx = {int(r.transaction_id): float(r.retur_total or 0) for r in trx_return_rows}
    trx_time_rows = []
    for r in trx_base_rows:
        tid = int(r.transaction_id)
        trx_time_rows.append(
            type(
                'Row',
                (),
                {
                    'created_at': r.created_at,
                    'omzet_total': float(r.omzet_total or 0),
                    'hpp_total': float(hpp_map_by_trx.get(tid, 0) or 0),
                    'retur_total': float(retur_map_by_trx.get(tid, 0) or 0),
                    'trx_count': 1,
                },
            )()
        )
    trend = _sales_breakdown_time_series(trx_time_rows, hpp_unit_expr, granularity, tz_id)

    branch_compare_rows = (
        db.session.query(
            Branch.id.label('branch_id'),
            Branch.nama.label('branch_name'),
            func.coalesce(func.sum(Transaction.total), 0).label('omzet'),
            func.count(func.distinct(Transaction.id)).label('trx_count'),
        )
        .join(Transaction, Transaction.branch_id == Branch.id)
        .filter(Transaction.id.in_(trx_ids_q))
        .group_by(Branch.id, Branch.nama)
        .all()
    )
    branch_hpp_rows = (
        db.session.query(
            Transaction.branch_id.label('branch_id'),
            func.coalesce(func.sum(TransactionItem.qty * hpp_unit_expr), 0).label('hpp'),
        )
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .outerjoin(Product, TransactionItem.product_id == Product.id)
        .filter(Transaction.id.in_(trx_ids_q))
        .group_by(Transaction.branch_id)
        .all()
    )
    branch_retur_rows = (
        db.session.query(
            SalesReturn.branch_id.label('branch_id'),
            func.coalesce(func.sum(SalesReturn.total_retur), 0).label('retur'),
        )
        .filter(SalesReturn.source_transaction_id.in_(trx_ids_q))
        .group_by(SalesReturn.branch_id)
        .all()
    )
    hpp_by_branch = {int(r.branch_id): float(r.hpp or 0) for r in branch_hpp_rows if r.branch_id is not None}
    retur_by_branch = {int(r.branch_id): float(r.retur or 0) for r in branch_retur_rows if r.branch_id is not None}
    branch_compare = []
    for r in branch_compare_rows:
        bid = int(r.branch_id)
        omzet = float(r.omzet or 0)
        retur = float(retur_by_branch.get(bid, 0) or 0)
        net_sales = omzet - retur
        hpp = float(hpp_by_branch.get(bid, 0) or 0)
        margin = net_sales - hpp
        branch_compare.append({
            'branch_id': bid,
            'branch_name': r.branch_name,
            'omzet': omzet,
            'retur': retur,
            'net_sales': net_sales,
            'hpp': hpp,
            'margin': margin,
            'trx_count': int(r.trx_count or 0),
        })
    branch_compare.sort(key=lambda x: x['net_sales'], reverse=True)

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

    date_from_str = d_from.isoformat()
    date_to_str = d_to.isoformat()
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
        ('granularity', granularity),
        ('compare_prev', '1' if compare_prev else '0'),
        ('focus', focus),
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
        filtered_produk_rows=filtered_produk_rows,
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
        trend_labels=trend['labels'],
        trend_omzet=trend['omzet'],
        trend_hpp=trend['hpp'],
        trend_retur=trend['retur'],
        trend_margin=trend['margin'],
        trend_trx_count=trend['trx_count'],
        granularity=granularity,
        compare_prev=compare_prev,
        prev_produk_totals=prev_produk_totals,
        prev_change=prev_change,
        branch_compare=branch_compare,
        can_view_profit=can_view_profit,
        hpp_confidence=hpp_confidence,
        focus=focus,
        tab_produk_qs=tab_produk_qs,
        tab_kategori_qs=tab_kategori_qs,
    )


@reports_bp.route('/penjualan-produk-kategori/export-summary.csv')
@login_required
def sales_breakdown_export_summary():
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id
    tz_id = resolve_effective_timezone_id(current_user)
    d_from = parse_ymd_to_date(request.args.get('date_from'))
    d_to = parse_ymd_to_date(request.args.get('date_to'))
    today = local_today_date(tz_id)
    if d_from is None or d_to is None:
        d_to = today
        d_from = d_to - timedelta(days=29)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    date_from, date_to = _utc_bounds_local_dates(d_from, d_to, tz_id)

    hpp_mode = (request.args.get('hpp_mode') or 'snapshot').strip().lower()
    if hpp_mode not in ('master', 'snapshot'):
        hpp_mode = 'snapshot'
    hpp_unit_expr, _, _ = _sales_breakdown_hpp_unit_expr(hpp_mode)

    trx_q = Transaction.query.filter(
        Transaction.tenant_id == tenant_id,
        Transaction.status == 'selesai',
        Transaction.created_at.between(date_from, date_to),
    )
    branch_id_param = request.args.get('branch_id', '').strip()
    if current_user.role == 'kasir' and current_user.branch_id:
        trx_q = trx_q.filter(Transaction.branch_id == current_user.branch_id)
    elif branch_id_param:
        bid = _coerce_int(branch_id_param)
        if bid and Branch.query.filter_by(id=bid, tenant_id=tenant_id).first():
            trx_q = trx_q.filter(Transaction.branch_id == bid)

    trx_ids_q = trx_q.with_entities(Transaction.id)
    product_rows = (
        db.session.query(
            func.coalesce(TransactionItem.nama_produk, Product.nama, '-').label('nama_produk'),
            TransactionItem.product_id.label('produk_id'),
            func.coalesce(func.sum(TransactionItem.qty), 0).label('qty'),
            func.coalesce(func.sum(TransactionItem.subtotal), 0).label('omzet'),
            func.coalesce(func.sum(TransactionItem.qty * hpp_unit_expr), 0).label('hpp'),
        )
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .outerjoin(Product, TransactionItem.product_id == Product.id)
        .filter(Transaction.id.in_(trx_ids_q))
        .group_by(TransactionItem.product_id, TransactionItem.nama_produk, Product.nama)
        .order_by(func.coalesce(func.sum(TransactionItem.subtotal), 0).desc())
        .all()
    )
    returns_map = _sales_breakdown_returns_map(trx_ids_q)

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(['periode_dari', d_from.isoformat(), 'periode_sampai', d_to.isoformat()])
    w.writerow(['produk', 'qty', 'omzet', 'retur', 'net_sales', 'hpp', 'net_margin', 'net_margin_pct'])
    for r in product_rows:
        omzet = float(r.omzet or 0)
        hpp = float(r.hpp or 0)
        retur = float(returns_map.get(int(r.produk_id or 0), 0) or 0)
        net_sales = omzet - retur
        net_margin = net_sales - hpp
        pct = ((net_margin / net_sales) * 100.0) if net_sales > 0 else 0.0
        w.writerow([r.nama_produk, float(r.qty or 0), omzet, retur, net_sales, hpp, net_margin, round(pct, 4)])

    fn = f'laporan-produk-summary-{local_yyyymmdd_for_tenant_id(tenant_id)}-{datetime.utcnow().strftime("%H%M")}.csv'
    data = '\ufeff' + buf.getvalue()
    return Response(
        data,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={fn}'},
    )


@reports_bp.route('/laba-kotor-harian')
@login_required
def gross_profit_daily():
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id
    utc_start, utc_end, d_from, d_to, selected_branch_id = _gross_profit_filters(tenant_id)
    tz_id = resolve_effective_timezone_id(current_user)
    trx_q, ret_q, op_q = _gross_profit_common_queries(tenant_id, utc_start, utc_end, selected_branch_id)

    hpp_unit_expr, hpp_mode_used, hpp_mode_note = _sales_breakdown_hpp_unit_expr('snapshot')
    day_key_trx = _sql_calendar_date_from_utc_naive(Transaction.created_at, tz_id)
    day_key_ret = _sql_calendar_date_from_utc_naive(SalesReturn.created_at, tz_id)
    day_key_op = _sql_calendar_date_from_utc_naive(OperationalExpense.tanggal, tz_id)
    hpp_total_expr = func.sum(TransactionItem.qty * hpp_unit_expr)

    sales_daily_rows = (
        db.session.query(day_key_trx.label('d'), func.coalesce(func.sum(Transaction.total), 0).label('v'))
        .filter(Transaction.id.in_(trx_q.with_entities(Transaction.id)))
        .group_by(day_key_trx)
        .all()
    )
    returns_daily_rows = (
        db.session.query(day_key_ret.label('d'), func.coalesce(func.sum(SalesReturn.total_retur), 0).label('v'))
        .filter(SalesReturn.id.in_(ret_q.with_entities(SalesReturn.id)))
        .group_by(day_key_ret)
        .all()
    )
    hpp_daily_rows = (
        db.session.query(day_key_trx.label('d'), func.coalesce(hpp_total_expr, 0).label('v'))
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .outerjoin(Product, TransactionItem.product_id == Product.id)
        .filter(Transaction.id.in_(trx_q.with_entities(Transaction.id)))
        .group_by(day_key_trx)
        .all()
    )
    opex_daily_rows = (
        db.session.query(day_key_op.label('d'), func.coalesce(func.sum(OperationalExpense.jumlah), 0).label('v'))
        .filter(OperationalExpense.id.in_(op_q.with_entities(OperationalExpense.id)))
        .group_by(day_key_op)
        .all()
    )

    sales_map = {str(r.d): float(r.v or 0) for r in sales_daily_rows}
    return_map = {str(r.d): float(r.v or 0) for r in returns_daily_rows}
    hpp_map = {str(r.d): float(r.v or 0) for r in hpp_daily_rows}
    opex_map = {str(r.d): float(r.v or 0) for r in opex_daily_rows}
    daily_rows = _gross_profit_daily_rows(d_from, d_to, sales_map, return_map, hpp_map, opex_map)

    totals = {
        'sales': sum(r['sales'] for r in daily_rows),
        'returns': sum(r['returns'] for r in daily_rows),
        'net_sales': sum(r['net_sales'] for r in daily_rows),
        'hpp': sum(r['hpp'] for r in daily_rows),
        'gross_profit': sum(r['gross_profit'] for r in daily_rows),
        'opex': sum(r['opex'] for r in daily_rows),
        'operating_profit': sum(r['operating_profit'] for r in daily_rows),
    }
    totals['gross_margin_pct'] = (
        (totals['gross_profit'] / totals['net_sales']) * 100.0 if totals['net_sales'] > 0 else 0.0
    )
    totals['opex_ratio_pct'] = (totals['opex'] / totals['net_sales']) * 100.0 if totals['net_sales'] > 0 else 0.0

    chart_labels = [datetime.strptime(r['date'], '%Y-%m-%d').strftime('%d/%m') for r in daily_rows]
    chart_net_sales = [round(r['net_sales'], 2) for r in daily_rows]
    chart_hpp = [round(r['hpp'], 2) for r in daily_rows]
    chart_opex = [round(r['opex'], 2) for r in daily_rows]
    chart_operating = [round(r['operating_profit'], 2) for r in daily_rows]

    loss_days = [r for r in daily_rows if r['operating_profit'] < 0]
    thin_margin_days = [r for r in daily_rows if r['net_sales'] > 0 and r['gross_margin_pct'] < 10]
    heavy_opex_days = [r for r in daily_rows if r['net_sales'] > 0 and r['opex_ratio_pct'] > 30]
    alerts = {
        'loss_days_count': len(loss_days),
        'thin_margin_count': len(thin_margin_days),
        'heavy_opex_count': len(heavy_opex_days),
        'worst_operating_day': min(daily_rows, key=lambda x: x['operating_profit']) if daily_rows else None,
    }

    branch_sales_rows = (
        db.session.query(Transaction.branch_id, func.coalesce(func.sum(Transaction.total), 0).label('sales'))
        .filter(Transaction.id.in_(trx_q.with_entities(Transaction.id)))
        .group_by(Transaction.branch_id)
        .all()
    )
    branch_returns_rows = (
        db.session.query(SalesReturn.branch_id, func.coalesce(func.sum(SalesReturn.total_retur), 0).label('returns'))
        .filter(SalesReturn.id.in_(ret_q.with_entities(SalesReturn.id)))
        .group_by(SalesReturn.branch_id)
        .all()
    )
    branch_hpp_rows = (
        db.session.query(Transaction.branch_id, func.coalesce(hpp_total_expr, 0).label('hpp'))
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .outerjoin(Product, TransactionItem.product_id == Product.id)
        .filter(Transaction.id.in_(trx_q.with_entities(Transaction.id)))
        .group_by(Transaction.branch_id)
        .all()
    )
    branch_opex_rows = (
        db.session.query(OperationalExpense.branch_id, func.coalesce(func.sum(OperationalExpense.jumlah), 0).label('opex'))
        .filter(OperationalExpense.id.in_(op_q.with_entities(OperationalExpense.id)))
        .group_by(OperationalExpense.branch_id)
        .all()
    )
    branches = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Branch.nama).all()
    branch_name_map = {b.id: b.nama for b in branches}
    branch_map = {}
    for r in branch_sales_rows:
        bid = int(r.branch_id or 0)
        branch_map.setdefault(bid, {'branch_id': bid, 'nama': branch_name_map.get(bid, 'Tanpa cabang')})
        branch_map[bid]['sales'] = float(r.sales or 0)
    for r in branch_returns_rows:
        bid = int(r.branch_id or 0)
        branch_map.setdefault(bid, {'branch_id': bid, 'nama': branch_name_map.get(bid, 'Tanpa cabang')})
        branch_map[bid]['returns'] = float(r.returns or 0)
    for r in branch_hpp_rows:
        bid = int(r.branch_id or 0)
        branch_map.setdefault(bid, {'branch_id': bid, 'nama': branch_name_map.get(bid, 'Tanpa cabang')})
        branch_map[bid]['hpp'] = float(r.hpp or 0)
    for r in branch_opex_rows:
        bid = int(r.branch_id or 0)
        branch_map.setdefault(bid, {'branch_id': bid, 'nama': branch_name_map.get(bid, 'Tanpa cabang')})
        branch_map[bid]['opex'] = float(r.opex or 0)
    branch_rows = []
    for row in branch_map.values():
        sales = float(row.get('sales', 0) or 0)
        returns = float(row.get('returns', 0) or 0)
        hpp = float(row.get('hpp', 0) or 0)
        opex = float(row.get('opex', 0) or 0)
        row['sales'] = sales
        row['returns'] = returns
        row['hpp'] = hpp
        row['opex'] = opex
        net_sales = sales - returns
        gross_profit = net_sales - hpp
        operating_profit = gross_profit - opex
        row['net_sales'] = net_sales
        row['gross_profit'] = gross_profit
        row['operating_profit'] = operating_profit
        row['gross_margin_pct'] = ((gross_profit / net_sales) * 100.0) if net_sales > 0 else 0.0
        branch_rows.append(row)
    branch_rows.sort(key=lambda x: x['operating_profit'], reverse=True)

    top_product_rows = (
        db.session.query(
            func.coalesce(TransactionItem.nama_produk, Product.nama, '-').label('nama_produk'),
            func.coalesce(func.sum(TransactionItem.qty), 0).label('qty'),
            func.coalesce(func.sum(TransactionItem.subtotal), 0).label('sales'),
            func.coalesce(hpp_total_expr, 0).label('hpp'),
        )
        .join(Transaction, TransactionItem.transaction_id == Transaction.id)
        .outerjoin(Product, TransactionItem.product_id == Product.id)
        .filter(Transaction.id.in_(trx_q.with_entities(Transaction.id)))
        .group_by(TransactionItem.nama_produk, Product.nama)
        .order_by(func.coalesce(func.sum(TransactionItem.subtotal), 0).desc())
        .limit(10)
        .all()
    )
    top_products = []
    for r in top_product_rows:
        sales = float(r.sales or 0)
        hpp = float(r.hpp or 0)
        top_products.append(
            {
                'nama_produk': r.nama_produk or '-',
                'qty': float(r.qty or 0),
                'sales': sales,
                'hpp': hpp,
                'gross_profit': sales - hpp,
            }
        )

    today_d = local_today_date(tz_id)
    chip_kw = {}
    if selected_branch_id:
        chip_kw['branch_id'] = selected_branch_id
    chip_today = url_for(
        'reports.gross_profit_daily',
        **chip_kw,
        date_from=today_d.isoformat(),
        date_to=today_d.isoformat(),
    )
    chip_7_from = today_d - timedelta(days=6)
    chip_7 = url_for(
        'reports.gross_profit_daily',
        **chip_kw,
        date_from=chip_7_from.isoformat(),
        date_to=today_d.isoformat(),
    )
    chip_30_from = today_d - timedelta(days=29)
    chip_30 = url_for(
        'reports.gross_profit_daily',
        **chip_kw,
        date_from=chip_30_from.isoformat(),
        date_to=today_d.isoformat(),
    )
    is_preset_today = d_from == today_d and d_to == today_d
    is_preset_7 = d_from == chip_7_from and d_to == today_d
    is_preset_30 = d_from == chip_30_from and d_to == today_d

    return render_template(
        'reports/gross_profit_daily.html',
        date_from=d_from.isoformat(),
        date_to=d_to.isoformat(),
        selected_branch_id=selected_branch_id,
        branches=branches if current_user.role != 'kasir' else [],
        totals=totals,
        daily_rows=daily_rows,
        branch_rows=branch_rows,
        top_products=top_products,
        alerts=alerts,
        chart_labels=chart_labels,
        chart_net_sales=chart_net_sales,
        chart_hpp=chart_hpp,
        chart_opex=chart_opex,
        chart_operating=chart_operating,
        chip_today=chip_today,
        chip_7=chip_7,
        chip_30=chip_30,
        is_preset_today=is_preset_today,
        is_preset_7=is_preset_7,
        is_preset_30=is_preset_30,
        hpp_mode_used=hpp_mode_used,
        hpp_mode_note=hpp_mode_note,
    )


@reports_bp.route('/laba-kotor-harian/export.csv')
@login_required
def gross_profit_daily_export_csv():
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id
    utc_start, utc_end, d_from, d_to, selected_branch_id = _gross_profit_filters(tenant_id)
    tz_id = resolve_effective_timezone_id(current_user)
    trx_q, ret_q, op_q = _gross_profit_common_queries(tenant_id, utc_start, utc_end, selected_branch_id)
    hpp_unit_expr, _, _ = _sales_breakdown_hpp_unit_expr('snapshot')
    day_key_trx = _sql_calendar_date_from_utc_naive(Transaction.created_at, tz_id)
    day_key_ret = _sql_calendar_date_from_utc_naive(SalesReturn.created_at, tz_id)
    day_key_op = _sql_calendar_date_from_utc_naive(OperationalExpense.tanggal, tz_id)
    hpp_total_expr = func.sum(TransactionItem.qty * hpp_unit_expr)

    sales_map = {
        str(r.d): float(r.v or 0)
        for r in (
            db.session.query(day_key_trx.label('d'), func.coalesce(func.sum(Transaction.total), 0).label('v'))
            .filter(Transaction.id.in_(trx_q.with_entities(Transaction.id)))
            .group_by(day_key_trx)
            .all()
        )
    }
    return_map = {
        str(r.d): float(r.v or 0)
        for r in (
            db.session.query(day_key_ret.label('d'), func.coalesce(func.sum(SalesReturn.total_retur), 0).label('v'))
            .filter(SalesReturn.id.in_(ret_q.with_entities(SalesReturn.id)))
            .group_by(day_key_ret)
            .all()
        )
    }
    hpp_map = {
        str(r.d): float(r.v or 0)
        for r in (
            db.session.query(day_key_trx.label('d'), func.coalesce(hpp_total_expr, 0).label('v'))
            .join(Transaction, TransactionItem.transaction_id == Transaction.id)
            .outerjoin(Product, TransactionItem.product_id == Product.id)
            .filter(Transaction.id.in_(trx_q.with_entities(Transaction.id)))
            .group_by(day_key_trx)
            .all()
        )
    }
    opex_map = {
        str(r.d): float(r.v or 0)
        for r in (
            db.session.query(day_key_op.label('d'), func.coalesce(func.sum(OperationalExpense.jumlah), 0).label('v'))
            .filter(OperationalExpense.id.in_(op_q.with_entities(OperationalExpense.id)))
            .group_by(day_key_op)
            .all()
        )
    }
    daily_rows = _gross_profit_daily_rows(d_from, d_to, sales_map, return_map, hpp_map, opex_map)

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            'tanggal',
            'penjualan_kotor',
            'retur',
            'penjualan_bersih',
            'hpp',
            'laba_kotor',
            'biaya_operasional',
            'laba_operasional',
            'gross_margin_pct',
            'opex_ratio_pct',
        ]
    )
    for r in daily_rows:
        w.writerow(
            [
                r['date'],
                round(r['sales'], 2),
                round(r['returns'], 2),
                round(r['net_sales'], 2),
                round(r['hpp'], 2),
                round(r['gross_profit'], 2),
                round(r['opex'], 2),
                round(r['operating_profit'], 2),
                round(r['gross_margin_pct'], 4),
                round(r['opex_ratio_pct'], 4),
            ]
        )

    fn = f'laporan-laba-kotor-harian-{local_yyyymmdd_for_tenant_id(tenant_id)}-{datetime.utcnow().strftime("%H%M")}.csv'
    data = '\ufeff' + buf.getvalue()
    return Response(
        data,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={fn}'},
    )


@reports_bp.route('/pembelian')
@login_required
def pembelian():
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id
    tz_id = resolve_effective_timezone_id(current_user)
    d_from = parse_ymd_to_date(request.args.get('date_from'))
    d_to = parse_ymd_to_date(request.args.get('date_to'))
    today = local_today_date(tz_id)
    if d_from is None or d_to is None:
        d_to = today
        d_from = d_to - timedelta(days=29)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    date_from, date_to = _utc_bounds_local_dates(d_from, d_to, tz_id)

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
        date_from=d_from.isoformat(),
        date_to=d_to.isoformat(),
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
        from flask import flash, redirect
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id
    tz_id = resolve_effective_timezone_id(current_user)
    d_from = parse_ymd_to_date(request.args.get('date_from'))
    d_to = parse_ymd_to_date(request.args.get('date_to'))
    today = local_today_date(tz_id)
    if d_from is None or d_to is None:
        d_to = today
        d_from = d_to - timedelta(days=29)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    date_from, date_to = _utc_bounds_local_dates(d_from, d_to, tz_id)

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
            format_utc_naive_as_local(po.tanggal_pesan, tz_id, '%Y-%m-%d %H:%M:%S') if po.tanggal_pesan else '',
            format_utc_naive_as_local(po.tanggal_terima, tz_id, '%Y-%m-%d %H:%M:%S') if po.tanggal_terima else '',
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

    fn = f'laporan-pembelian-{local_yyyymmdd_for_tenant_id(tenant_id)}-{datetime.utcnow().strftime("%H%M")}.csv'
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
    layer_qty_cost = dict(
        db.session.query(
            InventoryCostLayer.product_id,
            func.coalesce(func.sum(InventoryCostLayer.qty_remaining * InventoryCostLayer.unit_cost), 0.0),
        )
        .filter(
            InventoryCostLayer.tenant_id == tenant_id,
            InventoryCostLayer.qty_remaining > 0,
        )
        .group_by(InventoryCostLayer.product_id)
        .all()
    )
    all_products = b.all()
    nilai_beli = 0.0
    for p in all_products:
        layer_val = float(layer_qty_cost.get(p.id, 0.0) or 0.0)
        if layer_val > 0:
            nilai_beli += layer_val
        else:
            nilai_beli += float(p.stok or 0) * float(p.harga_beli or 0)
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


def _fifo_layer_maps(tenant_id):
    rows = (
        db.session.query(
            InventoryCostLayer.product_id,
            func.coalesce(func.sum(InventoryCostLayer.qty_remaining), 0.0),
            func.coalesce(func.sum(InventoryCostLayer.qty_remaining * InventoryCostLayer.unit_cost), 0.0),
        )
        .filter(
            InventoryCostLayer.tenant_id == tenant_id,
            InventoryCostLayer.qty_remaining > 0,
        )
        .group_by(InventoryCostLayer.product_id)
        .all()
    )
    qty_map = {int(pid): float(sum_qty or 0) for pid, sum_qty, _ in rows}
    value_map = {int(pid): float(sum_val or 0) for pid, _, sum_val in rows}
    return qty_map, value_map


def _fifo_health_rows(tenant_id, q=''):
    products_q = _stok_product_base(tenant_id)
    if q:
        like = f'%{q}%'
        products_q = products_q.filter(or_(Product.nama.ilike(like), Product.barcode.ilike(like)))
    products = products_q.order_by(Product.nama).all()
    qty_map, value_map = _fifo_layer_maps(tenant_id)
    rows = []
    for p in products:
        stok_qty = float(p.stok or 0)
        layer_qty = float(qty_map.get(p.id, 0) or 0)
        layer_val = float(value_map.get(p.id, 0) or 0)
        if stok_qty > layer_qty + 1e-6:
            status = 'missing_layer'
        elif layer_qty > stok_qty + 1e-6:
            status = 'over_layer'
        else:
            status = 'ok'
        rows.append({
            'product': p,
            'stok_qty': stok_qty,
            'layer_qty': layer_qty,
            'gap_qty': stok_qty - layer_qty,
            'layer_value': layer_val,
            'status': status,
        })
    rows.sort(key=lambda r: (0 if r['status'] != 'ok' else 1, -abs(r['gap_qty']), r['product'].nama.lower()))
    return rows


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

    fifo_qty_map, fifo_value_map = _fifo_layer_maps(tenant_id)
    total_nilai_persediaan = 0.0
    fifo_mismatch_count = 0
    fifo_layer_missing_count = 0
    fifo_overlayer_count = 0
    for p in products:
        stok_qty = float(p.stok or 0)
        layer_qty = float(fifo_qty_map.get(p.id, 0) or 0)
        layer_val = float(fifo_value_map.get(p.id, 0) or 0)
        if layer_val > 0:
            total_nilai_persediaan += layer_val
        else:
            total_nilai_persediaan += stok_qty * float(p.harga_beli or 0)

        if stok_qty > 0 and layer_qty <= 1e-9:
            fifo_layer_missing_count += 1
        if abs(stok_qty - layer_qty) > 1e-6:
            fifo_mismatch_count += 1
            if layer_qty > stok_qty + 1e-6:
                fifo_overlayer_count += 1
    fifo_fallback_events = (
        ProductAuditLog.query.filter(
            ProductAuditLog.tenant_id == tenant_id,
            ProductAuditLog.action.in_((
                'fifo_fallback_cost',
                'fifo_stock_out_fallback',
                'fifo_return_fallback_layer',
            )),
        ).count()
    )
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
        layer_val = float(fifo_value_map.get(p.id, 0) or 0)
        row['nilai_beli'] += layer_val if layer_val > 0 else (st * float(p.harga_beli or 0))
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
        fifo_qty_map=fifo_qty_map,
        fifo_value_map=fifo_value_map,
        fifo_health={
            'mismatch_count': fifo_mismatch_count,
            'layer_missing_count': fifo_layer_missing_count,
            'overlayer_count': fifo_overlayer_count,
            'fallback_events': int(fifo_fallback_events or 0),
        },
    )


@reports_bp.route('/stok/export-produk.csv')
@login_required
def stok_export_produk():
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
    rows = pq.options(joinedload(Product.category), joinedload(Product.supplier)).all()
    _, fifo_value_map = _fifo_layer_maps(tenant_id)

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
        layer_val = float(fifo_value_map.get(p.id, 0) or 0)
        nilai_beli = layer_val if layer_val > 0 else (st * float(p.harga_beli or 0))
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

    fn = f'laporan-stok-produk-{local_yyyymmdd_for_tenant_id(tenant_id)}-{datetime.utcnow().strftime("%H%M")}.csv'
    data = '\ufeff' + buf.getvalue()
    return Response(
        data,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={fn}'},
    )


@reports_bp.route('/fifo-health')
@login_required
def fifo_health():
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id
    q = (request.args.get('q') or '').strip()
    show = (request.args.get('show') or 'issue').strip().lower()
    if show not in ('all', 'issue'):
        show = 'issue'
    rows = _fifo_health_rows(tenant_id, q=q)
    if show == 'issue':
        rows = [r for r in rows if r['status'] != 'ok']
    summary = {
        'total': len(rows),
        'missing': sum(1 for r in rows if r['status'] == 'missing_layer'),
        'over': sum(1 for r in rows if r['status'] == 'over_layer'),
        'ok': sum(1 for r in rows if r['status'] == 'ok'),
    }
    can_reconcile = current_user.role in ('admin',)
    return render_template('reports/fifo_health.html', rows=rows, q=q, show=show, summary=summary, can_reconcile=can_reconcile)


@reports_bp.route('/fifo-health/reconcile/<int:product_id>', methods=['POST'])
@login_required
def fifo_health_reconcile(product_id):
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect
        flash('Aksi hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))
    if current_user.role not in ('admin',):
        from flask import flash, redirect
        flash('Hanya admin yang bisa melakukan sinkronisasi FIFO.', 'danger')
        return redirect(url_for('reports.fifo_health'))

    tenant_id = current_user.tenant_id
    product = Product.query.filter_by(id=product_id, tenant_id=tenant_id, aktif=True).first_or_404()
    qty_map, _ = _fifo_layer_maps(tenant_id)
    stok_qty = float(product.stok or 0)
    layer_qty = float(qty_map.get(product.id, 0) or 0)
    gap = stok_qty - layer_qty

    from flask import flash, redirect
    if gap <= 1e-6:
        flash(f'Tidak ada kekurangan layer untuk {product.nama}.', 'info')
        return redirect(url_for('reports.fifo_health'))

    create_cost_layer(
        tenant_id=tenant_id,
        product_id=product.id,
        qty_in=gap,
        unit_cost=float(product.harga_beli or 0),
        source_type='fifo_reconcile',
        source_id=product.id,
    )
    db.session.add(ProductAuditLog(
        tenant_id=tenant_id,
        actor_user_id=current_user.id,
        product_id=product.id,
        action='fifo_reconcile_layer_gap',
        detail=f'Quick-fix FIFO health: tambah layer qty={gap:.4f} unit_cost={float(product.harga_beli or 0):.2f}',
    ))
    db.session.commit()
    flash(f'Layer FIFO untuk "{product.nama}" ditambah {gap:.2f}.', 'success')
    return redirect(url_for('reports.fifo_health'))


@reports_bp.route('/members-insight')
@login_required
def members_insight():
    if current_user.is_superadmin or not current_user.tenant_id:
        from flask import flash, redirect
        flash('Laporan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))

    tenant_id = current_user.tenant_id
    q = (request.args.get('q') or '').strip()
    base = Member.query.filter_by(tenant_id=tenant_id)
    if q:
        like = f'%{q}%'
        base = base.filter(or_(Member.nama.ilike(like), Member.telepon.ilike(like)))
    members = base.order_by(Member.total_belanja.desc()).limit(200).all()

    vouchers_by_member = dict(
        db.session.query(
            VoucherRedemption.member_id,
            func.count(VoucherRedemption.id),
        )
        .filter(VoucherRedemption.tenant_id == tenant_id)
        .group_by(VoucherRedemption.member_id)
        .all()
    )
    rows = []
    for m in members:
        rolling_tx = int(m.rolling_tx_count or 0)
        aov = (float(m.rolling_spend or 0) / rolling_tx) if rolling_tx else 0
        rows.append({
            'member': m,
            'aov': aov,
            'voucher_used': int(vouchers_by_member.get(m.id, 0) or 0),
        })

    return render_template('reports/members_insight.html', rows=rows, q=q)
