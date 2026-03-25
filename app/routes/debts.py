import csv
import io
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    Response,
)
from flask_login import login_required, current_user
from sqlalchemy import or_, func

from .. import db
from ..models import Debt, DebtPayment, Member, Transaction, Branch

debts_bp = Blueprint('debts', __name__, url_prefix='/debts')

PER_PAGE = 20


def tenant_debts_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if current_user.tenant_id is None:
            flash('Hutang piutang per tenant. Gunakan panel Super Admin untuk data global.', 'info')
            return redirect(url_for('superadmin.index'))
        return f(*args, **kwargs)
    return decorated


def debts_admin_only(f):
    @wraps(f)
    @login_required
    @tenant_debts_required
    def decorated(*args, **kwargs):
        if current_user.role not in ('superadmin', 'admin'):
            flash('Hanya admin yang dapat mengakses fitur ini.', 'danger')
            return redirect(url_for('debts.index'))
        return f(*args, **kwargs)
    return decorated


def _unique_manual_nomor():
    for _ in range(20):
        n = f'MNL-{int(time.time())}-{secrets.token_hex(3).upper()}'
        if not Transaction.query.filter_by(nomor=n).first():
            return n
    return f'MNL-{secrets.token_hex(8).upper()}'


def _parse_date_start(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), '%Y-%m-%d')
    except ValueError:
        return None


def _parse_date_end(s):
    if not s:
        return None
    try:
        d = datetime.strptime(s.strip(), '%Y-%m-%d')
        return d + timedelta(days=1)
    except ValueError:
        return None


def _parse_jatuh_tempo(s):
    if not s or not str(s).strip():
        return None
    try:
        return datetime.strptime(str(s).strip()[:10], '%Y-%m-%d')
    except ValueError:
        return None


def _build_debts_query(tenant_id):
    q_text = (request.args.get('q') or '').strip()
    status_filter = (request.args.get('status') or 'belum_lunas').strip()
    date_from = _parse_date_start(request.args.get('date_from', ''))
    date_to_excl = _parse_date_end(request.args.get('date_to', ''))
    overdue_only = request.args.get('overdue', '').strip() == '1'
    sort = (request.args.get('sort') or 'created_desc').strip()

    q = (
        Debt.query.filter(Debt.tenant_id == tenant_id)
        .join(Member, Debt.member_id == Member.id)
        .outerjoin(Transaction, Debt.transaction_id == Transaction.id)
    )

    if status_filter != 'all':
        q = q.filter(Debt.status == status_filter)

    if q_text:
        like = f'%{q_text}%'
        q = q.filter(
            or_(
                Member.nama.ilike(like),
                Member.telepon.ilike(like),
                Transaction.nomor.ilike(like),
                Debt.keterangan.ilike(like),
            )
        )

    if date_from:
        q = q.filter(Debt.created_at >= date_from)
    if date_to_excl:
        q = q.filter(Debt.created_at < date_to_excl)

    if overdue_only and status_filter == 'belum_lunas':
        now = datetime.utcnow()
        q = q.filter(Debt.jatuh_tempo.isnot(None), Debt.jatuh_tempo < now)

    if sort == 'sisa_desc':
        q = q.order_by(Debt.sisa.desc(), Debt.created_at.desc())
    elif sort == 'jatuh_tempo_asc':
        q = q.order_by(Debt.jatuh_tempo.is_(None), Debt.jatuh_tempo.asc(), Debt.created_at.desc())
    else:
        q = q.order_by(Debt.created_at.desc())

    return q


def _member_sisa_summary(tenant_id, limit=20):
    rows = (
        db.session.query(
            Member.id,
            Member.nama,
            func.coalesce(func.sum(Debt.sisa), 0).label('total_sisa'),
        )
        .join(Debt, Debt.member_id == Member.id)
        .filter(
            Member.tenant_id == tenant_id,
            Debt.status == 'belum_lunas',
        )
        .group_by(Member.id, Member.nama)
        .having(func.coalesce(func.sum(Debt.sisa), 0) > 0)
        .order_by(func.sum(Debt.sisa).desc())
        .limit(limit)
        .all()
    )
    return rows


@debts_bp.route('/')
@tenant_debts_required
def index():
    tenant_id = current_user.tenant_id
    q = _build_debts_query(tenant_id)

    total_piutang = (
        db.session.query(func.coalesce(func.sum(Debt.sisa), 0))
        .filter(Debt.tenant_id == tenant_id, Debt.status == 'belum_lunas')
        .scalar()
        or 0
    )

    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    total_count = q.count()
    total_pages = max(1, (total_count + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages)

    debts = q.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()

    member_summary = _member_sisa_summary(tenant_id)
    members_for_manual = (
        Member.query.filter_by(tenant_id=tenant_id, aktif=True)
        .order_by(Member.nama)
        .all()
        if current_user.role in ('superadmin', 'admin')
        else []
    )

    now = datetime.utcnow()

    return render_template(
        'debts/index.html',
        debts=debts,
        status=request.args.get('status', 'belum_lunas'),
        total_piutang=total_piutang,
        q=request.args.get('q', ''),
        date_from=request.args.get('date_from', ''),
        date_to=request.args.get('date_to', ''),
        overdue=request.args.get('overdue', ''),
        sort=request.args.get('sort', 'created_desc'),
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        per_page=PER_PAGE,
        member_summary=member_summary,
        members_for_manual=members_for_manual,
        now=now,
    )


@debts_bp.route('/export.csv')
@login_required
@debts_admin_only
def export_csv():
    tenant_id = current_user.tenant_id
    q = _build_debts_query(tenant_id)
    rows = q.all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            'id',
            'member_nama',
            'member_telepon',
            'nomor_transaksi',
            'tanggal_hutang',
            'jumlah_awal',
            'sisa',
            'status',
            'jatuh_tempo',
            'keterangan',
        ]
    )
    for d in rows:
        nomor = d.transaction.nomor if d.transaction else ''
        w.writerow(
            [
                d.id,
                d.member.nama,
                d.member.telepon,
                nomor,
                d.created_at.strftime('%Y-%m-%d %H:%M') if d.created_at else '',
                int(round(d.jumlah)),
                int(round(d.sisa)),
                d.status,
                d.jatuh_tempo.strftime('%Y-%m-%d') if d.jatuh_tempo else '',
                (d.keterangan or '').replace('\n', ' '),
            ]
        )

    out = buf.getvalue()
    buf.close()
    fn = f'hutang-{datetime.utcnow().strftime("%Y%m%d-%H%M")}.csv'
    return Response(
        out.encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={fn}'},
    )


@debts_bp.route('/manual', methods=['POST'])
@login_required
@debts_admin_only
def manual_add():
    tenant_id = current_user.tenant_id
    mid = request.form.get('member_id')
    try:
        member_id = int(mid)
    except (TypeError, ValueError):
        flash('Pilih member.', 'danger')
        return redirect(url_for('debts.index'))

    member = Member.query.filter_by(id=member_id, tenant_id=tenant_id, aktif=True).first()
    if not member:
        flash('Member tidak valid.', 'danger')
        return redirect(url_for('debts.index'))

    try:
        jumlah = round(float(request.form.get('jumlah', 0) or 0))
    except (TypeError, ValueError):
        jumlah = 0
    if jumlah <= 0:
        flash('Jumlah hutang harus lebih dari 0.', 'danger')
        return redirect(url_for('debts.index'))

    keterangan = (request.form.get('keterangan') or '').strip() or 'Piutang manual'
    jt = _parse_jatuh_tempo(request.form.get('jatuh_tempo', ''))

    branch_id = current_user.branch_id
    if not branch_id:
        br = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).first()
        branch_id = br.id if br else None
    if not branch_id:
        flash('Tidak ada cabang aktif untuk mencatat transaksi pembukuan.', 'danger')
        return redirect(url_for('debts.index'))

    nomor = _unique_manual_nomor()
    trx = Transaction(
        tenant_id=tenant_id,
        branch_id=branch_id,
        user_id=current_user.id,
        member_id=member.id,
        nomor=nomor,
        subtotal=0,
        diskon=0,
        total=jumlah,
        bayar=0,
        kembalian=0,
        metode_bayar='manual_hutang',
        catatan=keterangan[:500] if keterangan else 'Piutang manual',
        status='batal',
    )
    db.session.add(trx)
    db.session.flush()

    debt = Debt(
        tenant_id=tenant_id,
        member_id=member.id,
        transaction_id=trx.id,
        jumlah=float(jumlah),
        sisa=float(jumlah),
        keterangan=keterangan[:255],
        jatuh_tempo=jt,
        status='belum_lunas',
    )
    db.session.add(debt)
    member.total_hutang = float(member.total_hutang or 0) + float(jumlah)

    db.session.commit()
    flash(f'Piutang manual Rp {jumlah:,.0f} untuk {member.nama} tercatat.', 'success')
    return redirect(url_for('debts.detail', id=debt.id))


@debts_bp.route('/<int:id>')
@tenant_debts_required
def detail(id):
    tenant_id = current_user.tenant_id
    debt = Debt.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    now = datetime.utcnow()
    return render_template('debts/detail.html', debt=debt, now=now)


@debts_bp.route('/member/<int:member_id>/history')
@tenant_debts_required
def member_history(member_id):
    tenant_id = current_user.tenant_id
    member = Member.query.filter_by(id=member_id, tenant_id=tenant_id).first_or_404()

    debts = (
        Debt.query.filter_by(tenant_id=tenant_id, member_id=member_id)
        .order_by(Debt.created_at.desc())
        .all()
    )
    payments = (
        DebtPayment.query.join(Debt, DebtPayment.debt_id == Debt.id)
        .filter(
            DebtPayment.tenant_id == tenant_id,
            Debt.member_id == member_id,
        )
        .order_by(DebtPayment.created_at.desc())
        .all()
    )

    total_awal = sum(float(d.jumlah or 0) for d in debts)
    total_sisa = sum(float(d.sisa or 0) for d in debts if d.status == 'belum_lunas')
    total_bayar = sum(float(p.jumlah or 0) for p in payments)

    return render_template(
        'debts/member_history.html',
        member=member,
        debts=debts,
        payments=payments,
        total_awal=total_awal,
        total_sisa=total_sisa,
        total_bayar=total_bayar,
        now=datetime.utcnow(),
    )


@debts_bp.route('/<int:id>/pay', methods=['POST'])
@tenant_debts_required
def pay(id):
    tenant_id = current_user.tenant_id
    debt = Debt.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()

    if debt.status == 'lunas':
        flash('Hutang ini sudah lunas.', 'warning')
        return redirect(url_for('debts.detail', id=debt.id))

    try:
        jumlah_bayar = round(float(request.form.get('jumlah', 0) or 0))
    except (TypeError, ValueError):
        flash('Jumlah pembayaran tidak valid.', 'danger')
        return redirect(url_for('debts.detail', id=debt.id))

    if jumlah_bayar <= 0:
        flash('Jumlah pembayaran harus lebih dari 0.', 'danger')
        return redirect(url_for('debts.detail', id=debt.id))

    sisa_int = int(round(debt.sisa))
    if jumlah_bayar > sisa_int:
        jumlah_bayar = sisa_int

    metode = (request.form.get('metode_bayar') or 'tunai').strip()
    if metode not in ('tunai', 'transfer', 'qris'):
        metode = 'tunai'

    catatan = (request.form.get('catatan') or '').strip()

    payment = DebtPayment(
        tenant_id=tenant_id,
        debt_id=debt.id,
        user_id=current_user.id,
        jumlah=float(jumlah_bayar),
        metode_bayar=metode,
        catatan=catatan or None,
    )
    db.session.add(payment)

    debt.sisa = float(debt.sisa) - float(jumlah_bayar)
    if debt.sisa <= 0:
        debt.sisa = 0
        debt.status = 'lunas'

    member = Member.query.get(debt.member_id)
    if member:
        member.total_hutang = float(member.total_hutang or 0) - float(jumlah_bayar)
        if member.total_hutang < 0:
            member.total_hutang = 0

    db.session.commit()
    flash(f'Pembayaran Rp {jumlah_bayar:,.0f} berhasil dicatat.', 'success')
    return redirect(url_for('debts.detail', id=debt.id))
