import csv
import json
import re
import secrets
import string
from io import StringIO
from functools import wraps
from datetime import datetime, timedelta, time

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    Response,
)
from flask_login import login_required, current_user, login_user
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload, joinedload

from .. import db
from ..models import (
    Tenant,
    User,
    Branch,
    Transaction,
    SuperadminAuditLog,
    TenantPackage,
    TenantPlanHistory,
)
from ..permissions import (
    PERMISSION_MODULES,
    MODULE_CODES,
    tenant_package_module_cap,
    blocked_module_labels_for_package_role,
)

superadmin_bp = Blueprint('superadmin', __name__, url_prefix='/superadmin')

UNLIMITED_THRESHOLD = 9000
PER_PAGE = 20
PAKET_KODE_RE = re.compile(r'^[a-z0-9_]{2,30}$')


def _paket_kuota_map():
    rows = TenantPackage.query.filter_by(aktif=True).order_by(
        TenantPackage.sort_order, TenantPackage.id,
    ).all()
    if not rows:
        return {
            'basic': (3, 5),
            'pro': (10, 20),
            'enterprise': (9999, 9999),
        }
    return {p.kode.lower(): (p.max_cabang, p.max_user) for p in rows}


def _active_packages():
    return TenantPackage.query.order_by(
        TenantPackage.sort_order, TenantPackage.nama,
    ).all()


def _modules_json_from_form(form):
    sel = frozenset(x for x in form.getlist('perm') if x in MODULE_CODES)
    if not sel or sel == MODULE_CODES:
        return None
    if 'dashboard' not in sel:
        sel = sel | {'dashboard'}
    return json.dumps(sorted(sel))


def superadmin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_superadmin:
            flash('Akses ditolak! Hanya Super Admin.', 'danger')
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated


def _parse_expiry_date(s):
    if not s or not str(s).strip():
        return None
    try:
        d = datetime.strptime(str(s).strip()[:10], '%Y-%m-%d').date()
        return datetime.combine(d, time(23, 59, 59))
    except ValueError:
        return None


def _log_sa(action, tenant_id=None, detail=None):
    db.session.add(SuperadminAuditLog(
        actor_user_id=current_user.id,
        action=action,
        target_tenant_id=tenant_id,
        detail=(detail[:2000] if detail else None),
    ))


def _record_plan_history(
    tenant_id,
    event,
    old_paket_id,
    new_paket_id,
    old_kode,
    new_kode,
    old_max_cabang,
    new_max_cabang,
    old_max_user,
    new_max_user,
):
    db.session.add(TenantPlanHistory(
        tenant_id=tenant_id,
        actor_user_id=current_user.id,
        event=event,
        old_paket_id=old_paket_id,
        new_paket_id=new_paket_id,
        old_paket_kode=old_kode,
        new_paket_kode=new_kode,
        old_max_cabang=old_max_cabang,
        new_max_cabang=new_max_cabang,
        old_max_user=old_max_user,
        new_max_user=new_max_user,
    ))


def _paket_preview_labels(modules_json):
    return {
        'admin': blocked_module_labels_for_package_role(modules_json, 'admin'),
        'kasir': blocked_module_labels_for_package_role(modules_json, 'kasir'),
    }


def _tenant_query_filtered():
    q = (request.args.get('q') or '').strip()
    query = Tenant.query.options(
        selectinload(Tenant.branches),
        selectinload(Tenant.users),
        selectinload(Tenant.subscription),
    )
    if q:
        like = f'%{q}%'
        query = query.filter(
            or_(
                Tenant.nama.ilike(like),
                Tenant.kode.ilike(like),
                Tenant.email.ilike(like),
            )
        )
    if (request.args.get('inactive_pkg') or '').strip() == '1':
        query = query.join(TenantPackage, Tenant.paket_id == TenantPackage.id).filter(
            TenantPackage.aktif.is_(False),
        )
    sort = request.args.get('sort', 'nama')
    order = request.args.get('order', 'asc')
    cols = {
        'nama': Tenant.nama,
        'tanggal_daftar': Tenant.tanggal_daftar,
        'paket': Tenant.paket,
        'kode': Tenant.kode,
    }
    col = cols.get(sort, Tenant.nama)
    query = query.order_by(col.asc() if order == 'asc' else col.desc())
    return query


def _omzet_today_by_tenant():
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    end = datetime.combine(today, datetime.max.time())
    rows = db.session.query(
        Transaction.tenant_id,
        func.coalesce(func.sum(Transaction.total), 0),
    ).filter(
        Transaction.created_at.between(start, end),
        Transaction.status == 'selesai',
    ).group_by(Transaction.tenant_id).all()
    return {tid: float(s or 0) for tid, s in rows}


def _revenue_window(tenant_id, days):
    since = datetime.utcnow() - timedelta(days=days)
    return db.session.query(
        func.coalesce(func.sum(Transaction.total), 0),
    ).filter(
        Transaction.tenant_id == tenant_id,
        Transaction.status == 'selesai',
        Transaction.created_at >= since,
    ).scalar() or 0


def _chart_last_7_days(tenant_id):
    today = datetime.utcnow().date()
    start_d = today - timedelta(days=6)
    start_dt = datetime.combine(start_d, datetime.min.time())
    rows = db.session.query(
        func.date(Transaction.created_at),
        func.coalesce(func.sum(Transaction.total), 0),
    ).filter(
        Transaction.tenant_id == tenant_id,
        Transaction.status == 'selesai',
        Transaction.created_at >= start_dt,
    ).group_by(func.date(Transaction.created_at)).all()
    by_key = {}
    for r in rows:
        k = r[0]
        if hasattr(k, 'isoformat'):
            key = k.isoformat()
        else:
            key = str(k)[:10]
        by_key[key] = float(r[1] or 0)
    chart = []
    for i in range(7):
        d = start_d + timedelta(days=i)
        key = d.isoformat()
        chart.append({'label': d.strftime('%d/%m'), 'value': by_key.get(key, 0.0)})
    mx = max((c['value'] for c in chart), default=0) or 1.0
    for c in chart:
        c['pct'] = min(100.0, round(100.0 * c['value'] / mx, 1))
    return chart


@superadmin_bp.route('/')
@login_required
@superadmin_required
def index():
    base_q = _tenant_query_filtered()
    total_filtered = base_q.count()
    total_pages = max(1, (total_filtered + PER_PAGE - 1) // PER_PAGE)
    page = max(1, int(request.args.get('page', 1) or 1))
    if page > total_pages:
        page = total_pages
    tenants = base_q.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()

    total_tenants = Tenant.query.count()
    total_users = User.query.count()
    total_transaksi = Transaction.query.count()
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    end = datetime.combine(today, datetime.max.time())
    penjualan_hari_ini = db.session.query(func.coalesce(func.sum(Transaction.total), 0)).filter(
        Transaction.created_at.between(start, end),
        Transaction.status == 'selesai',
    ).scalar() or 0

    omzet_today = _omzet_today_by_tenant()

    inactive_pkg_tenant_count = Tenant.query.filter(
        Tenant.paket_id.isnot(None),
    ).join(TenantPackage, Tenant.paket_id == TenantPackage.id).filter(
        TenantPackage.aktif.is_(False),
    ).count()

    return render_template(
        'superadmin/index.html',
        tenants=tenants,
        total_tenants=total_tenants,
        total_users=total_users,
        total_transaksi=total_transaksi,
        penjualan_hari_ini=penjualan_hari_ini,
        omzet_today=omzet_today,
        page=page,
        total_pages=total_pages,
        total_filtered=total_filtered,
        per_page=PER_PAGE,
        q=request.args.get('q', ''),
        sort=request.args.get('sort', 'nama'),
        order=request.args.get('order', 'asc'),
        unlimited_threshold=UNLIMITED_THRESHOLD,
        utc_now=datetime.utcnow(),
        inactive_pkg_tenant_count=inactive_pkg_tenant_count,
        inactive_pkg_filter=(request.args.get('inactive_pkg') or '').strip() == '1',
    )


@superadmin_bp.route('/export.csv')
@login_required
@superadmin_required
def export_csv():
    query = _tenant_query_filtered()
    tenants = query.all()
    omzet_today = _omzet_today_by_tenant()
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow([
        'nama', 'kode', 'paket', 'email', 'telepon', 'aktif',
        'tanggal_daftar', 'tanggal_expired', 'max_cabang', 'max_user',
        'jumlah_cabang', 'jumlah_user', 'omzet_hari_ini',
    ])
    for t in tenants:
        w.writerow([
            t.nama, t.kode, t.paket, t.email or '', t.telepon or '',
            'ya' if t.aktif else 'tidak',
            t.tanggal_daftar.strftime('%Y-%m-%d') if t.tanggal_daftar else '',
            t.tanggal_expired.strftime('%Y-%m-%d') if t.tanggal_expired else '',
            t.max_cabang, t.max_user,
            len(t.branches), len(t.users),
            round(omzet_today.get(t.id, 0), 2),
        ])
    _log_sa('export_csv', detail=f'rows={len(tenants)}')
    db.session.commit()
    data = buf.getvalue()
    return Response(
        data,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=tenants.csv'},
    )


@superadmin_bp.route('/audit')
@login_required
@superadmin_required
def audit_logs():
    page = max(1, int(request.args.get('page', 1) or 1))
    q = SuperadminAuditLog.query.order_by(SuperadminAuditLog.created_at.desc())
    total = q.count()
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    if page > total_pages:
        page = total_pages
    logs = q.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()
    return render_template(
        'superadmin/audit_logs.html',
        logs=logs,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=PER_PAGE,
    )


@superadmin_bp.route('/paket')
@login_required
@superadmin_required
def paket_index():
    packages = TenantPackage.query.order_by(
        TenantPackage.sort_order, TenantPackage.nama,
    ).all()
    counts = dict(
        db.session.query(Tenant.paket_id, func.count(Tenant.id))
        .filter(Tenant.paket_id.isnot(None))
        .group_by(Tenant.paket_id)
        .all(),
    )
    return render_template(
        'superadmin/paket_index.html',
        packages=packages,
        tenant_count_by_paket=counts,
        unlimited_threshold=UNLIMITED_THRESHOLD,
        permission_modules=PERMISSION_MODULES,
    )


@superadmin_bp.route('/paket/add', methods=['GET', 'POST'])
@login_required
@superadmin_required
def paket_add():
    if request.method == 'POST':
        kode = (request.form.get('kode') or '').strip().lower()
        if not PAKET_KODE_RE.match(kode):
            flash('Kode paket 2–30 karakter: huruf kecil, angka, underscore.', 'danger')
            return redirect(url_for('superadmin.paket_add'))
        if TenantPackage.query.filter_by(kode=kode).first():
            flash('Kode paket sudah dipakai.', 'danger')
            return redirect(url_for('superadmin.paket_add'))
        try:
            max_cabang = int(request.form.get('max_cabang') or 1)
        except (TypeError, ValueError):
            max_cabang = 1
        try:
            max_user = int(request.form.get('max_user') or 1)
        except (TypeError, ValueError):
            max_user = 1
        try:
            harga_bulanan = float(request.form.get('harga_bulanan') or 0)
        except (TypeError, ValueError):
            harga_bulanan = 0
        try:
            harga_tahunan = float(request.form.get('harga_tahunan') or 0)
        except (TypeError, ValueError):
            harga_tahunan = 0
        try:
            sort_order = int(request.form.get('sort_order') or 0)
        except (TypeError, ValueError):
            sort_order = 0
        pkg = TenantPackage(
            kode=kode,
            nama=(request.form.get('nama') or kode).strip() or kode,
            deskripsi=(request.form.get('deskripsi') or '').strip() or None,
            max_cabang=max(1, max_cabang),
            max_user=max(1, max_user),
            modules_json=_modules_json_from_form(request.form),
            harga_bulanan=max(0, harga_bulanan),
            harga_tahunan=max(0, harga_tahunan),
            aktif=bool(request.form.get('aktif')),
            sort_order=sort_order,
        )
        db.session.add(pkg)
        _log_sa('paket_create', detail=f'kode={kode}')
        db.session.commit()
        flash(f'Paket "{pkg.nama}" dibuat.', 'success')
        return redirect(url_for('superadmin.paket_index'))

    pv = _paket_preview_labels(None)
    return render_template(
        'superadmin/paket_form.html',
        pkg=None,
        permission_modules=PERMISSION_MODULES,
        module_codes=MODULE_CODES,
        paket_preview=pv,
    )


@superadmin_bp.route('/paket/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@superadmin_required
def paket_edit(id):
    pkg = TenantPackage.query.get_or_404(id)
    if request.method == 'POST':
        kode = (request.form.get('kode') or '').strip().lower()
        if not PAKET_KODE_RE.match(kode):
            flash('Kode paket tidak valid.', 'danger')
            return redirect(url_for('superadmin.paket_edit', id=id))
        other = TenantPackage.query.filter(
            TenantPackage.kode == kode,
            TenantPackage.id != id,
        ).first()
        if other:
            flash('Kode paket sudah dipakai paket lain.', 'danger')
            return redirect(url_for('superadmin.paket_edit', id=id))
        try:
            max_cabang = int(request.form.get('max_cabang') or 1)
        except (TypeError, ValueError):
            max_cabang = pkg.max_cabang
        try:
            max_user = int(request.form.get('max_user') or 1)
        except (TypeError, ValueError):
            max_user = pkg.max_user
        try:
            harga_bulanan = float(request.form.get('harga_bulanan') or 0)
        except (TypeError, ValueError):
            harga_bulanan = pkg.harga_bulanan
        try:
            harga_tahunan = float(request.form.get('harga_tahunan') or 0)
        except (TypeError, ValueError):
            harga_tahunan = pkg.harga_tahunan
        try:
            sort_order = int(request.form.get('sort_order') or 0)
        except (TypeError, ValueError):
            sort_order = pkg.sort_order

        old_kode = pkg.kode
        pkg.kode = kode
        pkg.nama = (request.form.get('nama') or pkg.nama).strip() or pkg.nama
        pkg.deskripsi = (request.form.get('deskripsi') or '').strip() or None
        pkg.max_cabang = max(1, max_cabang)
        pkg.max_user = max(1, max_user)
        pkg.modules_json = _modules_json_from_form(request.form)
        pkg.harga_bulanan = max(0, harga_bulanan)
        pkg.harga_tahunan = max(0, harga_tahunan)
        pkg.aktif = bool(request.form.get('aktif'))
        pkg.sort_order = sort_order

        if old_kode != kode:
            Tenant.query.filter_by(paket_id=pkg.id).update(
                {'paket': kode}, synchronize_session=False,
            )
        _log_sa('paket_edit', detail=f'paket_id={id} kode={kode}')
        db.session.commit()
        flash('Paket diperbarui.', 'success')
        return redirect(url_for('superadmin.paket_index'))

    selected = None
    if pkg.modules_json and str(pkg.modules_json).strip():
        try:
            data = json.loads(pkg.modules_json)
            if isinstance(data, list):
                selected = frozenset(str(x) for x in data if x in MODULE_CODES)
        except (json.JSONDecodeError, TypeError):
            selected = None

    pv = _paket_preview_labels(pkg.modules_json)
    return render_template(
        'superadmin/paket_form.html',
        pkg=pkg,
        permission_modules=PERMISSION_MODULES,
        module_codes=MODULE_CODES,
        selected_modules=selected,
        paket_preview=pv,
    )


@superadmin_bp.route('/paket/<int:id>/toggle', methods=['POST'])
@login_required
@superadmin_required
def paket_toggle(id):
    pkg = TenantPackage.query.get_or_404(id)
    pkg.aktif = not pkg.aktif
    _log_sa('paket_toggle', detail=f'paket_id={id} aktif={pkg.aktif}')
    db.session.commit()
    flash(f'Paket "{pkg.nama}" {"diaktifkan" if pkg.aktif else "dinonaktifkan"}.', 'info')
    return redirect(url_for('superadmin.paket_index'))


@superadmin_bp.route('/paket/<int:id>/delete', methods=['POST'])
@login_required
@superadmin_required
def paket_delete(id):
    pkg = TenantPackage.query.get_or_404(id)
    n = Tenant.query.filter_by(paket_id=id).count()
    if n:
        flash(f'Tidak bisa hapus: {n} tenant memakai paket ini.', 'danger')
        return redirect(url_for('superadmin.paket_index'))
    kode = pkg.kode
    db.session.delete(pkg)
    _log_sa('paket_delete', detail=f'kode={kode}')
    db.session.commit()
    flash(f'Paket "{kode}" dihapus.', 'success')
    return redirect(url_for('superadmin.paket_index'))


@superadmin_bp.route('/stop-impersonate', methods=['POST'])
@login_required
def stop_impersonate():
    real_id = session.pop('impersonator_id', None)
    if not real_id:
        flash('Tidak dalam mode masuk sebagai tenant.', 'info')
        return redirect(url_for('dashboard.index'))
    real = User.query.get(real_id)
    if not real or not real.is_superadmin:
        flash('Sesi tidak valid. Silakan login lagi.', 'danger')
        return redirect(url_for('auth.login'))
    login_user(real)
    db.session.refresh(real)
    session['user_session_version'] = int(getattr(real, 'session_version', 0) or 0)
    session['tenant_id'] = None
    session['branch_id'] = None
    flash('Kembali ke akun Super Admin.', 'success')
    return redirect(url_for('superadmin.index'))


@superadmin_bp.route('/tenants/add', methods=['GET', 'POST'])
@login_required
@superadmin_required
def add_tenant():
    if request.method == 'POST':
        kode = request.form['kode'].upper().strip()
        exist = Tenant.query.filter_by(kode=kode).first()
        if exist:
            flash('Kode tenant sudah digunakan!', 'danger')
            return redirect(url_for('superadmin.add_tenant'))

        try:
            paket_id = int(request.form.get('paket_id') or 0)
        except (TypeError, ValueError):
            paket_id = 0
        pkg = TenantPackage.query.filter_by(id=paket_id, aktif=True).first()
        if not pkg:
            flash('Pilih paket langganan yang valid.', 'danger')
            return redirect(url_for('superadmin.add_tenant'))
        kuota = _paket_kuota_map()
        d_mc, d_mu = kuota.get(pkg.kode.lower(), (pkg.max_cabang, pkg.max_user))
        try:
            max_cabang = int(request.form.get('max_cabang') or d_mc)
        except (TypeError, ValueError):
            max_cabang = d_mc
        try:
            max_user = int(request.form.get('max_user') or d_mu)
        except (TypeError, ValueError):
            max_user = d_mu

        tenant = Tenant(
            nama=request.form['nama'],
            kode=kode,
            alamat=request.form.get('alamat', ''),
            telepon=request.form.get('telepon', ''),
            email=request.form.get('email', ''),
            paket_id=pkg.id,
            paket=pkg.kode,
            max_cabang=max_cabang,
            max_user=max_user,
            tanggal_expired=_parse_expiry_date(request.form.get('tanggal_expired')),
        )
        db.session.add(tenant)
        db.session.flush()

        branch = Branch(
            tenant_id=tenant.id,
            nama='Cabang Utama',
            kode='MAIN',
            alamat=tenant.alamat or '',
        )
        db.session.add(branch)
        db.session.flush()

        admin_username = 'admin_' + kode.lower()
        admin = User(
            tenant_id=tenant.id,
            branch_id=branch.id,
            nama='Admin ' + tenant.nama,
            username=admin_username,
            role='admin',
        )
        default_pw = 'admin123'
        admin.set_password(default_pw)
        db.session.add(admin)
        _record_plan_history(
            tenant.id,
            'tenant_create',
            None,
            pkg.id,
            None,
            pkg.kode,
            None,
            max_cabang,
            None,
            max_user,
        )
        _log_sa('tenant_create', tenant.id, detail=f'kode={kode} paket={pkg.kode}')
        db.session.commit()

        session['tenant_bootstrap'] = {
            'tenant_id': tenant.id,
            'admin_username': admin_username,
            'admin_password': default_pw,
        }
        flash(f'Tenant "{tenant.nama}" berhasil dibuat. Simpan kredensial di halaman detail (sekali tampil).', 'success')
        return redirect(url_for('superadmin.view_tenant', id=tenant.id))

    packages = TenantPackage.query.filter_by(aktif=True).order_by(
        TenantPackage.sort_order, TenantPackage.nama,
    ).all()
    if not packages:
        flash('Belum ada paket aktif. Buat minimal satu paket di menu Paket tenant.', 'warning')
    return render_template(
        'superadmin/add_tenant.html',
        paket_kuota=_paket_kuota_map(),
        packages=packages,
    )


@superadmin_bp.route('/tenants/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@superadmin_required
def edit_tenant(id):
    tenant = Tenant.query.get_or_404(id)
    if request.method == 'POST':
        old_pid = tenant.paket_id
        old_kode = tenant.paket
        old_mc = tenant.max_cabang
        old_mu = tenant.max_user
        try:
            paket_id = int(request.form.get('paket_id') or 0)
        except (TypeError, ValueError):
            paket_id = 0
        pkg = TenantPackage.query.get(paket_id)
        if not pkg:
            flash('Paket tidak ditemukan.', 'danger')
            return redirect(url_for('superadmin.edit_tenant', id=id))
        sync = request.form.get('sync_paket_quotas')
        kuota = _paket_kuota_map()
        d_mc, d_mu = kuota.get(pkg.kode.lower(), (pkg.max_cabang, pkg.max_user))
        try:
            max_cabang = int(d_mc if sync else (request.form.get('max_cabang') or tenant.max_cabang))
        except (TypeError, ValueError):
            max_cabang = tenant.max_cabang
        try:
            max_user = int(d_mu if sync else (request.form.get('max_user') or tenant.max_user))
        except (TypeError, ValueError):
            max_user = tenant.max_user

        tenant.nama = request.form.get('nama', tenant.nama).strip() or tenant.nama
        tenant.alamat = request.form.get('alamat', '')
        tenant.telepon = request.form.get('telepon', '')
        tenant.email = request.form.get('email', '')
        tenant.paket_id = pkg.id
        tenant.paket = pkg.kode
        tenant.max_cabang = max_cabang
        tenant.max_user = max_user
        tenant.aktif = bool(request.form.get('aktif'))
        exp = _parse_expiry_date(request.form.get('tanggal_expired'))
        tenant.tanggal_expired = exp

        if (
            old_pid != tenant.paket_id
            or (old_kode or '') != (tenant.paket or '')
            or old_mc != tenant.max_cabang
            or old_mu != tenant.max_user
        ):
            _record_plan_history(
                tenant.id,
                'plan_change',
                old_pid,
                tenant.paket_id,
                old_kode,
                tenant.paket,
                old_mc,
                tenant.max_cabang,
                old_mu,
                tenant.max_user,
            )
            _log_sa(
                'tenant_plan_change',
                tenant.id,
                detail=f'paket {(old_kode or "")}→{tenant.paket} '
                f'cab {old_mc}→{tenant.max_cabang} user {old_mu}→{tenant.max_user}',
            )
        _log_sa('tenant_edit', tenant.id)
        db.session.commit()
        flash('Data tenant diperbarui.', 'success')
        return redirect(url_for('superadmin.view_tenant', id=id))

    packages = TenantPackage.query.filter(
        or_(TenantPackage.aktif.is_(True), TenantPackage.id == tenant.paket_id),
    ).order_by(TenantPackage.sort_order, TenantPackage.nama).all()
    return render_template(
        'superadmin/edit_tenant.html',
        tenant=tenant,
        paket_kuota=_paket_kuota_map(),
        packages=packages,
        unlimited_threshold=UNLIMITED_THRESHOLD,
    )


@superadmin_bp.route('/tenants/toggle/<int:id>', methods=['POST'])
@login_required
@superadmin_required
def toggle_tenant(id):
    tenant = Tenant.query.get_or_404(id)
    tenant.aktif = not tenant.aktif
    _log_sa('tenant_toggle', id, detail=f'aktif={tenant.aktif}')
    db.session.commit()
    status = 'diaktifkan' if tenant.aktif else 'dinonaktifkan'
    flash(f'Tenant "{tenant.nama}" {status}.', 'info')
    idx_args = {}
    for key in ('q', 'sort', 'order', 'page'):
        v = request.form.get('ret_' + key) or request.args.get(key)
        if v:
            idx_args[key] = v
    return redirect(url_for('superadmin.index', **idx_args))


@superadmin_bp.route('/tenants/<int:id>/reset-password', methods=['POST'])
@login_required
@superadmin_required
def reset_tenant_user_password(id):
    tenant = Tenant.query.get_or_404(id)
    uid = int(request.form.get('user_id', 0) or 0)
    user = User.query.filter_by(id=uid, tenant_id=id).first()
    if not user:
        flash('User tidak ditemukan.', 'danger')
        return redirect(url_for('superadmin.view_tenant', id=id))
    new_pw = (request.form.get('new_password') or '').strip()
    gen = request.form.get('generate_random')
    if gen:
        alphabet = string.ascii_letters + string.digits
        new_pw = ''.join(secrets.choice(alphabet) for _ in range(12))
    if len(new_pw) < 6:
        flash('Password minimal 6 karakter atau pilih generate.', 'danger')
        return redirect(url_for('superadmin.view_tenant', id=id))
    user.set_password(new_pw)
    user.session_version = int(getattr(user, 'session_version', 0) or 0) + 1
    _log_sa('reset_user_password', id, detail=f'user_id={user.id} username={user.username}')
    db.session.commit()
    session['password_reset_result'] = {'username': user.username, 'password': new_pw}
    flash('Password user diperbarui. Lihat sekali di halaman ini.', 'success')
    return redirect(url_for('superadmin.view_tenant', id=id))


@superadmin_bp.route('/tenants/<int:id>/impersonate', methods=['POST'])
@login_required
@superadmin_required
def impersonate(id):
    tenant = Tenant.query.get_or_404(id)
    admin_u = User.query.filter_by(tenant_id=id, role='admin', aktif=True).first()
    if not admin_u:
        admin_u = User.query.filter_by(tenant_id=id, aktif=True).order_by(User.id).first()
    if not admin_u:
        flash('Tidak ada user aktif di tenant ini.', 'danger')
        return redirect(url_for('superadmin.view_tenant', id=id))
    impersonator_id = current_user.id
    _log_sa('impersonate', id, detail=f'target_user_id={admin_u.id}')
    db.session.commit()
    login_user(admin_u)
    db.session.refresh(admin_u)
    session['impersonator_id'] = impersonator_id
    session['user_session_version'] = int(getattr(admin_u, 'session_version', 0) or 0)
    session['tenant_id'] = admin_u.tenant_id
    session['branch_id'] = admin_u.branch_id
    flash(f'Anda masuk sebagai {admin_u.nama} (@{admin_u.username}).', 'info')
    return redirect(url_for('dashboard.index'))


@superadmin_bp.route('/tenants/<int:id>/riwayat-paket')
@login_required
@superadmin_required
def tenant_plan_history(id):
    tenant = Tenant.query.get_or_404(id)
    page = max(1, int(request.args.get('page', 1) or 1))
    q = TenantPlanHistory.query.filter_by(tenant_id=id).order_by(
        TenantPlanHistory.created_at.desc(),
    )
    total = q.count()
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    if page > total_pages:
        page = total_pages
    rows = (
        q.options(joinedload(TenantPlanHistory.actor))
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
        .all()
    )
    return render_template(
        'superadmin/tenant_plan_history.html',
        tenant=tenant,
        rows=rows,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=PER_PAGE,
    )


@superadmin_bp.route('/tenants/<int:id>')
@login_required
@superadmin_required
def view_tenant(id):
    tenant = Tenant.query.options(
        selectinload(Tenant.branches),
        selectinload(Tenant.users),
        selectinload(Tenant.subscription),
    ).get_or_404(id)
    branches = tenant.branches
    users = tenant.users

    total_penjualan = db.session.query(func.coalesce(func.sum(Transaction.total), 0)).filter_by(
        tenant_id=id, status='selesai',
    ).scalar() or 0
    total_transaksi = Transaction.query.filter_by(tenant_id=id, status='selesai').count()

    today = datetime.utcnow().date()
    st = datetime.combine(today, datetime.min.time())
    en = datetime.combine(today, datetime.max.time())
    omzet_today = db.session.query(func.coalesce(func.sum(Transaction.total), 0)).filter(
        Transaction.tenant_id == id,
        Transaction.status == 'selesai',
        Transaction.created_at.between(st, en),
    ).scalar() or 0

    omzet_7 = float(_revenue_window(id, 7))
    omzet_30 = float(_revenue_window(id, 30))
    chart_7d = _chart_last_7_days(id)

    bootstrap = session.pop('tenant_bootstrap', None)
    pw_reset = session.pop('password_reset_result', None)
    if bootstrap and bootstrap.get('tenant_id') != id:
        session['tenant_bootstrap'] = bootstrap
        bootstrap = None

    now = datetime.utcnow()
    expired = bool(tenant.tanggal_expired and now > tenant.tanggal_expired)
    near_expiry = bool(
        tenant.tanggal_expired and not expired
        and tenant.tanggal_expired - now <= timedelta(days=14)
    )

    bc, bu = len(branches), len(users)
    quota_branch_warn = tenant.max_cabang < UNLIMITED_THRESHOLD and bc >= tenant.max_cabang
    quota_branch_near = tenant.max_cabang < UNLIMITED_THRESHOLD and not quota_branch_warn and bc >= max(1, tenant.max_cabang - 1)
    quota_user_warn = tenant.max_user < UNLIMITED_THRESHOLD and bu >= tenant.max_user
    quota_user_near = tenant.max_user < UNLIMITED_THRESHOLD and not quota_user_warn and bu >= max(1, tenant.max_user - 1)

    cap = tenant_package_module_cap(tenant.id)
    if cap is None:
        paket_modul_ringkas = 'Semua modul (dibatasi hanya oleh izin per pengguna)'
    else:
        paket_modul_ringkas = ', '.join(
            label for code, label in PERMISSION_MODULES if code in cap
        ) or 'Hanya dashboard'

    plan_history_recent = (
        TenantPlanHistory.query.options(joinedload(TenantPlanHistory.actor))
        .filter_by(tenant_id=id)
        .order_by(TenantPlanHistory.created_at.desc())
        .limit(12)
        .all()
    )

    return render_template(
        'superadmin/view_tenant.html',
        tenant=tenant,
        branches=branches,
        users=users,
        total_penjualan=total_penjualan,
        total_transaksi=total_transaksi,
        omzet_today=omzet_today,
        omzet_7=omzet_7,
        omzet_30=omzet_30,
        chart_7d=chart_7d,
        tenant_bootstrap=bootstrap,
        password_reset_result=pw_reset,
        expired=expired,
        near_expiry=near_expiry,
        quota_branch_warn=quota_branch_warn,
        quota_branch_near=quota_branch_near,
        quota_user_warn=quota_user_warn,
        quota_user_near=quota_user_near,
        unlimited_threshold=UNLIMITED_THRESHOLD,
        paket_modul_ringkas=paket_modul_ringkas,
        plan_history_recent=plan_history_recent,
    )
