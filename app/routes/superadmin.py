import csv
import json
import os
import re
import secrets
import string
import shutil as shutil_mod
import subprocess
from io import StringIO
from urllib.parse import parse_qs, unquote, urlparse, urlunparse
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
from ..timezones import (
    INDONESIA_TIMEZONE_CHOICES,
    normalize_timezone_id,
    lead_period_utc_bounds,
    resolve_effective_timezone_id,
    timezone_short_label,
)
from ..models import (
    Tenant,
    User,
    Branch,
    Transaction,
    Product,
    SuperadminAuditLog,
    TenantPackage,
    TenantPlanHistory,
    LeadCapture,
    AppSetting,
    TenantInvoice,
    Announcement,
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


@superadmin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
@superadmin_required
def settings():
    """Zona waktu tampilan untuk akun Super Admin (preferensi pribadi)."""
    if request.method == 'POST':
        current_user.timezone = normalize_timezone_id(request.form.get('timezone'))
        db.session.commit()
        flash('Zona waktu akun Super Admin disimpan.', 'success')
        return redirect(url_for('superadmin.settings'))
    return render_template(
        'superadmin/settings.html',
        timezone_choices=INDONESIA_TIMEZONE_CHOICES,
        current_tz=normalize_timezone_id(getattr(current_user, 'timezone', None)),
    )


def _parse_expiry_date(s):
    if not s or not str(s).strip():
        return None
    try:
        d = datetime.strptime(str(s).strip()[:10], '%Y-%m-%d').date()
        return datetime.combine(d, time(23, 59, 59))
    except ValueError:
        return None


def _parse_date_start_utc(s):
    """Tanggal mulai (awal hari UTC naive) — untuk pengumuman / field 'dari tanggal'."""
    if not s or not str(s).strip():
        return None
    try:
        d = datetime.strptime(str(s).strip()[:10], '%Y-%m-%d').date()
        return datetime.combine(d, time(0, 0, 0))
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
        selectinload(Tenant.lead_source),
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
            timezone=normalize_timezone_id(request.form.get('timezone')),
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
        timezone_choices=INDONESIA_TIMEZONE_CHOICES,
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
        tenant.timezone = normalize_timezone_id(request.form.get('timezone'))

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
        timezone_choices=INDONESIA_TIMEZONE_CHOICES,
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

    from ..models import Supplier
    onboarding_steps = []
    has_products = Product.query.filter_by(tenant_id=id, aktif=True).count() > 0
    has_supplier = Supplier.query.filter_by(tenant_id=id).first() is not None
    has_tx = total_transaksi > 0
    has_multi_user = len([u for u in users if u.aktif]) > 1
    has_multi_branch = len([b for b in branches if b.aktif]) > 1
    onboarding_steps.append({'label': 'Produk ditambahkan', 'done': has_products})
    onboarding_steps.append({'label': 'Supplier ditambahkan', 'done': has_supplier})
    onboarding_steps.append({'label': 'Transaksi pertama', 'done': has_tx})
    onboarding_steps.append({'label': 'Multi-user aktif', 'done': has_multi_user})
    onboarding_steps.append({'label': 'Cabang tambahan', 'done': has_multi_branch})
    onboarding_done = sum(1 for s in onboarding_steps if s['done'])
    onboarding_pct = int(100 * onboarding_done / len(onboarding_steps)) if onboarding_steps else 0

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
        onboarding_steps=onboarding_steps,
        onboarding_done=onboarding_done,
        onboarding_pct=onboarding_pct,
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
@login_required
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
                _log_sa('marketplace_category_add', detail=f'nama={nama}')
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
@login_required
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
@login_required
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
        _log_sa('marketplace_seller_add', detail=f'nama={nama}')
        db.session.commit()
        flash(f'Seller "{nama}" berhasil ditambahkan.', 'success')
        return redirect(url_for('superadmin.marketplace_sellers'))

    return render_template('superadmin/marketplace/seller_form.html', seller=None)


@superadmin_bp.route('/marketplace/sellers/<int:seller_id>/edit', methods=['GET', 'POST'])
@login_required
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

        _log_sa('marketplace_seller_edit', detail=f'seller_id={seller_id}')
        db.session.commit()
        flash('Seller berhasil diperbarui.', 'success')
        return redirect(url_for('superadmin.marketplace_sellers'))

    return render_template('superadmin/marketplace/seller_form.html', seller=seller)


@superadmin_bp.route('/marketplace/sellers/<int:seller_id>/toggle', methods=['POST'])
@login_required
@superadmin_required
def marketplace_seller_toggle(seller_id):
    seller = MarketplaceSeller.query.get_or_404(seller_id)
    seller.aktif = not seller.aktif
    _log_sa('marketplace_seller_toggle', detail=f'seller_id={seller_id} aktif={seller.aktif}')
    db.session.commit()
    flash(f'Seller {"diaktifkan" if seller.aktif else "dinonaktifkan"}.', 'success')
    return redirect(url_for('superadmin.marketplace_sellers'))


# ── PRODUCTS ──────────────────────────────────────────────────

@superadmin_bp.route('/marketplace/products')
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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


# ─────────────────────────────────────────────────────────────
# SUPERADMIN DASHBOARD (Analytics)
# ─────────────────────────────────────────────────────────────

def _platform_monthly_revenue(year, month):
    from calendar import monthrange
    _, dim = monthrange(year, month)
    start = datetime(year, month, 1)
    end = datetime(year, month, dim, 23, 59, 59)
    return db.session.query(
        func.coalesce(func.sum(Transaction.total), 0)
    ).filter(
        Transaction.status == 'selesai',
        Transaction.created_at.between(start, end),
    ).scalar() or 0


def _tenant_health_score(tenant):
    now = datetime.utcnow()
    since_7d = now - timedelta(days=7)
    since_30d = now - timedelta(days=30)
    tx_7d = Transaction.query.filter(
        Transaction.tenant_id == tenant.id,
        Transaction.status == 'selesai',
        Transaction.created_at >= since_7d,
    ).count()
    tx_30d = Transaction.query.filter(
        Transaction.tenant_id == tenant.id,
        Transaction.status == 'selesai',
        Transaction.created_at >= since_30d,
    ).count()
    products = Product.query.filter_by(tenant_id=tenant.id, aktif=True).count()
    users_active = User.query.filter_by(tenant_id=tenant.id, aktif=True).count()

    score = 0
    if tx_7d >= 10:
        score += 40
    elif tx_7d >= 3:
        score += 25
    elif tx_7d >= 1:
        score += 10

    if tx_30d >= 50:
        score += 25
    elif tx_30d >= 15:
        score += 15
    elif tx_30d >= 1:
        score += 5

    if products >= 20:
        score += 20
    elif products >= 5:
        score += 10

    if users_active >= 2:
        score += 15
    elif users_active >= 1:
        score += 5

    return min(100, score), tx_7d, tx_30d


@superadmin_bp.route('/dashboard')
@login_required
@superadmin_required
def sa_dashboard():
    now = datetime.utcnow()
    today = now.date()
    start_today = datetime.combine(today, datetime.min.time())
    end_today = datetime.combine(today, datetime.max.time())

    total_tenants = Tenant.query.count()
    tenants_aktif = Tenant.query.filter_by(aktif=True).count()
    tenants_nonaktif = total_tenants - tenants_aktif

    month_start = datetime(now.year, now.month, 1)
    tenants_baru_bulan = Tenant.query.filter(
        Tenant.tanggal_daftar >= month_start
    ).count()

    revenue_today = db.session.query(
        func.coalesce(func.sum(Transaction.total), 0)
    ).filter(
        Transaction.status == 'selesai',
        Transaction.created_at.between(start_today, end_today),
    ).scalar() or 0

    revenue_month = _platform_monthly_revenue(now.year, now.month)

    expiring_soon = Tenant.query.filter(
        Tenant.aktif.is_(True),
        Tenant.tanggal_expired.isnot(None),
        Tenant.tanggal_expired > now,
        Tenant.tanggal_expired <= now + timedelta(days=14),
    ).order_by(Tenant.tanggal_expired.asc()).all()

    expired_tenants = Tenant.query.filter(
        Tenant.aktif.is_(True),
        Tenant.tanggal_expired.isnot(None),
        Tenant.tanggal_expired < now,
    ).count()

    top_5 = db.session.query(
        Tenant.id, Tenant.nama, Tenant.kode,
        func.coalesce(func.sum(Transaction.total), 0).label('omzet'),
    ).join(Transaction, Transaction.tenant_id == Tenant.id).filter(
        Transaction.status == 'selesai',
        Transaction.created_at >= month_start,
    ).group_by(Tenant.id, Tenant.nama, Tenant.kode).order_by(
        func.sum(Transaction.total).desc()
    ).limit(5).all()

    chart_30d_labels = []
    chart_30d_data = []
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        s = datetime.combine(d, datetime.min.time())
        e = datetime.combine(d, datetime.max.time())
        rev = db.session.query(
            func.coalesce(func.sum(Transaction.total), 0)
        ).filter(
            Transaction.status == 'selesai',
            Transaction.created_at.between(s, e),
        ).scalar() or 0
        chart_30d_labels.append(d.strftime('%d/%m'))
        chart_30d_data.append(float(rev))

    chart_growth_labels = []
    chart_growth_data = []
    for i in range(5, -1, -1):
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        from calendar import monthrange
        _, dim = monthrange(y, m)
        ms = datetime(y, m, 1)
        me = datetime(y, m, dim, 23, 59, 59)
        cnt = Tenant.query.filter(
            Tenant.tanggal_daftar.between(ms, me)
        ).count()
        chart_growth_labels.append(ms.strftime('%b %Y'))
        chart_growth_data.append(cnt)

    mp_pending = MarketplaceOrder.query.filter_by(status='pending').count()

    invoices_overdue = TenantInvoice.query.filter_by(status='overdue').count()
    invoices_unpaid = TenantInvoice.query.filter_by(status='unpaid').count()

    leads_new = LeadCapture.query.filter_by(status='new').count()

    return render_template(
        'superadmin/dashboard.html',
        total_tenants=total_tenants,
        tenants_aktif=tenants_aktif,
        tenants_nonaktif=tenants_nonaktif,
        tenants_baru_bulan=tenants_baru_bulan,
        revenue_today=revenue_today,
        revenue_month=revenue_month,
        expiring_soon=expiring_soon,
        expired_tenants=expired_tenants,
        top_5=top_5,
        chart_30d_labels=json.dumps(chart_30d_labels),
        chart_30d_data=json.dumps(chart_30d_data),
        chart_growth_labels=json.dumps(chart_growth_labels),
        chart_growth_data=json.dumps(chart_growth_data),
        mp_pending=mp_pending,
        invoices_overdue=invoices_overdue,
        invoices_unpaid=invoices_unpaid,
        leads_new=leads_new,
    )


# ─────────────────────────────────────────────────────────────
# LEAD CAPTURE
# ─────────────────────────────────────────────────────────────

LEAD_STATUSES = [
    ('new', 'Baru'),
    ('contacted', 'Dihubungi'),
    ('converted', 'Dikonversi'),
    ('rejected', 'Ditolak'),
]

LEAD_PERIODS = [
    ('all', 'Semua waktu'),
    ('today', 'Hari ini'),
    ('yesterday', 'Kemarin'),
    ('week', 'Minggu ini'),
    ('month', 'Bulan ini'),
    ('year', 'Tahun ini'),
    ('last7', '7 hari terakhir'),
    ('last30', '30 hari terakhir'),
]


def _leads_apply_period(query, period: str, tz_id: str):
    b = lead_period_utc_bounds(period, tz_id)
    if not b:
        return query
    query = query.filter(LeadCapture.created_at >= b['gte'])
    if 'lte' in b:
        query = query.filter(LeadCapture.created_at <= b['lte'])
    return query


def _redirect_leads_back():
    ref = request.referrer
    if ref and ref.startswith(request.host_url):
        return redirect(ref)
    return redirect(url_for('superadmin.leads_index'))


@superadmin_bp.route('/leads')
@login_required
@superadmin_required
def leads_index():
    page = max(1, int(request.args.get('page', 1) or 1))
    status_filter = request.args.get('status', '').strip()
    q = (request.args.get('q') or '').strip()
    period_filter = (request.args.get('period') or 'all').strip().lower()
    valid_periods = {p for p, _ in LEAD_PERIODS}
    if period_filter not in valid_periods:
        period_filter = 'all'

    tz_id = resolve_effective_timezone_id(current_user)

    def _base_leads_query():
        qq = LeadCapture.query
        if status_filter:
            qq = qq.filter_by(status=status_filter)
        if q:
            like = f'%{q}%'
            qq = qq.filter(
                or_(
                    LeadCapture.nama.ilike(like),
                    LeadCapture.no_wa.ilike(like),
                    LeadCapture.jenis_usaha.ilike(like),
                )
            )
        qq = _leads_apply_period(qq, period_filter, tz_id)
        return qq

    query = _base_leads_query().order_by(LeadCapture.created_at.desc())
    total = query.count()
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    if page > total_pages:
        page = total_pages
    leads = query.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()

    def _count_status(st: str):
        qq = LeadCapture.query.filter_by(status=st)
        if q:
            like = f'%{q}%'
            qq = qq.filter(
                or_(
                    LeadCapture.nama.ilike(like),
                    LeadCapture.no_wa.ilike(like),
                    LeadCapture.jenis_usaha.ilike(like),
                )
            )
        qq = _leads_apply_period(qq, period_filter, tz_id)
        return qq.count()

    stats = {
        'new': _count_status('new'),
        'contacted': _count_status('contacted'),
        'converted': _count_status('converted'),
        'rejected': _count_status('rejected'),
    }

    period_counts = {}
    for pkey, _ in LEAD_PERIODS:
        qq = LeadCapture.query
        qq = _leads_apply_period(qq, pkey, tz_id)
        period_counts[pkey] = qq.count()

    def _leads_url(
        period=None,
        status=None,
        page_num=None,
        drop_status=False,
    ):
        """Bangun URL /superadmin/leads dengan filter periode + status + q + halaman."""
        args = {}
        if q:
            args['q'] = q
        pf = period_filter if period is None else period
        if pf and pf != 'all':
            args['period'] = pf
        if not drop_status:
            st = status if status is not None else status_filter
            if st:
                args['status'] = st
        pn = page if page_num is None else page_num
        if pn is not None and pn > 1:
            args['page'] = pn
        return url_for('superadmin.leads_index', **args)

    def _leads_url_period(pcode: str):
        return _leads_url(period=pcode)

    lead_period_links = []
    for pcode, plabel in LEAD_PERIODS:
        lead_period_links.append(
            {
                'code': pcode,
                'label': plabel,
                'count': period_counts[pcode],
                'url': _leads_url_period(pcode),
                'active': period_filter == pcode,
            }
        )

    lead_status_links = []
    for code, label in LEAD_STATUSES:
        lead_status_links.append(
            {
                'code': code,
                'label': label,
                'url': _leads_url(status=code),
                'active': status_filter == code,
            }
        )

    leads_url_clear_status = _leads_url(drop_status=True) if status_filter else None

    leads_pagination = None
    if total_pages > 1:
        leads_pagination = {
            'prev': _leads_url(page_num=page - 1) if page > 1 else None,
            'next': _leads_url(page_num=page + 1) if page < total_pages else None,
        }

    return render_template(
        'superadmin/leads_index.html',
        leads=leads,
        page=page,
        total_pages=total_pages,
        total=total,
        q=q,
        status_filter=status_filter,
        lead_statuses=LEAD_STATUSES,
        stats=stats,
        period_filter=period_filter,
        lead_period_links=lead_period_links,
        lead_status_links=lead_status_links,
        leads_tz_short=timezone_short_label(tz_id),
        leads_pagination=leads_pagination,
        leads_url_clear_status=leads_url_clear_status,
    )


@superadmin_bp.route('/leads/<int:id>/status', methods=['POST'])
@login_required
@superadmin_required
def lead_update_status(id):
    lead = LeadCapture.query.get_or_404(id)
    new_status = request.form.get('status', '').strip()
    catatan = request.form.get('catatan_admin', '').strip()
    valid = [s for s, _ in LEAD_STATUSES]
    if new_status in valid:
        lead.status = new_status
    if catatan:
        lead.catatan_admin = catatan
    _log_sa('lead_status_change', detail=f'lead_id={id} status={new_status}')
    db.session.commit()
    flash('Status lead diperbarui.', 'success')
    return _redirect_leads_back()


@superadmin_bp.route('/leads/<int:id>/convert', methods=['POST'])
@login_required
@superadmin_required
def lead_convert(id):
    lead = LeadCapture.query.get_or_404(id)
    if lead.trial_tenant_id:
        flash('Lead sudah pernah dikonversi.', 'warning')
        return _redirect_leads_back()
    flash(
        f'Silakan buat tenant baru. Data lead: {lead.nama} / {lead.jenis_usaha or "-"}',
        'info',
    )
    return redirect(url_for('superadmin.add_tenant'))


@superadmin_bp.route('/leads/<int:id>/delete', methods=['POST'])
@login_required
@superadmin_required
def lead_delete(id):
    lead = LeadCapture.query.get_or_404(id)
    db.session.delete(lead)
    _log_sa('lead_delete', detail=f'lead_id={id} nama={lead.nama}')
    db.session.commit()
    flash('Lead dihapus.', 'success')
    return _redirect_leads_back()


# ─────────────────────────────────────────────────────────────
# PLATFORM SETTINGS
# ─────────────────────────────────────────────────────────────

PLATFORM_SETTING_KEYS = [
    ('platform_name', 'Nama Platform', 'Kasir Sembako'),
    ('default_grace_days', 'Grace Days Setelah Expired', '0'),
    ('expired_mode', 'Mode Expired (block_login / read_only)', 'block_login'),
    ('default_tenant_timezone', 'Default Timezone Tenant Baru', 'Asia/Jakarta'),
    ('maintenance_mode', 'Maintenance Mode (0/1)', '0'),
    ('smtp_host', 'SMTP Host', ''),
    ('smtp_port', 'SMTP Port', '587'),
    ('smtp_user', 'SMTP Username', ''),
    ('smtp_pass', 'SMTP Password', ''),
    ('wa_gateway_url', 'WhatsApp Gateway URL', ''),
    ('wa_gateway_token', 'WhatsApp Gateway Token', ''),
    ('notif_expiring_days', 'Notif Tenant Expiring (hari sebelum)', '7'),
]


@superadmin_bp.route('/platform-settings', methods=['GET', 'POST'])
@login_required
@superadmin_required
def platform_settings():
    if request.method == 'POST':
        for key, label, default in PLATFORM_SETTING_KEYS:
            val = request.form.get(key, '').strip()
            AppSetting.set(key, val)
        _log_sa('platform_settings_update')
        db.session.commit()
        flash('Pengaturan platform disimpan.', 'success')
        return redirect(url_for('superadmin.platform_settings'))

    current_settings = {}
    for key, label, default in PLATFORM_SETTING_KEYS:
        current_settings[key] = AppSetting.get(key, default)

    return render_template(
        'superadmin/platform_settings.html',
        setting_keys=PLATFORM_SETTING_KEYS,
        current_settings=current_settings,
    )


# ─────────────────────────────────────────────────────────────
# TENANT HEALTH SCORES
# ─────────────────────────────────────────────────────────────

@superadmin_bp.route('/tenant-health')
@login_required
@superadmin_required
def tenant_health():
    tenants = Tenant.query.filter_by(aktif=True).order_by(Tenant.nama).all()
    health_data = []
    for t in tenants:
        score, tx_7d, tx_30d = _tenant_health_score(t)
        if score >= 60:
            level = 'healthy'
        elif score >= 30:
            level = 'warning'
        else:
            level = 'danger'
        health_data.append({
            'tenant': t,
            'score': score,
            'level': level,
            'tx_7d': tx_7d,
            'tx_30d': tx_30d,
        })
    health_data.sort(key=lambda x: x['score'])

    filter_level = request.args.get('level', '').strip()
    if filter_level:
        health_data = [h for h in health_data if h['level'] == filter_level]

    return render_template(
        'superadmin/tenant_health.html',
        health_data=health_data,
        filter_level=filter_level,
    )


# ─────────────────────────────────────────────────────────────
# BILLING / INVOICE
# ─────────────────────────────────────────────────────────────

def _generate_invoice_number():
    now = datetime.utcnow()
    prefix = f'INV-{now.strftime("%Y%m")}'
    last = TenantInvoice.query.filter(
        TenantInvoice.nomor.like(f'{prefix}%')
    ).order_by(TenantInvoice.id.desc()).first()
    if last:
        try:
            seq = int(last.nomor.split('-')[-1]) + 1
        except (ValueError, IndexError):
            seq = 1
    else:
        seq = 1
    return f'{prefix}-{seq:04d}'


@superadmin_bp.route('/billing')
@login_required
@superadmin_required
def billing_index():
    page = max(1, int(request.args.get('page', 1) or 1))
    status_filter = request.args.get('status', '').strip()
    tid = request.args.get('tenant', type=int)

    query = TenantInvoice.query
    if status_filter:
        query = query.filter_by(status=status_filter)
    if tid:
        query = query.filter_by(tenant_id=tid)
    query = query.order_by(TenantInvoice.created_at.desc())

    total = query.count()
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    if page > total_pages:
        page = total_pages
    invoices = query.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()

    stats = {
        'unpaid': TenantInvoice.query.filter_by(status='unpaid').count(),
        'paid': TenantInvoice.query.filter_by(status='paid').count(),
        'overdue': TenantInvoice.query.filter_by(status='overdue').count(),
        'total_unpaid': db.session.query(
            func.coalesce(func.sum(TenantInvoice.nominal), 0)
        ).filter(TenantInvoice.status.in_(['unpaid', 'overdue'])).scalar() or 0,
    }

    tenants = Tenant.query.order_by(Tenant.nama).all()

    return render_template(
        'superadmin/billing_index.html',
        invoices=invoices,
        page=page,
        total_pages=total_pages,
        total=total,
        status_filter=status_filter,
        tenant_filter=tid,
        stats=stats,
        tenants=tenants,
    )


@superadmin_bp.route('/billing/add', methods=['GET', 'POST'])
@login_required
@superadmin_required
def billing_add():
    if request.method == 'POST':
        tid = request.form.get('tenant_id', type=int)
        tenant = Tenant.query.get(tid) if tid else None
        if not tenant:
            flash('Pilih tenant.', 'danger')
            return redirect(url_for('superadmin.billing_add'))

        nominal = 0
        try:
            nominal = float(request.form.get('nominal', 0))
        except (TypeError, ValueError):
            pass

        inv = TenantInvoice(
            tenant_id=tid,
            nomor=_generate_invoice_number(),
            nominal=nominal,
            periode_mulai=_parse_expiry_date(request.form.get('periode_mulai')),
            periode_akhir=_parse_expiry_date(request.form.get('periode_akhir')),
            catatan=request.form.get('catatan', '').strip() or None,
            status='unpaid',
            created_by=current_user.id,
        )
        db.session.add(inv)
        _log_sa('invoice_create', tid, detail=f'nomor={inv.nomor} nominal={nominal}')
        db.session.commit()
        flash(f'Invoice {inv.nomor} dibuat.', 'success')
        return redirect(url_for('superadmin.billing_index'))

    tenants = Tenant.query.filter_by(aktif=True).order_by(Tenant.nama).all()
    return render_template('superadmin/billing_form.html', invoice=None, tenants=tenants)


@superadmin_bp.route('/billing/<int:id>/pay', methods=['POST'])
@login_required
@superadmin_required
def billing_pay(id):
    inv = TenantInvoice.query.get_or_404(id)
    inv.status = 'paid'
    inv.tanggal_bayar = datetime.utcnow()
    inv.metode_bayar = request.form.get('metode_bayar', 'transfer').strip()
    _log_sa('invoice_paid', inv.tenant_id, detail=f'nomor={inv.nomor}')
    db.session.commit()
    flash(f'Invoice {inv.nomor} ditandai lunas.', 'success')
    return redirect(url_for('superadmin.billing_index'))


@superadmin_bp.route('/billing/<int:id>/cancel', methods=['POST'])
@login_required
@superadmin_required
def billing_cancel(id):
    inv = TenantInvoice.query.get_or_404(id)
    inv.status = 'cancelled'
    _log_sa('invoice_cancel', inv.tenant_id, detail=f'nomor={inv.nomor}')
    db.session.commit()
    flash(f'Invoice {inv.nomor} dibatalkan.', 'success')
    return redirect(url_for('superadmin.billing_index'))


# ─────────────────────────────────────────────────────────────
# NOTIFICATIONS (send via configured gateway)
# ─────────────────────────────────────────────────────────────

def _send_wa_message(phone, message):
    url = AppSetting.get('wa_gateway_url', '')
    token = AppSetting.get('wa_gateway_token', '')
    if not url or not token:
        return False, 'WhatsApp gateway belum dikonfigurasi'
    import urllib.request
    data = json.dumps({'phone': phone, 'message': message, 'token': token}).encode()
    req = Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=15) as resp:
            return True, resp.read().decode()
    except Exception as e:
        return False, str(e)


def _send_email_message(to_email, subject, body):
    host = AppSetting.get('smtp_host', '')
    port = int(AppSetting.get('smtp_port', '587') or 587)
    user = AppSetting.get('smtp_user', '')
    passwd = AppSetting.get('smtp_pass', '')
    if not host or not user:
        return False, 'SMTP belum dikonfigurasi'
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = user
    msg['To'] = to_email
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            s.login(user, passwd)
            s.send_message(msg)
        return True, 'Sent'
    except Exception as e:
        return False, str(e)


@superadmin_bp.route('/notifications/send-expiring', methods=['POST'])
@login_required
@superadmin_required
def notif_send_expiring():
    days = int(AppSetting.get('notif_expiring_days', '7') or 7)
    now = datetime.utcnow()
    expiring = Tenant.query.filter(
        Tenant.aktif.is_(True),
        Tenant.tanggal_expired.isnot(None),
        Tenant.tanggal_expired > now,
        Tenant.tanggal_expired <= now + timedelta(days=days),
    ).all()

    sent = 0
    for t in expiring:
        msg = (
            f'Halo {t.nama}, langganan Kasir Sembako Anda akan berakhir pada '
            f'{t.tanggal_expired.strftime("%d/%m/%Y")}. Segera perpanjang agar layanan tidak terhenti.'
        )
        if t.telepon:
            ok, _ = _send_wa_message(t.telepon, msg)
            if ok:
                sent += 1
        if t.email:
            ok, _ = _send_email_message(t.email, 'Perpanjangan Langganan', msg)
            if ok:
                sent += 1

    _log_sa('notif_expiring_sent', detail=f'tenant_count={len(expiring)} sent={sent}')
    db.session.commit()
    flash(f'Notifikasi dikirim ke {sent} kontak dari {len(expiring)} tenant.', 'success')
    return redirect(url_for('superadmin.sa_dashboard'))


# ─────────────────────────────────────────────────────────────
# BULK ACTIONS
# ─────────────────────────────────────────────────────────────

@superadmin_bp.route('/bulk/toggle', methods=['POST'])
@login_required
@superadmin_required
def bulk_toggle():
    ids = request.form.getlist('tenant_ids')
    action = request.form.get('action', 'deactivate')
    count = 0
    for tid in ids:
        try:
            t = Tenant.query.get(int(tid))
            if t:
                t.aktif = (action == 'activate')
                _log_sa('bulk_toggle', t.id, detail=f'aktif={t.aktif}')
                count += 1
        except (TypeError, ValueError):
            continue
    db.session.commit()
    flash(f'{count} tenant di-{"aktifkan" if action == "activate" else "nonaktifkan"}.', 'success')
    return redirect(url_for('superadmin.index'))


@superadmin_bp.route('/bulk/extend', methods=['POST'])
@login_required
@superadmin_required
def bulk_extend():
    ids = request.form.getlist('tenant_ids')
    try:
        days = int(request.form.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    count = 0
    for tid in ids:
        try:
            t = Tenant.query.get(int(tid))
            if t:
                base = t.tanggal_expired or datetime.utcnow()
                if base < datetime.utcnow():
                    base = datetime.utcnow()
                t.tanggal_expired = base + timedelta(days=days)
                _log_sa('bulk_extend', t.id, detail=f'+{days}d')
                count += 1
        except (TypeError, ValueError):
            continue
    db.session.commit()
    flash(f'{count} tenant diperpanjang +{days} hari.', 'success')
    return redirect(url_for('superadmin.index'))


@superadmin_bp.route('/bulk/export-selected', methods=['POST'])
@login_required
@superadmin_required
def bulk_export_selected():
    ids = request.form.getlist('tenant_ids')
    tenants = Tenant.query.filter(Tenant.id.in_([int(x) for x in ids if x.isdigit()])).all()
    omzet_today = _omzet_today_by_tenant()
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(['nama', 'kode', 'paket', 'email', 'telepon', 'aktif', 'omzet_hari_ini'])
    for t in tenants:
        w.writerow([
            t.nama, t.kode, t.paket, t.email or '', t.telepon or '',
            'ya' if t.aktif else 'tidak',
            round(omzet_today.get(t.id, 0), 2),
        ])
    _log_sa('bulk_export', detail=f'count={len(tenants)}')
    db.session.commit()
    return Response(
        buf.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=tenants_selected.csv'},
    )


# ─────────────────────────────────────────────────────────────
# DATABASE BACKUP
# ─────────────────────────────────────────────────────────────

def _superadmin_backup_dir():
    """Folder backups/ di root proyek (satu tingkat di atas paket app)."""
    from flask import current_app

    root = os.path.abspath(os.path.join(current_app.root_path, os.pardir))
    return os.path.join(root, 'backups')


def _mask_database_uri(uri):
    """Sembunyikan password di URI untuk ditampilkan di halaman."""
    if not uri or not isinstance(uri, str):
        return ''
    try:
        p = urlparse(uri)
        if not p.netloc or '@' not in p.netloc:
            return uri
        userinfo, hostpart = p.netloc.rsplit('@', 1)
        if ':' not in userinfo:
            return uri
        user, _pwd = userinfo.split(':', 1)
        masked_netloc = f'{user}:***@{hostpart}'
        return urlunparse((p.scheme, masked_netloc, p.path, p.params, p.query, p.fragment))
    except Exception:
        return uri


def _database_engine_kind(uri):
    if not uri:
        return 'other'
    low = uri.lower()
    if low.startswith('sqlite'):
        return 'sqlite'
    if 'postgresql' in low or low.startswith('postgres:'):
        return 'postgresql'
    return 'other'


def _parse_postgres_connection(uri):
    """Ambil host, port, user, password, dbname, sslmode dari SQLAlchemy URI."""
    u = (uri or '').replace('postgresql+psycopg2', 'postgresql').replace('postgres://', 'postgresql://')
    parsed = urlparse(u)
    if parsed.scheme not in ('postgresql', 'postgres'):
        return None
    path = (parsed.path or '').strip('/')
    dbname = path.split('/')[0] if path else ''
    if not dbname:
        return None
    user = unquote(parsed.username) if parsed.username else ''
    password = unquote(parsed.password) if parsed.password else ''
    host = parsed.hostname
    port = parsed.port
    qs = parse_qs(parsed.query or '')
    sslmode = (qs.get('sslmode') or [''])[0] or None
    return {
        'user': user,
        'password': password,
        'host': host,
        'port': port,
        'dbname': dbname,
        'sslmode': sslmode,
    }


def _find_pg_dump():
    """Lokasi pg_dump. Systemd sering set PATH hanya ke venv, jadi cek path standar juga."""
    found = shutil_mod.which('pg_dump')
    if found and os.path.isfile(found) and os.access(found, os.X_OK):
        return found
    for candidate in (
        '/usr/bin/pg_dump',
        '/usr/local/bin/pg_dump',
        '/snap/bin/pg_dump',
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


@superadmin_bp.route('/backup')
@login_required
@superadmin_required
def backup_page():
    from flask import current_app

    raw_uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    db_path = _mask_database_uri(raw_uri)
    db_kind = _database_engine_kind(raw_uri)
    pg_dump_ok = bool(_find_pg_dump()) if db_kind == 'postgresql' else False

    backup_dir = _superadmin_backup_dir()
    backups = []
    if os.path.isdir(backup_dir):
        for f in sorted(os.listdir(backup_dir), reverse=True):
            if f.endswith('.db') or f.endswith('.sql') or f.endswith('.gz'):
                fp = os.path.join(backup_dir, f)
                if not os.path.isfile(fp):
                    continue
                sz = os.path.getsize(fp)
                backups.append({
                    'name': f,
                    'size': f'{sz / 1024 / 1024:.2f} MB' if sz > 1048576 else f'{sz / 1024:.1f} KB',
                    'date': datetime.fromtimestamp(os.path.getmtime(fp)).strftime('%d/%m/%Y %H:%M'),
                })
    return render_template(
        'superadmin/backup.html',
        db_path=db_path,
        backups=backups,
        db_kind=db_kind,
        pg_dump_ok=pg_dump_ok,
    )


@superadmin_bp.route('/backup/create', methods=['POST'])
@login_required
@superadmin_required
def backup_create():
    from flask import current_app

    db_uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    backup_dir = _superadmin_backup_dir()
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')

    if _database_engine_kind(db_uri) == 'sqlite':
        if 'sqlite' not in db_uri:
            flash('Konfigurasi database SQLite tidak dikenali.', 'danger')
            return redirect(url_for('superadmin.backup_page'))
        db_file = db_uri.replace('sqlite:///', '')
        if not os.path.isfile(db_file):
            flash('File database tidak ditemukan.', 'danger')
            return redirect(url_for('superadmin.backup_page'))
        backup_name = f'backup_{ts}.db'
        shutil_mod.copy2(db_file, os.path.join(backup_dir, backup_name))
        _log_sa('backup_create', detail=backup_name)
        db.session.commit()
        flash(f'Backup berhasil: {backup_name}', 'success')
        return redirect(url_for('superadmin.backup_page'))

    if _database_engine_kind(db_uri) == 'postgresql':
        pg_dump = _find_pg_dump()
        if not pg_dump:
            flash(
                'Perintah pg_dump tidak ditemukan di server. Pasang client PostgreSQL '
                '(paket postgresql-client) agar backup dari panel bisa jalan.',
                'danger',
            )
            return redirect(url_for('superadmin.backup_page'))
        params = _parse_postgres_connection(db_uri)
        if not params:
            flash('URI PostgreSQL tidak valid; tidak bisa membuat backup.', 'danger')
            return redirect(url_for('superadmin.backup_page'))

        env = os.environ.copy()
        if params['password']:
            env['PGPASSWORD'] = params['password']
        if params.get('sslmode'):
            env['PGSSLMODE'] = params['sslmode']

        backup_name = f'backup_{ts}.sql'
        tmp_name = f'.{backup_name}.tmp'
        out_tmp = os.path.join(backup_dir, tmp_name)
        out_final = os.path.join(backup_dir, backup_name)

        cmd = [pg_dump, '-F', 'p', '--no-owner', '--no-acl', '-f', out_tmp]
        if params.get('host'):
            cmd.extend(['-h', params['host']])
        if params.get('port'):
            cmd.extend(['-p', str(params['port'])])
        if params.get('user'):
            cmd.extend(['-U', params['user']])
        cmd.append(params['dbname'])

        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            if os.path.isfile(out_tmp):
                try:
                    os.remove(out_tmp)
                except OSError:
                    pass
            flash('Backup dibatalkan: waktu habis (database sangat besar).', 'danger')
            return redirect(url_for('superadmin.backup_page'))
        except OSError as e:
            flash(f'Gagal menjalankan pg_dump: {e}', 'danger')
            return redirect(url_for('superadmin.backup_page'))

        if proc.returncode != 0:
            if os.path.isfile(out_tmp):
                try:
                    os.remove(out_tmp)
                except OSError:
                    pass
            err = (proc.stderr or proc.stdout or 'tanpa pesan').strip()
            flash(f'pg_dump gagal: {err[:800]}', 'danger')
            return redirect(url_for('superadmin.backup_page'))

        try:
            os.replace(out_tmp, out_final)
        except OSError as e:
            flash(f'Gagal menyimpan file backup: {e}', 'danger')
            return redirect(url_for('superadmin.backup_page'))

        _log_sa('backup_create', detail=backup_name)
        db.session.commit()
        flash(f'Backup PostgreSQL berhasil: {backup_name}', 'success')
        return redirect(url_for('superadmin.backup_page'))

    flash('Jenis database ini belum didukung untuk backup dari panel.', 'warning')
    return redirect(url_for('superadmin.backup_page'))


@superadmin_bp.route('/backup/download/<filename>')
@login_required
@superadmin_required
def backup_download(filename):
    from flask import send_from_directory

    backup_dir = _superadmin_backup_dir()
    safe_name = secure_filename(filename)
    return send_from_directory(backup_dir, safe_name, as_attachment=True)


# ─────────────────────────────────────────────────────────────
# MULTI-SUPERADMIN MANAGEMENT
# ─────────────────────────────────────────────────────────────

@superadmin_bp.route('/admins')
@login_required
@superadmin_required
def superadmin_users():
    admins = User.query.filter_by(role='superadmin').order_by(User.id).all()
    return render_template('superadmin/superadmin_users.html', admins=admins)


@superadmin_bp.route('/admins/add', methods=['POST'])
@login_required
@superadmin_required
def superadmin_user_add():
    username = (request.form.get('username') or '').strip()
    nama = (request.form.get('nama') or '').strip()
    password = (request.form.get('password') or '').strip()
    if not username or not nama or len(password) < 6:
        flash('Username, nama, dan password (min 6 karakter) wajib diisi.', 'danger')
        return redirect(url_for('superadmin.superadmin_users'))
    if User.query.filter_by(username=username).first():
        flash('Username sudah dipakai.', 'danger')
        return redirect(url_for('superadmin.superadmin_users'))
    u = User(username=username, nama=nama, role='superadmin')
    u.set_password(password)
    db.session.add(u)
    _log_sa('superadmin_user_create', detail=f'username={username}')
    db.session.commit()
    flash(f'Akun Super Admin "{username}" dibuat.', 'success')
    return redirect(url_for('superadmin.superadmin_users'))


@superadmin_bp.route('/admins/<int:id>/toggle', methods=['POST'])
@login_required
@superadmin_required
def superadmin_user_toggle(id):
    u = User.query.get_or_404(id)
    if u.id == current_user.id:
        flash('Tidak bisa menonaktifkan akun sendiri.', 'danger')
        return redirect(url_for('superadmin.superadmin_users'))
    u.aktif = not u.aktif
    _log_sa('superadmin_user_toggle', detail=f'user_id={id} aktif={u.aktif}')
    db.session.commit()
    flash(f'Akun "{u.username}" {"diaktifkan" if u.aktif else "dinonaktifkan"}.', 'success')
    return redirect(url_for('superadmin.superadmin_users'))


# ─────────────────────────────────────────────────────────────
# ANNOUNCEMENTS
# ─────────────────────────────────────────────────────────────

@superadmin_bp.route('/announcements')
@login_required
@superadmin_required
def announcements_index():
    announcements = Announcement.query.order_by(Announcement.created_at.desc()).all()
    return render_template('superadmin/announcements.html', announcements=announcements)


@superadmin_bp.route('/announcements/add', methods=['POST'])
@login_required
@superadmin_required
def announcement_add():
    judul = (request.form.get('judul') or '').strip()
    isi = (request.form.get('isi') or '').strip()
    if not judul or not isi:
        flash('Judul dan isi wajib diisi.', 'danger')
        return redirect(url_for('superadmin.announcements_index'))
    a = Announcement(
        judul=judul,
        isi=isi,
        tipe=request.form.get('tipe', 'info').strip() or 'info',
        target=request.form.get('target', 'all').strip() or 'all',
        tanggal_mulai=_parse_date_start_utc(request.form.get('tanggal_mulai')) or datetime.utcnow(),
        tanggal_selesai=_parse_expiry_date(request.form.get('tanggal_selesai')),
        created_by=current_user.id,
    )
    db.session.add(a)
    _log_sa('announcement_create', detail=judul[:80])
    db.session.commit()
    flash('Pengumuman dibuat.', 'success')
    return redirect(url_for('superadmin.announcements_index'))


@superadmin_bp.route('/announcements/<int:id>/toggle', methods=['POST'])
@login_required
@superadmin_required
def announcement_toggle(id):
    a = Announcement.query.get_or_404(id)
    a.aktif = not a.aktif
    db.session.commit()
    flash(f'Pengumuman {"diaktifkan" if a.aktif else "dinonaktifkan"}.', 'success')
    return redirect(url_for('superadmin.announcements_index'))


@superadmin_bp.route('/announcements/<int:id>/delete', methods=['POST'])
@login_required
@superadmin_required
def announcement_delete(id):
    a = Announcement.query.get_or_404(id)
    db.session.delete(a)
    _log_sa('announcement_delete', detail=f'id={id}')
    db.session.commit()
    flash('Pengumuman dihapus.', 'success')
    return redirect(url_for('superadmin.announcements_index'))
