from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)
from flask_login import login_required, current_user
from sqlalchemy import func, or_, exists
from sqlalchemy.orm import selectinload

from .. import db
from ..models import Transaction, Branch, User, Member, SalesReturn
from ..timezones import (
    local_today_date,
    parse_ymd_to_date,
    resolve_effective_timezone_id,
    utc_naive_bounds_for_local_date,
)

transactions_bp = Blueprint('transactions', __name__, url_prefix='/transactions')
PER_PAGE = 25


def _require_tenant():
    if current_user.is_superadmin or not current_user.tenant_id:
        flash('Riwayat transaksi hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))
    return None


def _filtered_query():
    """Query dasar + semua filter GET (tanpa order/limit)."""
    tenant_id = current_user.tenant_id
    q = (
        Transaction.query.options(
            selectinload(Transaction.user),
            selectinload(Transaction.branch),
            selectinload(Transaction.member),
            selectinload(Transaction.payments),
            selectinload(Transaction.sales_returns),
        )
        .filter(Transaction.tenant_id == tenant_id)
    )

    if current_user.role == 'kasir' and current_user.branch_id:
        q = q.filter(Transaction.branch_id == current_user.branch_id)

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
    q = q.filter(Transaction.created_at.between(start_utc, end_utc))

    bid = request.args.get('branch_id', '').strip()
    if bid and current_user.role != 'kasir':
        if bid == 'none':
            q = q.filter(Transaction.branch_id.is_(None))
        else:
            try:
                i = int(bid)
                if Branch.query.filter_by(id=i, tenant_id=tenant_id).first():
                    q = q.filter(Transaction.branch_id == i)
            except ValueError:
                pass

    st = request.args.get('status', '').strip()
    if st in ('selesai', 'batal'):
        q = q.filter(Transaction.status == st)

    met = request.args.get('metode', '').strip().lower()
    if met in ('tunai', 'transfer', 'qris', 'kredit', 'mixed'):
        q = q.filter(Transaction.metode_bayar == met)

    uid = request.args.get('user_id', '').strip()
    if uid and current_user.role != 'kasir':
        try:
            ui = int(uid)
            if User.query.filter_by(id=ui, tenant_id=tenant_id).first():
                q = q.filter(Transaction.user_id == ui)
        except ValueError:
            pass

    mid = request.args.get('member_id', '').strip()
    if mid:
        try:
            mi = int(mid)
            if Member.query.filter_by(id=mi, tenant_id=tenant_id).first():
                q = q.filter(Transaction.member_id == mi)
        except ValueError:
            pass

    only_member = request.args.get('only_member')
    if only_member in ('1', 'true', 'yes'):
        q = q.filter(Transaction.member_id.isnot(None))

    qstr = (request.args.get('q') or '').strip()
    if qstr:
        like = f'%{qstr}%'
        q = q.filter(or_(Transaction.nomor.ilike(like), Transaction.catatan.ilike(like)))

    try:
        tmin = float(request.args.get('min_total', '') or '')
        q = q.filter(Transaction.total >= tmin)
    except ValueError:
        pass
    try:
        tmax = float(request.args.get('max_total', '') or '')
        q = q.filter(Transaction.total <= tmax)
    except ValueError:
        pass

    member_q = (request.args.get('member_q') or '').strip()
    if member_q:
        like = f'%{member_q}%'
        mids = [
            r[0]
            for r in db.session.query(Member.id)
            .filter(Member.tenant_id == tenant_id, Member.nama.ilike(like))
            .limit(200)
            .all()
        ]
        if mids:
            q = q.filter(Transaction.member_id.in_(mids))
        else:
            q = q.filter(Transaction.id == -1)

    retur_f = (request.args.get('retur') or '').strip().lower()
    if retur_f == 'ada':
        q = q.filter(
            exists().where(SalesReturn.source_transaction_id == Transaction.id)
        )
    elif retur_f == 'tidak':
        q = q.filter(
            ~exists().where(SalesReturn.source_transaction_id == Transaction.id)
        )

    return q, d_from, d_to


def _order_clause():
    sort = request.args.get('sort', 'created_desc')
    if sort == 'created_asc':
        return Transaction.created_at.asc()
    if sort == 'total_desc':
        return Transaction.total.desc()
    if sort == 'total_asc':
        return Transaction.total.asc()
    if sort == 'nomor':
        return Transaction.nomor.asc()
    return Transaction.created_at.desc()


@transactions_bp.route('/')
@login_required
def index():
    redir = _require_tenant()
    if redir:
        return redir

    q, d_from, d_to = _filtered_query()
    total_count = q.count()
    q_sum, _, _ = _filtered_query()
    sum_total = float(q_sum.with_entities(func.coalesce(func.sum(Transaction.total), 0)).scalar() or 0)

    tenant_id = current_user.tenant_id
    if total_count == 0:
        sum_returns = 0.0
    else:
        q_ids, _, _ = _filtered_query()
        id_subq = q_ids.with_entities(Transaction.id).subquery()
        sum_returns = float(
            db.session.query(func.coalesce(func.sum(SalesReturn.total_retur), 0.0))
            .filter(
                SalesReturn.tenant_id == tenant_id,
                SalesReturn.source_transaction_id.in_(id_subq),
            )
            .scalar()
            or 0
        )
    sum_net = max(0.0, sum_total - sum_returns)

    page = max(1, int(request.args.get('page', 1) or 1))
    ordered = q.order_by(_order_clause())
    transactions = ordered.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()
    total_pages = max(1, (total_count + PER_PAGE - 1) // PER_PAGE)
    if page > total_pages:
        page = total_pages
        transactions = q.order_by(_order_clause()).offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()

    qs_base = urlencode([(k, v) for k, v in request.args.items(multi=True) if k != 'page'])

    tz_id = resolve_effective_timezone_id(current_user)
    d = local_today_date(tz_id)
    presets = SimpleNamespace(
        today_from=d.isoformat(),
        today_to=d.isoformat(),
        d7_from=(d - timedelta(days=6)).isoformat(),
        d7_to=d.isoformat(),
        d30_from=(d - timedelta(days=29)).isoformat(),
        d30_to=d.isoformat(),
    )

    branches = Branch.query.filter_by(tenant_id=current_user.tenant_id, aktif=True).order_by(Branch.nama).all()

    preserved = {}
    if current_user.role != 'kasir':
        bv = (request.args.get('branch_id') or '').strip()
        if bv:
            preserved['branch_id'] = bv
    sv = (request.args.get('status') or '').strip()
    if sv:
        preserved['status'] = sv
    qv = (request.args.get('q') or '').strip()
    if qv:
        preserved['q'] = qv
    rv = (request.args.get('retur') or '').strip()
    if rv:
        preserved['retur'] = rv

    chip_today = url_for(
        'transactions.index',
        date_from=presets.today_from,
        date_to=presets.today_to,
        **preserved,
    )
    chip_7 = url_for(
        'transactions.index',
        date_from=presets.d7_from,
        date_to=presets.d7_to,
        **preserved,
    )
    chip_30 = url_for(
        'transactions.index',
        date_from=presets.d30_from,
        date_to=presets.d30_to,
        **preserved,
    )

    avg_nota_net = (sum_net / total_count) if total_count else 0.0

    return render_template(
        'transactions/index.html',
        transactions=transactions,
        total_count=total_count,
        sum_total=sum_total,
        sum_returns=sum_returns,
        sum_net=sum_net,
        avg_nota_net=avg_nota_net,
        page=page,
        total_pages=total_pages,
        branches=branches,
        date_from=d_from.isoformat(),
        date_to=d_to.isoformat(),
        filter_branch=request.args.get('branch_id', ''),
        filter_status=request.args.get('status', ''),
        filter_retur=request.args.get('retur', ''),
        q=request.args.get('q', ''),
        qs_base=qs_base,
        presets=presets,
        chip_today=chip_today,
        chip_7=chip_7,
        chip_30=chip_30,
        is_preset_today=(d_from == d and d_to == d),
        is_preset_7=(d_from == (d - timedelta(days=6)) and d_to == d),
        is_preset_30=(d_from == (d - timedelta(days=29)) and d_to == d),
    )


@transactions_bp.route('/<int:id>')
@login_required
def detail(id):
    redir = _require_tenant()
    if redir:
        return redir

    tenant_id = current_user.tenant_id
    trx = (
        Transaction.query.options(
            selectinload(Transaction.user),
            selectinload(Transaction.branch),
            selectinload(Transaction.member),
            selectinload(Transaction.items),
            selectinload(Transaction.payments),
            selectinload(Transaction.sales_returns),
        )
        .filter_by(id=id, tenant_id=tenant_id)
        .first_or_404()
    )
    if current_user.role == 'kasir' and current_user.branch_id and trx.branch_id != current_user.branch_id:
        flash('Transaksi tidak ditemukan.', 'danger')
        return redirect(url_for('transactions.index'))

    returns_list = sorted(
        list(trx.sales_returns),
        key=lambda r: r.created_at or datetime(1970, 1, 1),
        reverse=True,
    )

    return render_template('transactions/detail.html', trx=trx, returns_list=returns_list)
