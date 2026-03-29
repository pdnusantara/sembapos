from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import desc
from sqlalchemy.orm import selectinload

from .. import db
from ..models import (
    SalesReturn,
    SalesReturnItem,
    Transaction,
    Branch,
    Product,
)
from ..sales_return_service import (
    process_sales_return,
    qty_already_returned,
    REFUND_METHODS,
)
from ..shifts_util import get_open_shift
from ..timezones import (
    local_today_date,
    parse_ymd_to_date,
    resolve_effective_timezone_id,
    utc_naive_bounds_for_local_date,
)

returns_bp = Blueprint('returns', __name__, url_prefix='/returns')


def _require_tenant():
    if current_user.is_superadmin or not current_user.tenant_id:
        flash('Retur penjualan hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))
    return None


def _can_access_source_transaction(trx):
    if trx.tenant_id != current_user.tenant_id:
        return False
    if current_user.role == 'kasir' and current_user.branch_id and trx.branch_id != current_user.branch_id:
        return False
    return True


@returns_bp.route('/')
@login_required
def index():
    redir = _require_tenant()
    if redir:
        return redir
    tenant_id = current_user.tenant_id

    tz_id = resolve_effective_timezone_id(current_user)
    today = local_today_date(tz_id)
    raw_from = parse_ymd_to_date(request.args.get('date_from'))
    raw_to = parse_ymd_to_date(request.args.get('date_to'))
    d_to = raw_to if raw_to is not None else today
    d_from = raw_from if raw_from is not None else d_to - timedelta(days=29)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    start_utc, _ = utc_naive_bounds_for_local_date(d_from, tz_id)
    _, end_utc = utc_naive_bounds_for_local_date(d_to, tz_id)

    q = SalesReturn.query.filter(
        SalesReturn.tenant_id == tenant_id,
        SalesReturn.created_at.between(start_utc, end_utc),
    ).options(
        selectinload(SalesReturn.source_transaction),
        selectinload(SalesReturn.user),
        selectinload(SalesReturn.branch),
    )

    if current_user.role == 'kasir' and current_user.branch_id:
        q = q.filter(SalesReturn.branch_id == current_user.branch_id)

    bid = request.args.get('branch_id', '').strip()
    if bid and current_user.role != 'kasir':
        try:
            i = int(bid)
            if Branch.query.filter_by(id=i, tenant_id=tenant_id).first():
                q = q.filter(SalesReturn.branch_id == i)
        except ValueError:
            pass

    nomor_q = (request.args.get('q') or '').strip()
    if nomor_q:
        like = f'%{nomor_q}%'
        q = q.filter(SalesReturn.nomor.ilike(like))

    rows = q.order_by(desc(SalesReturn.created_at)).limit(500).all()

    branches = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Branch.nama).all()

    return render_template(
        'returns/index.html',
        rows=rows,
        branches=branches,
        date_from=d_from.isoformat(),
        date_to=d_to.isoformat(),
        branch_id_param=bid,
        q=nomor_q,
    )


@returns_bp.route('/from/<int:tid>', methods=['GET', 'POST'])
@login_required
def create_from_transaction(tid):
    redir = _require_tenant()
    if redir:
        return redir

    tenant_id = current_user.tenant_id
    back_to = (request.args.get('back_to') or '').strip()
    return_form_url = (
        url_for('returns.create_from_transaction', tid=tid, back_to=back_to)
        if back_to
        else url_for('returns.create_from_transaction', tid=tid)
    )
    trx = (
        Transaction.query.options(selectinload(Transaction.items), selectinload(Transaction.member))
        .filter_by(id=tid, tenant_id=tenant_id)
        .first_or_404()
    )
    if not _can_access_source_transaction(trx):
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('transactions.index'))

    if trx.status != 'selesai':
        flash('Hanya transaksi selesai yang bisa diretur.', 'warning')
        return redirect(url_for('transactions.detail', id=tid))

    branch_id = trx.branch_id
    shift = get_open_shift(tenant_id, branch_id, current_user.id)

    items_meta = []
    for it in trx.items:
        ret = qty_already_returned(it.id)
        items_meta.append({
            'ti': it,
            'returned': ret,
            'available': max(0.0, float(it.qty) - ret),
        })

    products_tukar = [
        {
            'id': p.id,
            'nama': p.nama,
            'stok': float(p.stok or 0),
            'harga_jual': float(p.harga_jual or 0),
        }
        for p in Product.query.filter_by(tenant_id=tenant_id, aktif=True)
        .filter(Product.stok > 0)
        .order_by(Product.nama)
        .limit(400)
        .all()
    ]

    if request.method == 'POST':
        alasan = request.form.get('alasan', '')
        catatan = request.form.get('catatan', '')
        metode = request.form.get('metode_pengembalian', 'tunai')
        jenis = request.form.get('jenis', 'retur')
        if jenis == 'tukar' and not shift:
            flash('Buka shift kasir di halaman POS untuk memproses tukar barang.', 'warning')
            return redirect(return_form_url)

        line_inputs = []
        for it in trx.items:
            key = f'qty_{it.id}'
            raw = request.form.get(key, '').strip()
            if not raw:
                continue
            try:
                qv = float(raw)
            except ValueError:
                flash(f'Qty retur tidak valid untuk {it.nama_produk}.', 'danger')
                return redirect(return_form_url)
            if qv > 0:
                line_inputs.append({'transaction_item_id': it.id, 'qty': qv})

        replacement = None
        if jenis == 'tukar':
            repl_ids = request.form.getlist('repl_id')
            repl_qtys = request.form.getlist('repl_qty')
            items = []
            for pid_s, q_s in zip(repl_ids, repl_qtys):
                if not pid_s or not str(q_s).strip():
                    continue
                try:
                    items.append({'id': int(pid_s), 'qty': float(q_s)})
                except (ValueError, TypeError):
                    continue
            replacement = {
                'items': items,
                'metode_bayar': request.form.get('repl_metode', 'tunai'),
                'bayar': float(request.form.get('repl_bayar', 0) or 0),
                'diskon_manual': float(request.form.get('repl_diskon', 0) or 0),
                'catatan': request.form.get('repl_catatan', ''),
                'use_source_member': request.form.get('use_source_member') == '1',
                'debt_jatuh_tempo': request.form.get('repl_jatuh_tempo', ''),
            }

        try:
            sr, repl = process_sales_return(
                user=current_user,
                tenant_id=tenant_id,
                branch_id=branch_id,
                shift=shift,
                source=trx,
                line_inputs=line_inputs,
                alasan=alasan,
                catatan=catatan,
                metode_pengembalian=metode,
                jenis=jenis,
                replacement=replacement,
            )
            db.session.commit()
            flash(f'Retur {sr.nomor} berhasil disimpan.', 'success')
            return redirect(url_for('returns.detail', rid=sr.id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), 'danger')
        except Exception:
            db.session.rollback()
            flash('Gagal menyimpan retur. Coba lagi.', 'danger')

    return render_template(
        'returns/form.html',
        trx=trx,
        items_meta=items_meta,
        products_tukar=products_tukar,
        shift=shift,
        back_to=back_to,
        refund_methods=[x for x in ('tunai', 'transfer', 'qris', 'potong_hutang', 'tanpa_uang') if x in REFUND_METHODS],
    )


@returns_bp.route('/<int:rid>')
@login_required
def detail(rid):
    redir = _require_tenant()
    if redir:
        return redir
    tenant_id = current_user.tenant_id
    sr = (
        SalesReturn.query.options(
            selectinload(SalesReturn.items).selectinload(SalesReturnItem.source_line),
            selectinload(SalesReturn.source_transaction),
            selectinload(SalesReturn.replacement_transaction),
            selectinload(SalesReturn.user),
            selectinload(SalesReturn.branch),
        )
        .filter_by(id=rid, tenant_id=tenant_id)
        .first_or_404()
    )
    if current_user.role == 'kasir' and current_user.branch_id and sr.branch_id != current_user.branch_id:
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('returns.index'))

    return render_template('returns/detail.html', sr=sr)


@returns_bp.route('/<int:rid>/print')
@login_required
def print_return(rid):
    redir = _require_tenant()
    if redir:
        return redir
    tenant_id = current_user.tenant_id
    sr = (
        SalesReturn.query.options(
            selectinload(SalesReturn.items).selectinload(SalesReturnItem.source_line),
            selectinload(SalesReturn.source_transaction),
            selectinload(SalesReturn.replacement_transaction),
            selectinload(SalesReturn.branch),
            selectinload(SalesReturn.user),
        )
        .filter_by(id=rid, tenant_id=tenant_id)
        .first_or_404()
    )
    if current_user.role == 'kasir' and current_user.branch_id and sr.branch_id != current_user.branch_id:
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('returns.index'))

    return render_template('returns/print.html', sr=sr)
