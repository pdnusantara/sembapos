import os
import re
import uuid
from datetime import datetime
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from sqlalchemy import or_, func
from sqlalchemy.exc import IntegrityError

from .. import db
from ..models import User, Branch, Tenant, UserAuditLog, Transaction
from ..permissions import (
    parse_perm_form,
    normalize_permissions_json,
    effective_permission_codes,
)
from ..timezones import INDONESIA_TIMEZONE_CHOICES, normalize_timezone_id

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

MIN_PASSWORD_LEN = 8
USER_PAGE_SIZE = 15
USERNAME_RE = re.compile(r'^[a-zA-Z0-9_]{3,50}$')


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.role not in ['superadmin', 'admin']:
            flash('Akses ditolak! Hanya admin yang bisa mengakses halaman ini.', 'danger')
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated


def log_user_audit(tenant_id, actor_id, action, target_user_id=None, detail=None):
    db.session.add(UserAuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor_id,
        action=action,
        target_user_id=target_user_id,
        detail=(detail[:2000] if detail else None),
    ))


def other_active_admins_count(tenant_id, exclude_user_id):
    return User.query.filter(
        User.tenant_id == tenant_id,
        User.id != exclude_user_id,
        User.role == 'admin',
        User.aktif.is_(True),
    ).count()


def validate_username(username):
    u = (username or '').strip()
    if not USERNAME_RE.match(u):
        return None, 'Username 3–50 karakter: huruf, angka, underscore (_).'
    return u, None


def _compute_permissions_from_form(role):
    """Return (error_message_or_None, permissions_json_value_or_None)."""
    if not request.form.get('use_custom_perms'):
        return None, None
    plist = parse_perm_form(request.form)
    if not plist:
        return 'Pilih minimal satu modul yang boleh diakses.', None
    if set(plist) <= {'dashboard'}:
        return 'Pilih minimal satu modul selain Dashboard (mis. Kasir / POS).', None
    return None, normalize_permissions_json(role, plist)


def validate_password_pair(pw, pw2):
    if not pw and not pw2:
        return None, None
    if not pw or not pw2:
        return None, 'Isi password baru dan konfirmasi.'
    if pw != pw2:
        return None, 'Password dan konfirmasi tidak sama.'
    if len(pw) < MIN_PASSWORD_LEN:
        return None, f'Password minimal {MIN_PASSWORD_LEN} karakter.'
    return pw, None


def _delete_tenant_logo_file(relative_path):
    if not relative_path:
        return
    abs_path = os.path.join(current_app.static_folder, relative_path)
    if os.path.isfile(abs_path):
        try:
            os.remove(abs_path)
        except OSError:
            pass


def _save_tenant_logo(file_storage, tenant_id):
    if not file_storage or not file_storage.filename:
        return None
    raw = file_storage.filename
    ext = raw.rsplit('.', 1)[-1].lower() if '.' in raw else ''
    if ext not in current_app.config['PRODUCT_IMAGE_ALLOWED']:
        raise ValueError('Format logo tidak didukung (png, jpg, jpeg, webp, gif).')
    sub = str(tenant_id)
    folder = os.path.join(current_app.static_folder, 'uploads', 'tenants', sub)
    os.makedirs(folder, exist_ok=True)
    fname = f'logo_{uuid.uuid4().hex}.{ext}'
    path_abs = os.path.join(folder, fname)
    file_storage.save(path_abs)
    return f'uploads/tenants/{sub}/{fname}'


@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def settings():
    """Profil toko, logo, dan zona waktu untuk seluruh pengguna tenant."""
    if current_user.is_superadmin:
        flash('Super Admin mengatur zona waktu lewat menu Pengaturan di grup Super Admin.', 'info')
        return redirect(url_for('superadmin.index'))
    tenant_id = current_user.tenant_id
    if not tenant_id:
        flash('Tidak ada tenant terkait akun ini.', 'danger')
        return redirect(url_for('dashboard.index'))
    tenant = Tenant.query.get_or_404(tenant_id)
    if request.method == 'POST':
        nama = (request.form.get('nama') or '').strip()
        if not nama:
            flash('Nama toko wajib diisi.', 'danger')
            return redirect(url_for('admin.settings'))
        tenant.nama = nama[:100]
        tenant.alamat = (request.form.get('alamat') or '').strip()
        tenant.provinsi = (request.form.get('provinsi') or '').strip() or None
        tenant.kab_kota = (request.form.get('kab_kota') or '').strip() or None
        tenant.kecamatan = (request.form.get('kecamatan') or '').strip() or None
        tenant.desa = (request.form.get('desa') or '').strip() or None
        tenant.telepon = (request.form.get('telepon') or '').strip()[:20]
        tenant.email = (request.form.get('email') or '').strip()[:100]
        tenant.timezone = normalize_timezone_id(request.form.get('timezone'))

        f = request.files.get('logo')
        if f and f.filename:
            try:
                new_path = _save_tenant_logo(f, tenant_id)
                _delete_tenant_logo_file(tenant.logo)
                tenant.logo = new_path
            except ValueError as e:
                flash(str(e), 'danger')
                return redirect(url_for('admin.settings'))
        elif request.form.get('remove_logo'):
            old = tenant.logo
            tenant.logo = None
            _delete_tenant_logo_file(old)

        log_user_audit(
            tenant_id,
            current_user.id,
            'tenant_settings_update',
            detail='profil toko / logo / zona waktu',
        )
        db.session.commit()
        flash('Pengaturan toko disimpan.', 'success')
        return redirect(url_for('admin.settings'))
    return render_template(
        'admin/settings.html',
        tenant=tenant,
        timezone_choices=INDONESIA_TIMEZONE_CHOICES,
        current_tz=normalize_timezone_id(getattr(tenant, 'timezone', None)),
    )


@admin_bp.route('/users')
@login_required
@admin_required
def users():
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        flash('Kelola pengguna per tenant lewat menu Super Admin.', 'info')
        return redirect(url_for('superadmin.index'))

    q_text = (request.args.get('q') or '').strip()
    role_f = (request.args.get('role') or '').strip()
    branch_f = (request.args.get('branch_id') or '').strip()
    status_f = (request.args.get('status') or '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    q = User.query.filter_by(tenant_id=tenant_id)
    if q_text:
        like = f'%{q_text}%'
        q = q.filter(or_(User.nama.ilike(like), User.username.ilike(like)))
    if role_f in ('admin', 'kasir'):
        q = q.filter(User.role == role_f)
    if branch_f == 'none':
        q = q.filter(User.branch_id.is_(None))
    elif branch_f.isdigit():
        q = q.filter(User.branch_id == int(branch_f))
    if status_f == 'active':
        q = q.filter(User.aktif.is_(True))
    elif status_f == 'inactive':
        q = q.filter(User.aktif.is_(False))

    total = q.count()
    total_pages = max(1, (total + USER_PAGE_SIZE - 1) // USER_PAGE_SIZE)
    page = min(page, total_pages)

    users_page = (
        q.order_by(User.nama)
        .offset((page - 1) * USER_PAGE_SIZE)
        .limit(USER_PAGE_SIZE)
        .all()
    )

    branches = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Branch.nama).all()
    audit_logs = (
        UserAuditLog.query.filter_by(tenant_id=tenant_id)
        .order_by(UserAuditLog.created_at.desc())
        .limit(25)
        .all()
    )

    return render_template(
        'admin/users.html',
        users=users_page,
        branches=branches,
        total_users=total,
        page=page,
        total_pages=total_pages,
        per_page=USER_PAGE_SIZE,
        q=q_text,
        filter_role=role_f,
        filter_branch=branch_f,
        filter_status=status_f,
        audit_logs=audit_logs,
    )


@admin_bp.route('/users/add', methods=['POST'])
@login_required
@admin_required
def add_user():
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        return redirect(url_for('superadmin.index'))

    tenant = Tenant.query.get(tenant_id)
    cur_count = User.query.filter_by(tenant_id=tenant_id).count()
    if tenant and tenant.max_user and cur_count >= tenant.max_user:
        flash(f'Kuota pengguna ({tenant.max_user}) untuk tenant ini sudah penuh.', 'danger')
        return redirect(url_for('admin.users'))

    username, err = validate_username(request.form.get('username', ''))
    if err:
        flash(err, 'danger')
        return redirect(url_for('admin.users'))

    if User.query.filter_by(username=username).first():
        flash('Username sudah digunakan!', 'danger')
        return redirect(url_for('admin.users'))

    password = request.form.get('password') or ''
    password_confirm = request.form.get('password_confirm') or ''
    if not password:
        flash('Password wajib diisi.', 'danger')
        return redirect(url_for('admin.users'))
    _, pw_err = validate_password_pair(password, password_confirm)
    if pw_err:
        flash(pw_err, 'danger')
        return redirect(url_for('admin.users'))

    role = request.form.get('role', 'kasir')
    if role not in ('admin', 'kasir'):
        role = 'kasir'

    branch_raw = request.form.get('branch_id') or ''
    branch_id = int(branch_raw) if branch_raw.isdigit() else None
    if branch_id:
        br = Branch.query.filter_by(id=branch_id, tenant_id=tenant_id).first()
        if not br:
            flash('Cabang tidak valid.', 'danger')
            return redirect(url_for('admin.users'))

    nama = (request.form.get('nama') or '').strip()
    if len(nama) < 2:
        flash('Nama minimal 2 karakter.', 'danger')
        return redirect(url_for('admin.users'))

    perm_err, perm_json = _compute_permissions_from_form(role)
    if perm_err:
        flash(perm_err, 'danger')
        return redirect(url_for('admin.users'))

    user = User(
        tenant_id=tenant_id,
        branch_id=branch_id,
        nama=nama,
        username=username,
        role=role,
        session_version=0,
        permissions_json=perm_json,
    )
    user.set_password(password)
    db.session.add(user)
    log_user_audit(
        tenant_id,
        current_user.id,
        'user_created',
        user.id,
        f'username={username}, role={role}, custom_perm={bool(perm_json)}',
    )
    db.session.commit()
    flash(f'User "{user.nama}" berhasil ditambahkan!', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/toggle/<int:id>', methods=['POST'])
@login_required
@admin_required
def toggle_user(id):
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        return redirect(url_for('superadmin.index'))

    user = User.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    if user.id == current_user.id:
        flash('Tidak bisa menonaktifkan akun sendiri!', 'warning')
        return redirect(url_for('admin.users'))

    next_aktif = not user.aktif
    if not next_aktif and user.role == 'admin':
        if other_active_admins_count(tenant_id, user.id) < 1:
            flash('Minimal harus ada satu Admin aktif.', 'danger')
            return redirect(url_for('admin.users'))

    user.aktif = next_aktif
    log_user_audit(
        tenant_id,
        current_user.id,
        'user_toggled',
        user.id,
        'aktif=' + ('true' if user.aktif else 'false'),
    )
    db.session.commit()
    flash(f'User "{user.nama}" {"diaktifkan" if user.aktif else "dinonaktifkan"}.', 'info')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user(id):
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        return redirect(url_for('superadmin.index'))

    user = User.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    branches = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Branch.nama).all()

    def _edit_ctx(**overrides):
        kw = {
            'user': user,
            'branches': branches,
            'perm_codes_checked': set(effective_permission_codes(user)),
            'use_custom_perms': bool((user.permissions_json or '').strip()),
        }
        kw.update(overrides)
        return kw

    if request.method == 'GET':
        return render_template('admin/user_edit.html', **_edit_ctx())

    nama = (request.form.get('nama') or '').strip()
    if len(nama) < 2:
        flash('Nama minimal 2 karakter.', 'danger')
        return render_template('admin/user_edit.html', **_edit_ctx())

    username, uerr = validate_username(request.form.get('username', ''))
    if uerr:
        flash(uerr, 'danger')
        return render_template('admin/user_edit.html', **_edit_ctx())

    taken = User.query.filter(User.username == username, User.id != user.id).first()
    if taken:
        flash('Username sudah dipakai pengguna lain.', 'danger')
        return render_template('admin/user_edit.html', **_edit_ctx())

    role = request.form.get('role', user.role)
    if role not in ('admin', 'kasir'):
        role = user.role

    if user.id == current_user.id and user.role == 'admin' and role == 'kasir':
        flash('Anda tidak dapat menurunkan role sendiri dari Admin ke Kasir.', 'danger')
        return render_template('admin/user_edit.html', **_edit_ctx())

    if user.role == 'admin' and role == 'kasir':
        if other_active_admins_count(tenant_id, user.id) < 1:
            flash('Tetapkan Admin aktif lain sebelum menurunkan pengguna ini.', 'danger')
            return render_template('admin/user_edit.html', **_edit_ctx())

    branch_raw = request.form.get('branch_id') or ''
    branch_id = int(branch_raw) if branch_raw.isdigit() else None
    if branch_id:
        br = Branch.query.filter_by(id=branch_id, tenant_id=tenant_id).first()
        if not br:
            flash('Cabang tidak valid.', 'danger')
            return render_template('admin/user_edit.html', **_edit_ctx())

    want_aktif = 'aktif' in request.form
    if user.id == current_user.id and not want_aktif:
        flash('Anda tidak dapat menonaktifkan akun sendiri dari halaman ini.', 'danger')
        return render_template('admin/user_edit.html', **_edit_ctx())

    if user.role == 'admin' and user.aktif and not want_aktif:
        if other_active_admins_count(tenant_id, user.id) < 1:
            flash('Minimal harus ada satu Admin aktif.', 'danger')
            return render_template('admin/user_edit.html', **_edit_ctx())

    pw = request.form.get('password_new') or ''
    pw2 = request.form.get('password_confirm') or ''
    new_pw, pw_err = validate_password_pair(pw, pw2)
    if pw_err:
        flash(pw_err, 'danger')
        return render_template('admin/user_edit.html', **_edit_ctx())

    perm_err, perm_json = _compute_permissions_from_form(role)
    if perm_err:
        flash(perm_err, 'danger')
        repop = set(parse_perm_form(request.form))
        if request.form.get('use_custom_perms') and not repop:
            repop = {'dashboard'}
        return render_template(
            'admin/user_edit.html',
            **_edit_ctx(
                perm_codes_checked=repop if request.form.get('use_custom_perms') else set(effective_permission_codes(user)),
                use_custom_perms=bool(request.form.get('use_custom_perms')),
            ),
        )

    user.nama = nama
    user.username = username
    user.role = role
    user.branch_id = branch_id
    user.aktif = want_aktif
    user.permissions_json = perm_json

    detail_parts = [
        f'username={username}',
        f'role={role}',
        f'aktif={user.aktif}',
        f'perm_custom={bool(perm_json)}',
    ]
    if new_pw:
        user.set_password(new_pw)
        user.session_version = int(getattr(user, 'session_version', 0) or 0) + 1
        log_user_audit(
            tenant_id,
            current_user.id,
            'password_reset',
            user.id,
            'password diubah; ' + ', '.join(detail_parts),
        )
    else:
        log_user_audit(tenant_id, current_user.id, 'user_updated', user.id, ', '.join(detail_parts))

    db.session.commit()
    flash(f'Data "{user.nama}" berhasil disimpan.', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/<int:id>/force-logout', methods=['POST'])
@login_required
@admin_required
def force_logout_user(id):
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        return redirect(url_for('superadmin.index'))

    user = User.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    if user.id == current_user.id:
        flash('Untuk akun sendiri gunakan menu Keluar.', 'info')
        return redirect(url_for('admin.users'))

    user.session_version = int(getattr(user, 'session_version', 0) or 0) + 1
    log_user_audit(tenant_id, current_user.id, 'force_logout', user.id, user.username)
    db.session.commit()
    flash(f'Sesi pengguna "{user.nama}" telah dihentikan (harus login ulang).', 'success')
    return redirect(url_for('admin.users'))


def _branch_stats_map(tenant_id, branch_ids):
    stats = {bid: {'users': 0, 'trx_today': 0} for bid in branch_ids}
    if not branch_ids:
        return stats
    for bid, c in (
        db.session.query(User.branch_id, func.count(User.id))
        .filter(User.tenant_id == tenant_id, User.branch_id.in_(branch_ids))
        .group_by(User.branch_id)
        .all()
    ):
        if bid in stats:
            stats[bid]['users'] = c
    today = datetime.utcnow().date()
    t0 = datetime.combine(today, datetime.min.time())
    t1 = datetime.combine(today, datetime.max.time())
    for bid, c in (
        db.session.query(Transaction.branch_id, func.count(Transaction.id))
        .filter(
            Transaction.tenant_id == tenant_id,
            Transaction.branch_id.in_(branch_ids),
            Transaction.status == 'selesai',
            Transaction.created_at.between(t0, t1),
        )
        .group_by(Transaction.branch_id)
        .all()
    ):
        if bid in stats:
            stats[bid]['trx_today'] = c
    return stats


@admin_bp.route('/branches')
@login_required
@admin_required
def branches():
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        return redirect(url_for('superadmin.index'))

    q_text = (request.args.get('q') or '').strip()
    status_f = (request.args.get('status') or '').strip()

    q = Branch.query.filter_by(tenant_id=tenant_id)
    if q_text:
        like = f'%{q_text}%'
        q = q.filter(
            or_(
                Branch.nama.ilike(like),
                Branch.kode.ilike(like),
                Branch.alamat.ilike(like),
                Branch.telepon.ilike(like),
            )
        )
    if status_f == 'active':
        q = q.filter(Branch.aktif.is_(True))
    elif status_f == 'inactive':
        q = q.filter(Branch.aktif.is_(False))

    branches_list = q.order_by(Branch.nama).all()
    ids = [b.id for b in branches_list]
    branch_stats = _branch_stats_map(tenant_id, ids)

    audit_logs = (
        UserAuditLog.query.filter(
            UserAuditLog.tenant_id == tenant_id,
            UserAuditLog.action.like('branch_%'),
        )
        .order_by(UserAuditLog.created_at.desc())
        .limit(20)
        .all()
    )

    tenant = Tenant.query.get(tenant_id)
    branch_count_total = Branch.query.filter_by(tenant_id=tenant_id).count()

    return render_template(
        'admin/branches.html',
        branches=branches_list,
        branch_stats=branch_stats,
        audit_logs=audit_logs,
        tenant=tenant,
        branch_count_total=branch_count_total,
        q=q_text,
        filter_status=status_f,
    )


@admin_bp.route('/branches/add', methods=['POST'])
@login_required
@admin_required
def add_branch():
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        return redirect(url_for('superadmin.index'))

    tenant = Tenant.query.get(tenant_id)
    cur = Branch.query.filter_by(tenant_id=tenant_id).count()
    if tenant and tenant.max_cabang and cur >= tenant.max_cabang:
        flash(
            f'Kuota cabang ({tenant.max_cabang}) untuk tenant ini sudah penuh. Hubungi super admin untuk menaikkan kuota.',
            'danger',
        )
        return redirect(url_for('admin.branches'))

    nama = (request.form.get('nama') or '').strip()
    kode = (request.form.get('kode') or '').strip().upper()
    alamat = (request.form.get('alamat') or '').strip()
    telepon = (request.form.get('telepon') or '').strip()

    if len(nama) < 2:
        flash('Nama cabang minimal 2 karakter.', 'danger')
        return redirect(url_for('admin.branches'))
    if len(kode) < 2:
        flash('Kode cabang minimal 2 karakter.', 'danger')
        return redirect(url_for('admin.branches'))

    if Branch.query.filter_by(tenant_id=tenant_id, kode=kode).first():
        flash(f'Kode cabang "{kode}" sudah dipakai di tenant ini.', 'danger')
        return redirect(url_for('admin.branches'))

    branch = Branch(
        tenant_id=tenant_id,
        nama=nama,
        kode=kode,
        alamat=alamat,
        telepon=telepon,
    )
    db.session.add(branch)
    try:
        db.session.flush()
        log_user_audit(
            tenant_id,
            current_user.id,
            'branch_created',
            detail=f'branch_id={branch.id},kode={kode},nama={nama}',
        )
        db.session.commit()
        flash(f'Cabang "{branch.nama}" berhasil ditambahkan!', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Kode cabang bentrok (unik per tenant). Pilih kode lain.', 'danger')
    return redirect(url_for('admin.branches'))


@admin_bp.route('/branches/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_branch(id):
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        return redirect(url_for('superadmin.index'))

    branch = Branch.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()

    if request.method == 'GET':
        return render_template('admin/branch_edit.html', branch=branch)

    nama = (request.form.get('nama') or '').strip()
    kode = (request.form.get('kode') or '').strip().upper()
    alamat = (request.form.get('alamat') or '').strip()
    telepon = (request.form.get('telepon') or '').strip()

    if len(nama) < 2:
        flash('Nama cabang minimal 2 karakter.', 'danger')
        return render_template('admin/branch_edit.html', branch=branch)
    if len(kode) < 2:
        flash('Kode cabang minimal 2 karakter.', 'danger')
        return render_template('admin/branch_edit.html', branch=branch)

    taken = Branch.query.filter(
        Branch.tenant_id == tenant_id,
        Branch.kode == kode,
        Branch.id != branch.id,
    ).first()
    if taken:
        flash(f'Kode "{kode}" sudah dipakai cabang lain.', 'danger')
        return render_template('admin/branch_edit.html', branch=branch)

    branch.nama = nama
    branch.kode = kode
    branch.alamat = alamat
    branch.telepon = telepon
    try:
        log_user_audit(
            tenant_id,
            current_user.id,
            'branch_updated',
            detail=f'branch_id={branch.id},kode={kode},nama={nama}',
        )
        db.session.commit()
        flash(f'Cabang "{branch.nama}" berhasil diperbarui.', 'success')
        return redirect(url_for('admin.branches'))
    except IntegrityError:
        db.session.rollback()
        flash('Kode cabang bentrok. Pilih kode lain.', 'danger')
        return render_template('admin/branch_edit.html', branch=branch)


@admin_bp.route('/branches/toggle/<int:id>', methods=['POST'])
@login_required
@admin_required
def toggle_branch(id):
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        return redirect(url_for('superadmin.index'))
    branch = Branch.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()

    next_aktif = not branch.aktif
    if not next_aktif:
        active_on = User.query.filter_by(branch_id=branch.id, aktif=True).count()
        if active_on > 0:
            flash(
                f'Tidak bisa menonaktifkan: {active_on} pengguna aktif masih terikat cabang ini. '
                'Pindahkan mereka ke cabang lain di Manajemen Pengguna.',
                'danger',
            )
            return redirect(url_for('admin.branches'))
        inactive_on = User.query.filter_by(branch_id=branch.id, aktif=False).count()
        if inactive_on > 0:
            flash(
                f'Perhatian: {inactive_on} pengguna nonaktif masih mencatat cabang ini di profil.',
                'warning',
            )

    branch.aktif = next_aktif
    log_user_audit(
        tenant_id,
        current_user.id,
        'branch_toggled',
        detail=f'branch_id={branch.id},kode={branch.kode},aktif={branch.aktif}',
    )
    db.session.commit()
    flash(f'Cabang "{branch.nama}" {"diaktifkan" if branch.aktif else "dinonaktifkan"}.', 'info')
    return redirect(url_for('admin.branches'))
