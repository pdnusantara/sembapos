from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import func, desc

from .. import db
from ..models import CashierShift, Transaction, Branch, User
from ..shifts_util import effective_branch_id, get_open_shift

shifts_bp = Blueprint('shifts', __name__, url_prefix='/shifts')


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


def _require_tenant():
    if current_user.is_superadmin or not current_user.tenant_id:
        flash('Shift kasir hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))
    return None


def _is_tenant_admin():
    return current_user.role == 'admin'


def _can_view_shift(shift):
    if shift.tenant_id != current_user.tenant_id:
        return False
    if _is_tenant_admin():
        return True
    return shift.user_id == current_user.id


def _can_close_shift(shift):
    if shift.status != 'open':
        return False
    if shift.tenant_id != current_user.tenant_id:
        return False
    if _is_tenant_admin():
        return True
    return shift.user_id == current_user.id


def _cash_tunai_selesai_sum(shift_id):
    q = db.session.query(func.coalesce(func.sum(Transaction.total), 0)).filter(
        Transaction.shift_id == shift_id,
        Transaction.status == 'selesai',
        Transaction.metode_bayar == 'tunai',
    ).scalar()
    return float(q or 0)


def _breakdown_by_method(shift_id):
    rows = (
        db.session.query(
            Transaction.metode_bayar,
            func.count(Transaction.id),
            func.coalesce(func.sum(Transaction.total), 0),
        )
        .filter(
            Transaction.shift_id == shift_id,
            Transaction.status == 'selesai',
        )
        .group_by(Transaction.metode_bayar)
        .all()
    )
    out = {}
    for metode, cnt, total in rows:
        key = metode or 'lain'
        out[key] = {'count': int(cnt or 0), 'sum': float(total or 0)}
    return out


def _shift_payload(shift, include_preview=True):
    branch = shift.branch
    user = shift.user
    by_method = _breakdown_by_method(shift.id)
    extra = float(_cash_tunai_selesai_sum(shift.id))
    expected = float(shift.opening_float or 0) + extra
    payload = {
        'id': shift.id,
        'status': shift.status,
        'opened_at': shift.opened_at.isoformat() + 'Z' if shift.opened_at else None,
        'closed_at': shift.closed_at.isoformat() + 'Z' if shift.closed_at else None,
        'opening_float': float(shift.opening_float or 0),
        'branch_id': shift.branch_id,
        'branch_nama': branch.nama if branch else '',
        'user_id': shift.user_id,
        'kasir_nama': user.nama if user else '',
        'by_method': by_method,
        'cash_sales_total': extra,
    }
    if include_preview:
        payload['expected_cash'] = expected
    if shift.status == 'closed':
        payload['closing_counted'] = float(shift.closing_counted) if shift.closing_counted is not None else None
        payload['expected_cash_stored'] = float(shift.expected_cash) if shift.expected_cash is not None else None
        payload['variance'] = float(shift.variance) if shift.variance is not None else None
    return payload


def _perform_close(shift, closing_counted, note_close):
    if shift.status != 'open':
        return False, 'Shift sudah ditutup.'
    expected = float(shift.opening_float or 0) + _cash_tunai_selesai_sum(shift.id)
    counted = float(closing_counted)
    shift.expected_cash = expected
    shift.closing_counted = counted
    shift.variance = counted - expected
    shift.closed_at = datetime.utcnow()
    shift.status = 'closed'
    if note_close is not None:
        shift.note_close = (note_close or '').strip() or None
    return True, None


@shifts_bp.route('/api/status', methods=['GET'])
@login_required
def api_status():
    if current_user.is_superadmin or not current_user.tenant_id:
        return jsonify({'open': False, 'shift': None, 'branch_id': None}), 200
    tenant_id = current_user.tenant_id
    branch_id = effective_branch_id(tenant_id, current_user)
    shift = get_open_shift(tenant_id, branch_id, current_user.id)
    if not shift:
        return jsonify({'open': False, 'shift': None, 'branch_id': branch_id}), 200
    return jsonify({
        'open': True,
        'shift': _shift_payload(shift),
        'branch_id': branch_id,
    }), 200


@shifts_bp.route('/api/open', methods=['POST'])
@login_required
def api_open():
    if current_user.is_superadmin or not current_user.tenant_id:
        return jsonify({'success': False, 'message': 'Tidak tersedia untuk akun ini.'}), 403
    tenant_id = current_user.tenant_id
    branch_id = effective_branch_id(tenant_id, current_user)
    if not branch_id:
        return jsonify({'success': False, 'message': 'Cabang tidak ditemukan.'}), 400

    data = request.get_json(silent=True) or {}
    try:
        opening_float = float(data.get('opening_float', 0))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Saldo awal tidak valid.'}), 400
    if opening_float < 0:
        return jsonify({'success': False, 'message': 'Saldo awal tidak boleh negatif.'}), 400
    note_open = (data.get('note_open') or '').strip() or None

    existing = get_open_shift(tenant_id, branch_id, current_user.id)
    if existing:
        return jsonify({'success': False, 'message': 'Anda sudah memiliki shift yang terbuka.'}), 400

    dup = CashierShift.query.filter_by(
        tenant_id=tenant_id,
        branch_id=branch_id,
        user_id=current_user.id,
        status='open',
    ).first()
    if dup:
        return jsonify({'success': False, 'message': 'Shift terbuka sudah ada.'}), 400

    shift = CashierShift(
        tenant_id=tenant_id,
        branch_id=branch_id,
        user_id=current_user.id,
        opened_at=datetime.utcnow(),
        opening_float=opening_float,
        status='open',
        note_open=note_open,
    )
    db.session.add(shift)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Gagal membuka shift. Coba lagi.'}), 500

    return jsonify({'success': True, 'shift': _shift_payload(shift)}), 200


@shifts_bp.route('/api/close', methods=['POST'])
@login_required
def api_close():
    if current_user.is_superadmin or not current_user.tenant_id:
        return jsonify({'success': False, 'message': 'Tidak tersedia untuk akun ini.'}), 403
    tenant_id = current_user.tenant_id
    branch_id = effective_branch_id(tenant_id, current_user)
    shift = get_open_shift(tenant_id, branch_id, current_user.id)
    if not shift:
        return jsonify({'success': False, 'message': 'Tidak ada shift terbuka.'}), 400

    data = request.get_json(silent=True) or {}
    try:
        closing_counted = float(data.get('closing_counted'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Jumlah uang fisik tidak valid.'}), 400
    if closing_counted < 0:
        return jsonify({'success': False, 'message': 'Jumlah uang fisik tidak boleh negatif.'}), 400
    note_close = (data.get('note_close') or '').strip() or None

    ok, err = _perform_close(shift, closing_counted, note_close)
    if not ok:
        return jsonify({'success': False, 'message': err}), 400
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Gagal menutup shift.'}), 500

    return jsonify({'success': True, 'shift': _shift_payload(shift, include_preview=False)}), 200


@shifts_bp.route('/')
@login_required
def index():
    redir = _require_tenant()
    if redir:
        return redir
    tenant_id = current_user.tenant_id

    date_from = _parse_date(request.args.get('date_from'), end_of_day=False)
    date_to = _parse_date(request.args.get('date_to'), end_of_day=True)
    if not date_from or not date_to:
        date_from, date_to = _default_range_days(30)

    branch_id_param = request.args.get('branch_id', '').strip()
    user_id_param = request.args.get('user_id', '').strip()

    q = CashierShift.query.filter(
        CashierShift.tenant_id == tenant_id,
        CashierShift.opened_at >= date_from,
        CashierShift.opened_at <= date_to,
    )

    if current_user.role == 'kasir':
        q = q.filter(CashierShift.user_id == current_user.id)
    else:
        if branch_id_param:
            try:
                bid = int(branch_id_param)
                if Branch.query.filter_by(id=bid, tenant_id=tenant_id).first():
                    q = q.filter(CashierShift.branch_id == bid)
            except (ValueError, TypeError):
                pass
        if user_id_param:
            try:
                uid = int(user_id_param)
                u = User.query.filter_by(id=uid, tenant_id=tenant_id).first()
                if u:
                    q = q.filter(CashierShift.user_id == uid)
            except (ValueError, TypeError):
                pass

    shifts = q.order_by(desc(CashierShift.opened_at)).limit(500).all()

    branches = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Branch.nama).all()
    users = []
    if current_user.role != 'kasir':
        users = User.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(User.nama).all()

    return render_template(
        'shifts/index.html',
        shifts=shifts,
        branches=branches,
        users=users,
        date_from=date_from.strftime('%Y-%m-%d'),
        date_to=date_to.strftime('%Y-%m-%d'),
        branch_id_param=branch_id_param,
        user_id_param=user_id_param,
    )


@shifts_bp.route('/<int:sid>')
@login_required
def detail(sid):
    redir = _require_tenant()
    if redir:
        return redir
    shift = CashierShift.query.get_or_404(sid)
    if not _can_view_shift(shift):
        flash('Akses ditolak.', 'warning')
        return redirect(url_for('shifts.index'))

    transactions = (
        Transaction.query.filter_by(shift_id=shift.id)
        .order_by(desc(Transaction.created_at))
        .limit(2000)
        .all()
    )
    by_method = _breakdown_by_method(shift.id)
    cash_extra = _cash_tunai_selesai_sum(shift.id)
    expected_live = float(shift.opening_float or 0) + cash_extra

    return render_template(
        'shifts/detail.html',
        shift=shift,
        transactions=transactions,
        by_method=by_method,
        expected_live=expected_live,
        cash_extra=cash_extra,
        can_close=_can_close_shift(shift),
    )


@shifts_bp.route('/<int:sid>/close', methods=['POST'])
@login_required
def close_shift_form(sid):
    redir = _require_tenant()
    if redir:
        return redir
    shift = CashierShift.query.get_or_404(sid)
    if not _can_close_shift(shift):
        flash('Tidak bisa menutup shift ini.', 'warning')
        return redirect(url_for('shifts.detail', sid=sid))

    try:
        closing_counted = float(request.form.get('closing_counted', ''))
    except (TypeError, ValueError):
        flash('Jumlah uang fisik tidak valid.', 'danger')
        return redirect(url_for('shifts.detail', sid=sid))
    if closing_counted < 0:
        flash('Jumlah uang fisik tidak boleh negatif.', 'danger')
        return redirect(url_for('shifts.detail', sid=sid))

    note_close = (request.form.get('note_close') or '').strip() or None
    ok, err = _perform_close(shift, closing_counted, note_close)
    if not ok:
        flash(err, 'danger')
        return redirect(url_for('shifts.detail', sid=sid))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('Gagal menutup shift.', 'danger')
        return redirect(url_for('shifts.detail', sid=sid))

    flash('Shift berhasil ditutup.', 'success')
    return redirect(url_for('shifts.detail', sid=sid))
