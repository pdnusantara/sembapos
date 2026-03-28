import csv
import json
import os
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
    jsonify,
)
from urllib.request import urlopen, Request
from urllib.error import URLError
from flask_login import login_required, current_user, login_user
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload, joinedload
from werkzeug.utils import secure_filename

from .. import db
from ..models import (
    Tenant,
    User,
    Branch,
    Transaction,
    SuperadminAuditLog,
    TenantPackage,
    TenantPlanHistory,
    MarketplaceSeller,
    MarketplaceCategory,
    MarketplaceProduct,
    MarketplaceProductImage,
    MarketplaceOrder,
    MarketplaceOrderItem,
    MarketplaceOrderStatusHistory,
    MARKETPLACE_ORDER_STATUSES,
    MARKETPLACE_ORDER_STATUS_LABELS,
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
WILAYAH_API_BASE = 'https://www.emsifa.com/api-wilayah-indonesia/api'


def _normalize_wilayah_items(items):
    """API emsifa pakai field `name`; template superadmin pakai `nama`."""
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        oid = it.get('id')
        if oid is None:
            continue
        nama = (it.get('nama') or it.get('name') or '').strip() or str(oid)
        out.append({'id': str(oid), 'nama': nama})
    return out


def _fetch_wilayah_json(path):
    url = f'{WILAYAH_API_BASE}/{path.lstrip("/")}'
    req = Request(url, headers={'User-Agent': 'sembako-superadmin/1.0'})
    try:
        with urlopen(req, timeout=12) as resp:
            data = resp.read().decode('utf-8')
            parsed = json.loads(data)
            if isinstance(parsed, list):
                return _normalize_wilayah_items(parsed)
            return []
    except (URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return []


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
            provinsi=(request.form.get('provinsi') or '').strip() or None,
            kab_kota=(request.form.get('kab_kota') or '').strip() or None,
            kecamatan=(request.form.get('kecamatan') or '').strip() or None,
            desa=(request.form.get('desa') or '').strip() or None,
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


@superadmin_bp.route('/wilayah/provinsi')
@login_required
@superadmin_required
def wilayah_provinsi():
    return jsonify(_fetch_wilayah_json('provinces.json'))


@superadmin_bp.route('/wilayah/kabupaten/<prov_id>')
@login_required
@superadmin_required
def wilayah_kabupaten(prov_id):
    prov_id = re.sub(r'[^0-9]', '', str(prov_id))
    if not prov_id:
        return jsonify([])
    return jsonify(_fetch_wilayah_json(f'regencies/{prov_id}.json'))


@superadmin_bp.route('/wilayah/kecamatan/<kab_id>')
@login_required
@superadmin_required
def wilayah_kecamatan(kab_id):
    kab_id = re.sub(r'[^0-9]', '', str(kab_id))
    if not kab_id:
        return jsonify([])
    return jsonify(_fetch_wilayah_json(f'districts/{kab_id}.json'))


@superadmin_bp.route('/wilayah/desa/<kec_id>')
@login_required
@superadmin_required
def wilayah_desa(kec_id):
    kec_id = re.sub(r'[^0-9]', '', str(kec_id))
    if not kec_id:
        return jsonify([])
    return jsonify(_fetch_wilayah_json(f'villages/{kec_id}.json'))


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
        tenant.provinsi = (request.form.get('provinsi') or '').strip() or None
        tenant.kab_kota = (request.form.get('kab_kota') or '').strip() or None
        tenant.kecamatan = (request.form.get('kecamatan') or '').strip() or None
        tenant.desa = (request.form.get('desa') or '').strip() or None
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


# ─────────────────────────────────────────────────────────────
# MARKETPLACE — SELLER CENTER (Super Admin)
# ─────────────────────────────────────────────────────────────

ALLOWED_IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


def _allowed_image(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTS


def _save_marketplace_image(file_obj, subfolder='sellers'):
    """Simpan gambar ke static/uploads/marketplace/<subfolder>/, return URL relatif."""
    from flask import current_app
    upload_dir = os.path.join(
        current_app.static_folder, 'uploads', 'marketplace', subfolder
    )
    os.makedirs(upload_dir, exist_ok=True)
    filename = secure_filename(file_obj.filename)
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'jpg'
    unique_name = f"{secrets.token_hex(10)}.{ext}"
    file_obj.save(os.path.join(upload_dir, unique_name))
    return f"uploads/marketplace/{subfolder}/{unique_name}"


# ── CATEGORIES ────────────────────────────────────────────────

@superadmin_bp.route('/marketplace/categories', methods=['GET', 'POST'])
@superadmin_required
def marketplace_categories():
    if request.method == 'POST':
        action = request.form.get('action', 'add')

        if action == 'add':
            nama = request.form.get('nama', '').strip()
            icon = request.form.get('icon', '📦').strip() or '📦'
            sort_order = request.form.get('sort_order', 0, type=int)
            if not nama:
                flash('Nama kategori wajib diisi.', 'danger')
            else:
                slug = re.sub(r'[^a-z0-9]+', '-', nama.lower()).strip('-')
                existing = MarketplaceCategory.query.filter_by(slug=slug).first()
                if existing:
                    slug = f"{slug}-{secrets.token_hex(3)}"
                cat = MarketplaceCategory(
                    nama=nama, icon=icon, slug=slug, sort_order=sort_order
                )
                db.session.add(cat)
                db.session.commit()
                flash(f'Kategori "{nama}" berhasil ditambahkan.', 'success')

        elif action == 'delete':
            cat_id = request.form.get('cat_id', type=int)
            cat = MarketplaceCategory.query.get_or_404(cat_id)
            if cat.products:
                flash('Tidak bisa menghapus kategori yang masih memiliki produk.', 'danger')
            else:
                db.session.delete(cat)
                db.session.commit()
                flash('Kategori dihapus.', 'success')

        elif action == 'toggle':
            cat_id = request.form.get('cat_id', type=int)
            cat = MarketplaceCategory.query.get_or_404(cat_id)
            cat.aktif = not cat.aktif
            db.session.commit()
            flash(f'Kategori {"diaktifkan" if cat.aktif else "dinonaktifkan"}.', 'success')

        return redirect(url_for('superadmin.marketplace_categories'))

    categories = MarketplaceCategory.query.order_by(MarketplaceCategory.sort_order).all()
    return render_template(
        'superadmin/marketplace/categories.html',
        categories=categories,
    )


# ── SELLERS ──────────────────────────────────────────────────

@superadmin_bp.route('/marketplace/sellers')
@superadmin_required
def marketplace_sellers():
    sellers = MarketplaceSeller.query.order_by(MarketplaceSeller.nama).all()
    # Hitung stats per seller
    for s in sellers:
        s._total_produk = MarketplaceProduct.query.filter_by(seller_id=s.id).count()
        s._total_order = MarketplaceOrder.query.filter_by(seller_id=s.id).count()
    return render_template(
        'superadmin/marketplace/sellers.html',
        sellers=sellers,
    )


@superadmin_bp.route('/marketplace/sellers/add', methods=['GET', 'POST'])
@superadmin_required
def marketplace_seller_add():
    if request.method == 'POST':
        nama = request.form.get('nama', '').strip()
        if not nama:
            flash('Nama seller wajib diisi.', 'danger')
            return render_template('superadmin/marketplace/seller_form.html', seller=None)

        logo_url = None
        logo_file = request.files.get('logo')
        if logo_file and logo_file.filename and _allowed_image(logo_file.filename):
            logo_url = _save_marketplace_image(logo_file, 'sellers')

        seller = MarketplaceSeller(
            nama=nama,
            deskripsi=request.form.get('deskripsi', '').strip(),
            logo=logo_url,
            alamat=request.form.get('alamat', '').strip(),
            telepon=request.form.get('telepon', '').strip(),
            email=request.form.get('email', '').strip(),
        )
        db.session.add(seller)
        db.session.commit()
        flash(f'Seller "{nama}" berhasil ditambahkan.', 'success')
        return redirect(url_for('superadmin.marketplace_sellers'))

    return render_template('superadmin/marketplace/seller_form.html', seller=None)


@superadmin_bp.route('/marketplace/sellers/<int:seller_id>/edit', methods=['GET', 'POST'])
@superadmin_required
def marketplace_seller_edit(seller_id):
    seller = MarketplaceSeller.query.get_or_404(seller_id)

    if request.method == 'POST':
        nama = request.form.get('nama', '').strip()
        if not nama:
            flash('Nama seller wajib diisi.', 'danger')
            return render_template('superadmin/marketplace/seller_form.html', seller=seller)

        seller.nama = nama
        seller.deskripsi = request.form.get('deskripsi', '').strip()
        seller.alamat = request.form.get('alamat', '').strip()
        seller.telepon = request.form.get('telepon', '').strip()
        seller.email = request.form.get('email', '').strip()

        logo_file = request.files.get('logo')
        if logo_file and logo_file.filename and _allowed_image(logo_file.filename):
            seller.logo = _save_marketplace_image(logo_file, 'sellers')

        db.session.commit()
        flash('Seller berhasil diperbarui.', 'success')
        return redirect(url_for('superadmin.marketplace_sellers'))

    return render_template('superadmin/marketplace/seller_form.html', seller=seller)


@superadmin_bp.route('/marketplace/sellers/<int:seller_id>/toggle', methods=['POST'])
@superadmin_required
def marketplace_seller_toggle(seller_id):
    seller = MarketplaceSeller.query.get_or_404(seller_id)
    seller.aktif = not seller.aktif
    db.session.commit()
    flash(f'Seller {"diaktifkan" if seller.aktif else "dinonaktifkan"}.', 'success')
    return redirect(url_for('superadmin.marketplace_sellers'))


# ── PRODUCTS ──────────────────────────────────────────────────

@superadmin_bp.route('/marketplace/products')
@superadmin_required
def marketplace_products():
    page = request.args.get('page', 1, type=int)
    seller_id = request.args.get('seller', type=int)
    category_id = request.args.get('category', type=int)
    q = request.args.get('q', '').strip()

    query = MarketplaceProduct.query

    if seller_id:
        query = query.filter_by(seller_id=seller_id)
    if category_id:
        query = query.filter_by(category_id=category_id)
    if q:
        query = query.filter(MarketplaceProduct.nama.ilike(f'%{q}%'))

    query = query.order_by(MarketplaceProduct.created_at.desc())
    pagination = query.paginate(page=page, per_page=30, error_out=False)

    sellers = MarketplaceSeller.query.order_by(MarketplaceSeller.nama).all()
    categories = MarketplaceCategory.query.order_by(MarketplaceCategory.sort_order).all()

    return render_template(
        'superadmin/marketplace/products.html',
        products=pagination.items,
        pagination=pagination,
        sellers=sellers,
        categories=categories,
        seller_id=seller_id,
        category_id=category_id,
        q=q,
    )


@superadmin_bp.route('/marketplace/products/add', methods=['GET', 'POST'])
@superadmin_required
def marketplace_product_add():
    sellers = MarketplaceSeller.query.filter_by(aktif=True).order_by(MarketplaceSeller.nama).all()
    categories = MarketplaceCategory.query.filter_by(aktif=True).order_by(
        MarketplaceCategory.sort_order
    ).all()

    if request.method == 'POST':
        nama = request.form.get('nama', '').strip()
        seller_id = request.form.get('seller_id', type=int)
        harga = request.form.get('harga', 0, type=float)

        errors = []
        if not nama:
            errors.append('Nama produk wajib diisi.')
        if not seller_id:
            errors.append('Seller wajib dipilih.')
        if harga <= 0:
            errors.append('Harga harus lebih dari 0.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template(
                'superadmin/marketplace/product_form.html',
                product=None,
                sellers=sellers,
                categories=categories,
            )

        gambar_utama = None
        main_img = request.files.get('gambar_utama')
        if main_img and main_img.filename and _allowed_image(main_img.filename):
            gambar_utama = _save_marketplace_image(main_img, 'products')

        harga_grosir = request.form.get('harga_grosir', type=float) or None
        min_qty_grosir = request.form.get('min_qty_grosir', type=int) or None

        product = MarketplaceProduct(
            seller_id=seller_id,
            category_id=request.form.get('category_id', type=int) or None,
            nama=nama,
            deskripsi=request.form.get('deskripsi', '').strip(),
            harga=harga,
            harga_grosir=harga_grosir,
            min_qty_grosir=min_qty_grosir,
            stok=request.form.get('stok', 0, type=int),
            satuan=request.form.get('satuan', 'pcs').strip() or 'pcs',
            berat_gram=request.form.get('berat_gram', 0, type=int),
            gambar_utama=gambar_utama,
            sku=request.form.get('sku', '').strip() or None,
        )
        db.session.add(product)
        db.session.flush()

        # Galeri foto tambahan
        for extra_file in request.files.getlist('gallery_images'):
            if extra_file and extra_file.filename and _allowed_image(extra_file.filename):
                img_url = _save_marketplace_image(extra_file, 'products')
                img = MarketplaceProductImage(product_id=product.id, url=img_url)
                db.session.add(img)

        db.session.commit()
        flash(f'Produk "{nama}" berhasil ditambahkan.', 'success')
        return redirect(url_for('superadmin.marketplace_products'))

    return render_template(
        'superadmin/marketplace/product_form.html',
        product=None,
        sellers=sellers,
        categories=categories,
    )


@superadmin_bp.route('/marketplace/products/<int:product_id>/edit', methods=['GET', 'POST'])
@superadmin_required
def marketplace_product_edit(product_id):
    product = MarketplaceProduct.query.get_or_404(product_id)
    sellers = MarketplaceSeller.query.filter_by(aktif=True).order_by(MarketplaceSeller.nama).all()
    categories = MarketplaceCategory.query.filter_by(aktif=True).order_by(
        MarketplaceCategory.sort_order
    ).all()

    if request.method == 'POST':
        nama = request.form.get('nama', '').strip()
        harga = request.form.get('harga', 0, type=float)

        if not nama or harga <= 0:
            flash('Nama dan harga wajib diisi dengan benar.', 'danger')
            return render_template(
                'superadmin/marketplace/product_form.html',
                product=product,
                sellers=sellers,
                categories=categories,
            )

        product.nama = nama
        product.seller_id = request.form.get('seller_id', type=int) or product.seller_id
        product.category_id = request.form.get('category_id', type=int) or None
        product.deskripsi = request.form.get('deskripsi', '').strip()
        product.harga = harga
        product.harga_grosir = request.form.get('harga_grosir', type=float) or None
        product.min_qty_grosir = request.form.get('min_qty_grosir', type=int) or None
        product.stok = request.form.get('stok', 0, type=int)
        product.satuan = request.form.get('satuan', 'pcs').strip() or 'pcs'
        product.berat_gram = request.form.get('berat_gram', 0, type=int)
        product.sku = request.form.get('sku', '').strip() or None
        product.aktif = request.form.get('aktif') == '1'

        main_img = request.files.get('gambar_utama')
        if main_img and main_img.filename and _allowed_image(main_img.filename):
            product.gambar_utama = _save_marketplace_image(main_img, 'products')

        # Hapus gambar gallery yang dipilih
        delete_img_ids = request.form.getlist('delete_image')
        if delete_img_ids:
            MarketplaceProductImage.query.filter(
                MarketplaceProductImage.id.in_([int(x) for x in delete_img_ids if x.isdigit()])
            ).delete(synchronize_session=False)

        # Tambah galeri baru
        for extra_file in request.files.getlist('gallery_images'):
            if extra_file and extra_file.filename and _allowed_image(extra_file.filename):
                img_url = _save_marketplace_image(extra_file, 'products')
                img = MarketplaceProductImage(product_id=product.id, url=img_url)
                db.session.add(img)

        db.session.commit()
        flash('Produk berhasil diperbarui.', 'success')
        return redirect(url_for('superadmin.marketplace_products'))

    return render_template(
        'superadmin/marketplace/product_form.html',
        product=product,
        sellers=sellers,
        categories=categories,
    )


@superadmin_bp.route('/marketplace/products/<int:product_id>/delete', methods=['POST'])
@superadmin_required
def marketplace_product_delete(product_id):
    product = MarketplaceProduct.query.get_or_404(product_id)
    if product.order_items:
        flash('Produk tidak dapat dihapus karena sudah terdapat dalam pesanan.', 'danger')
        return redirect(url_for('superadmin.marketplace_products'))
    db.session.delete(product)
    db.session.commit()
    flash('Produk berhasil dihapus.', 'success')
    return redirect(url_for('superadmin.marketplace_products'))


# ── ORDERS (SELLER CENTER) ────────────────────────────────────

@superadmin_bp.route('/marketplace/orders')
@superadmin_required
def marketplace_orders():
    page = request.args.get('page', 1, type=int)
    status_filter = request.args.get('status', '')
    seller_id = request.args.get('seller', type=int)
    q = request.args.get('q', '').strip()

    query = MarketplaceOrder.query

    if status_filter:
        query = query.filter_by(status=status_filter)
    if seller_id:
        query = query.filter_by(seller_id=seller_id)
    if q:
        query = query.filter(
            or_(
                MarketplaceOrder.nomor.ilike(f'%{q}%'),
                MarketplaceOrder.nama_penerima.ilike(f'%{q}%'),
            )
        )

    query = query.order_by(MarketplaceOrder.created_at.desc())
    pagination = query.paginate(page=page, per_page=30, error_out=False)

    sellers = MarketplaceSeller.query.order_by(MarketplaceSeller.nama).all()

    # Stats
    stats = {
        'total': MarketplaceOrder.query.count(),
        'pending': MarketplaceOrder.query.filter_by(status='pending').count(),
        'processing': MarketplaceOrder.query.filter(
            MarketplaceOrder.status.in_(['confirmed', 'processing'])
        ).count(),
        'shipped': MarketplaceOrder.query.filter_by(status='shipped').count(),
        'delivered': MarketplaceOrder.query.filter_by(status='delivered').count(),
        'cancelled': MarketplaceOrder.query.filter_by(status='cancelled').count(),
    }

    return render_template(
        'superadmin/marketplace/orders.html',
        orders=pagination.items,
        pagination=pagination,
        sellers=sellers,
        status_filter=status_filter,
        seller_id=seller_id,
        q=q,
        all_statuses=MARKETPLACE_ORDER_STATUSES,
        stats=stats,
    )


@superadmin_bp.route('/marketplace/orders/<int:order_id>')
@superadmin_required
def marketplace_order_detail(order_id):
    order = MarketplaceOrder.query.get_or_404(order_id)
    return render_template(
        'superadmin/marketplace/order_detail.html',
        order=order,
        all_statuses=MARKETPLACE_ORDER_STATUSES,
        status_labels=MARKETPLACE_ORDER_STATUS_LABELS,
    )


@superadmin_bp.route('/marketplace/orders/<int:order_id>/status', methods=['POST'])
@superadmin_required
def marketplace_order_status(order_id):
    order = MarketplaceOrder.query.get_or_404(order_id)
    new_status = request.form.get('status', '').strip()
    catatan = request.form.get('catatan', '').strip()

    valid_statuses = [s for s, _ in MARKETPLACE_ORDER_STATUSES]
    if new_status not in valid_statuses:
        flash('Status tidak valid.', 'danger')
        return redirect(url_for('superadmin.marketplace_order_detail', order_id=order_id))

    if new_status == order.status:
        flash('Status tidak berubah.', 'info')
        return redirect(url_for('superadmin.marketplace_order_detail', order_id=order_id))

    old_status = order.status
    order.status = new_status
    history = MarketplaceOrderStatusHistory(
        order_id=order.id,
        from_status=old_status,
        to_status=new_status,
        catatan=catatan or None,
        changed_by_user_id=current_user.id,
    )
    db.session.add(history)

    # Kembalikan stok jika dibatalkan oleh superadmin
    if new_status == 'cancelled' and old_status not in ('cancelled',):
        for item in order.items:
            if item.product_id:
                product = MarketplaceProduct.query.get(item.product_id)
                if product:
                    product.stok += item.qty

    db.session.commit()
    flash(
        f'Status pesanan diubah dari "{MARKETPLACE_ORDER_STATUS_LABELS.get(old_status, old_status)}" '
        f'ke "{MARKETPLACE_ORDER_STATUS_LABELS.get(new_status, new_status)}".', 'success'
    )
    return redirect(url_for('superadmin.marketplace_order_detail', order_id=order_id))


@superadmin_bp.route('/marketplace/orders/<int:order_id>/invoice')
@superadmin_required
def marketplace_order_invoice(order_id):
    order = MarketplaceOrder.query.get_or_404(order_id)
    return render_template('marketplace/invoice.html', order=order, is_admin=True,
                           now=datetime.utcnow())


# ─────────────────────────────────────────────────────────────
# MARKETPLACE — CSV IMPORT
# ─────────────────────────────────────────────────────────────

_MP_CSV_COLUMNS = [
    'nama', 'sku', 'seller_id', 'category_id', 'satuan',
    'harga', 'harga_grosir', 'min_qty_grosir', 'stok',
    'berat_gram', 'deskripsi', 'aktif',
]


@superadmin_bp.route('/marketplace/products/import/sample.csv')
@superadmin_required
def marketplace_product_import_sample():
    rows = [
        _MP_CSV_COLUMNS,
        ['Beras Premium 5kg', 'BRS-5KG', '1', '1', 'karung', '75000', '70000', '10', '100', '5000', 'Beras premium kualitas terbaik', '1'],
        ['Gula Pasir 1kg', 'GUL-1KG', '1', '2', 'kg', '14000', '', '', '200', '1000', '', '1'],
    ]
    si = StringIO()
    w = csv.writer(si)
    for row in rows:
        w.writerow(row)
    output = '\ufeff' + si.getvalue()
    return Response(
        output,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=sample_marketplace_products.csv'},
    )


@superadmin_bp.route('/marketplace/products/import', methods=['GET', 'POST'])
@superadmin_required
def marketplace_product_import():
    sellers = MarketplaceSeller.query.filter_by(aktif=True).order_by(MarketplaceSeller.nama).all()
    categories = MarketplaceCategory.query.filter_by(aktif=True).order_by(MarketplaceCategory.nama).all()

    if request.method == 'GET':
        return render_template(
            'superadmin/marketplace/product_import.html',
            sellers=sellers,
            categories=categories,
        )

    uploaded = request.files.get('csv_file')
    if not uploaded or not uploaded.filename:
        flash('Pilih file CSV terlebih dahulu.', 'danger')
        return render_template('superadmin/marketplace/product_import.html',
                               sellers=sellers, categories=categories)

    filename = secure_filename(uploaded.filename).lower()
    if not filename.endswith('.csv'):
        flash('Hanya file .csv yang didukung.', 'danger')
        return render_template('superadmin/marketplace/product_import.html',
                               sellers=sellers, categories=categories)

    try:
        content = uploaded.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            uploaded.seek(0)
            content = uploaded.read().decode('latin-1')
        except Exception:
            flash('File tidak dapat dibaca. Pastikan encoding UTF-8.', 'danger')
            return render_template('superadmin/marketplace/product_import.html',
                                   sellers=sellers, categories=categories)

    reader = csv.DictReader(StringIO(content))
    if not reader.fieldnames or 'nama' not in reader.fieldnames or 'harga' not in reader.fieldnames:
        flash('Kolom wajib "nama" dan "harga" tidak ditemukan. Unduh contoh CSV untuk referensi.', 'danger')
        return render_template('superadmin/marketplace/product_import.html',
                               sellers=sellers, categories=categories)

    created_count = 0
    updated_count = 0
    errors = []

    for row_num, row in enumerate(reader, start=2):
        nama = (row.get('nama') or '').strip()
        if not nama:
            errors.append(f'Baris {row_num}: kolom "nama" kosong, dilewati.')
            continue

        try:
            harga = float((row.get('harga') or '0').replace(',', ''))
        except ValueError:
            errors.append(f'Baris {row_num} ({nama}): harga tidak valid.')
            continue

        sku = (row.get('sku') or '').strip() or None

        # Resolve seller_id
        raw_seller = (row.get('seller_id') or '').strip()
        try:
            seller_id = int(raw_seller) if raw_seller else None
        except ValueError:
            seller_id = None

        if not seller_id:
            errors.append(f'Baris {row_num} ({nama}): seller_id tidak valid atau kosong.')
            continue

        seller = MarketplaceSeller.query.get(seller_id)
        if not seller:
            errors.append(f'Baris {row_num} ({nama}): seller_id {seller_id} tidak ditemukan.')
            continue

        # Optional fields
        raw_cat = (row.get('category_id') or '').strip()
        category_id = None
        if raw_cat:
            try:
                category_id = int(raw_cat)
            except ValueError:
                pass

        satuan = (row.get('satuan') or 'pcs').strip() or 'pcs'

        try:
            harga_grosir_raw = (row.get('harga_grosir') or '').strip()
            harga_grosir = float(harga_grosir_raw.replace(',', '')) if harga_grosir_raw else None
        except ValueError:
            harga_grosir = None

        try:
            mqg_raw = (row.get('min_qty_grosir') or '').strip()
            min_qty_grosir = int(mqg_raw) if mqg_raw else None
        except ValueError:
            min_qty_grosir = None

        try:
            stok = int((row.get('stok') or '0').strip())
        except ValueError:
            stok = 0

        try:
            berat_gram = int((row.get('berat_gram') or '0').strip())
        except ValueError:
            berat_gram = 0

        deskripsi = (row.get('deskripsi') or '').strip() or None

        aktif_raw = (row.get('aktif') or '1').strip().lower()
        aktif = aktif_raw not in ('0', 'false', 'tidak', 'no', 'off')

        # Upsert: match by SKU if provided
        product = None
        if sku:
            product = MarketplaceProduct.query.filter_by(sku=sku, seller_id=seller_id).first()

        if product:
            product.nama = nama
            product.harga = harga
            product.harga_grosir = harga_grosir
            product.min_qty_grosir = min_qty_grosir
            product.stok = stok
            product.satuan = satuan
            product.berat_gram = berat_gram
            product.deskripsi = deskripsi
            product.category_id = category_id
            product.aktif = aktif
            updated_count += 1
        else:
            product = MarketplaceProduct(
                seller_id=seller_id,
                category_id=category_id,
                nama=nama,
                sku=sku,
                harga=harga,
                harga_grosir=harga_grosir,
                min_qty_grosir=min_qty_grosir,
                stok=stok,
                satuan=satuan,
                berat_gram=berat_gram,
                deskripsi=deskripsi,
                aktif=aktif,
            )
            db.session.add(product)
            created_count += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f'Gagal menyimpan data: {str(e)}', 'danger')
        return render_template('superadmin/marketplace/product_import.html',
                               sellers=sellers, categories=categories)

    if created_count or updated_count:
        flash(f'Import selesai: {created_count} produk baru ditambahkan, {updated_count} diperbarui.', 'success')
    if errors:
        for e in errors[:10]:
            flash(e, 'warning')
        if len(errors) > 10:
            flash(f'… dan {len(errors) - 10} kesalahan lainnya.', 'warning')

    return redirect(url_for('superadmin.marketplace_products'))


# ─────────────────────────────────────────────────────────────
# MARKETPLACE — REPORTS
# ─────────────────────────────────────────────────────────────

@superadmin_bp.route('/marketplace/reports')
@superadmin_required
def marketplace_reports():
    today = datetime.utcnow()
    year = request.args.get('year', today.year, type=int)
    month = request.args.get('month', today.month, type=int)

    # Date range for selected month
    from calendar import monthrange
    _, days_in_month = monthrange(year, month)
    start_dt = datetime(year, month, 1)
    end_dt = datetime(year, month, days_in_month, 23, 59, 59)

    non_cancelled = MarketplaceOrder.status != 'cancelled'

    # KPIs for selected month
    month_orders = MarketplaceOrder.query.filter(
        MarketplaceOrder.created_at >= start_dt,
        MarketplaceOrder.created_at <= end_dt,
        non_cancelled,
    ).all()
    total_orders_month = len(month_orders)
    total_omzet_month = sum(o.total for o in month_orders)

    active_sellers = db.session.query(func.count(func.distinct(MarketplaceOrder.seller_id))).filter(
        MarketplaceOrder.created_at >= start_dt,
        MarketplaceOrder.created_at <= end_dt,
        non_cancelled,
    ).scalar() or 0

    active_tenants = db.session.query(func.count(func.distinct(MarketplaceOrder.tenant_id))).filter(
        MarketplaceOrder.created_at >= start_dt,
        MarketplaceOrder.created_at <= end_dt,
        non_cancelled,
    ).scalar() or 0

    # Omzet per seller for selected month
    seller_revenue = db.session.query(
        MarketplaceSeller.nama,
        func.count(MarketplaceOrder.id).label('order_count'),
        func.sum(MarketplaceOrder.total).label('omzet'),
    ).join(MarketplaceOrder, MarketplaceOrder.seller_id == MarketplaceSeller.id).filter(
        MarketplaceOrder.created_at >= start_dt,
        MarketplaceOrder.created_at <= end_dt,
        non_cancelled,
    ).group_by(MarketplaceSeller.id, MarketplaceSeller.nama).order_by(
        func.sum(MarketplaceOrder.total).desc()
    ).all()

    # Top 10 best-selling products (all-time or month — using all-time for richer data)
    top_products = db.session.query(
        MarketplaceOrderItem.nama_produk,
        func.sum(MarketplaceOrderItem.qty).label('total_qty'),
        func.sum(MarketplaceOrderItem.subtotal).label('total_omzet'),
    ).join(MarketplaceOrder, MarketplaceOrder.id == MarketplaceOrderItem.order_id).filter(
        MarketplaceOrder.created_at >= start_dt,
        MarketplaceOrder.created_at <= end_dt,
        non_cancelled,
    ).group_by(MarketplaceOrderItem.nama_produk).order_by(
        func.sum(MarketplaceOrderItem.qty).desc()
    ).limit(10).all()

    # Top tenants by order count
    top_tenants = db.session.query(
        Tenant.nama,
        func.count(MarketplaceOrder.id).label('order_count'),
        func.sum(MarketplaceOrder.total).label('total_belanja'),
    ).join(MarketplaceOrder, MarketplaceOrder.tenant_id == Tenant.id).filter(
        MarketplaceOrder.created_at >= start_dt,
        MarketplaceOrder.created_at <= end_dt,
        non_cancelled,
    ).group_by(Tenant.id, Tenant.nama).order_by(
        func.count(MarketplaceOrder.id).desc()
    ).limit(10).all()

    # Daily order trend for selected month
    daily_trend_rows = db.session.query(
        func.date(MarketplaceOrder.created_at).label('day'),
        func.count(MarketplaceOrder.id).label('order_count'),
        func.sum(MarketplaceOrder.total).label('omzet'),
    ).filter(
        MarketplaceOrder.created_at >= start_dt,
        MarketplaceOrder.created_at <= end_dt,
        non_cancelled,
    ).group_by(func.date(MarketplaceOrder.created_at)).order_by(
        func.date(MarketplaceOrder.created_at)
    ).all()

    # Build complete daily series (fill gaps with 0)
    daily_map = {}
    for row in daily_trend_rows:
        key = str(row.day)[:10]
        daily_map[key] = {'count': row.order_count, 'omzet': float(row.omzet or 0)}

    from datetime import date as date_type, timedelta as td
    daily_labels = []
    daily_counts = []
    daily_omzet = []
    cur = start_dt.date()
    end_date = end_dt.date()
    while cur <= end_date:
        key = cur.strftime('%Y-%m-%d')
        daily_labels.append(cur.strftime('%d'))
        daily_counts.append(daily_map.get(key, {}).get('count', 0))
        daily_omzet.append(daily_map.get(key, {}).get('omzet', 0))
        cur += td(days=1)

    # Available years for filter
    min_year_row = db.session.query(func.min(func.extract('year', MarketplaceOrder.created_at))).scalar()
    min_year = int(min_year_row) if min_year_row else today.year
    years = list(range(min_year, today.year + 1))

    return render_template(
        'superadmin/marketplace/reports.html',
        year=year,
        month=month,
        years=years,
        total_orders_month=total_orders_month,
        total_omzet_month=total_omzet_month,
        active_sellers=active_sellers,
        active_tenants=active_tenants,
        seller_revenue=seller_revenue,
        top_products=top_products,
        top_tenants=top_tenants,
        daily_labels=daily_labels,
        daily_counts=daily_counts,
        daily_omzet=daily_omzet,
        days_in_month=days_in_month,
    )
