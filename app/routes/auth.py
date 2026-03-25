from flask import Blueprint, render_template, redirect, url_for, flash, request, session, current_app
from flask_login import login_user, logout_user, login_required, current_user
from datetime import datetime
from .. import db
from ..models import User, Tenant
from ..subscription import tenant_login_allowed

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember', False)

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password) and user.aktif:
            if user.tenant_id:
                tenant = Tenant.query.get(user.tenant_id)
                if tenant and not tenant.aktif:
                    flash('Tenant Anda dinonaktifkan. Hubungi penyedia layanan.', 'danger')
                    return render_template('login.html')
                if tenant:
                    ok, sub_msg = tenant_login_allowed(tenant, current_app.config)
                    if not ok:
                        flash(sub_msg or 'Akses tenant ditolak.', 'danger')
                        return render_template('login.html')
            login_user(user, remember=bool(remember))
            user.last_login = datetime.utcnow()
            db.session.commit()
            db.session.refresh(user)

            session['user_session_version'] = int(getattr(user, 'session_version', 0) or 0)
            # Store tenant & branch in session
            session['tenant_id'] = user.tenant_id
            session['branch_id'] = user.branch_id

            next_page = request.args.get('next')
            flash(f'Selamat datang, {user.nama}!', 'success')
            return redirect(next_page or url_for('dashboard.index'))
        else:
            flash('Username atau password salah, atau akun tidak aktif.', 'danger')

    return render_template('login.html')


@auth_bp.route('/logout', methods=['GET', 'POST'])
@login_required
def logout():
    """Jangan panggil session.clear() setelah logout_user(): Flask-Login menyimpan
    _remember='clear' di session agar cookie Remember Me terhapus di response;
    session.clear() menghapus flag itu sehingga pengguna tetap masuk lewat cookie."""
    logout_user()
    for key in (
        'tenant_id',
        'branch_id',
        'user_session_version',
        'impersonator_id',
        'tenant_bootstrap',
        'password_reset_result',
    ):
        session.pop(key, None)
    flash('Anda telah keluar dari sistem.', 'info')
    return redirect(url_for('auth.login'))
