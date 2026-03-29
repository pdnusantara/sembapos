import os
from flask import Flask, request, session, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_bcrypt import Bcrypt
from flask_wtf.csrf import CSRFProtect
from .config import Config

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
bcrypt = Bcrypt()
csrf = CSRFProtect()

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)
    bcrypt.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Silakan login terlebih dahulu.'
    login_manager.login_message_category = 'warning'

    from .routes.auth import auth_bp
    from .routes.landing import landing_bp
    from .routes.dashboard import dashboard_bp
    from .routes.pos import pos_bp
    from .routes.products import products_bp
    from .routes.reports import reports_bp
    from .routes.admin import admin_bp
    from .routes.superadmin import superadmin_bp
    from .routes.receipt import receipt_bp
    from .routes.suppliers import suppliers_bp
    from .routes.purchases import purchases_bp
    from .routes.members import members_bp
    from .routes.debts import debts_bp
    from .routes.transactions import transactions_bp
    from .routes.operating_expenses import operating_expenses_bp
    from .routes.shifts import shifts_bp
    from .routes.returns import returns_bp
    from .routes.marketplace import marketplace_bp
    from .permissions import register_module_guards

    register_module_guards()

    app.register_blueprint(auth_bp)
    app.register_blueprint(landing_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(pos_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(superadmin_bp)
    app.register_blueprint(receipt_bp)
    app.register_blueprint(suppliers_bp)
    app.register_blueprint(purchases_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(debts_bp)
    app.register_blueprint(transactions_bp)
    app.register_blueprint(operating_expenses_bp)
    app.register_blueprint(shifts_bp)
    app.register_blueprint(returns_bp)
    app.register_blueprint(marketplace_bp)

    @app.context_processor
    def _inject_static_css_version():
        """Nginx mem-cache /static lama (immutable); ?v= memaksa browser ambil CSS baru."""
        from flask import current_app

        override = current_app.config.get('STATIC_ASSET_VERSION')
        if override:
            v = str(override)
        else:
            path = os.path.join(current_app.static_folder, 'css', 'style.css')
            try:
                v = str(int(os.path.getmtime(path)))
            except OSError:
                v = '0'
        path_landing = os.path.join(current_app.static_folder, 'css', 'landing.css')
        try:
            landing_css_v = str(int(os.path.getmtime(path_landing)))
        except OSError:
            landing_css_v = v
        path_mkt = os.path.join(current_app.static_folder, 'css', 'marketplace.css')
        try:
            marketplace_css_v = str(int(os.path.getmtime(path_mkt)))
        except OSError:
            marketplace_css_v = v
        return dict(static_css_v=v, landing_css_v=landing_css_v, marketplace_css_v=marketplace_css_v)

    upload_root = os.path.join(app.static_folder, 'uploads', 'products')
    os.makedirs(upload_root, exist_ok=True)

    with app.app_context():
        # create_all aman untuk dev SQLite, tapi pada PostgreSQL + multi-worker
        # bisa race condition saat startup jika diaktifkan bersamaan.
        if app.config.get('AUTO_DB_CREATE_ALL', True):
            db.create_all()
        _ensure_branch_tenant_kode_unique_index()
        _ensure_product_price_tiers_columns()
        _ensure_tenant_location_columns()
        _ensure_timezone_columns()
        _seed_tenant_packages()

    @app.before_request
    def _check_user_session_version():
        from flask_login import current_user, logout_user
        if not request.endpoint or str(request.endpoint).startswith('static'):
            return
        if not current_user.is_authenticated:
            return
        ver_db = getattr(current_user, 'session_version', None)
        if ver_db is None:
            return
        sv = session.get('user_session_version')
        if sv is None:
            session['user_session_version'] = int(ver_db)
            return
        try:
            if int(sv) != int(ver_db):
                logout_user()
                session.clear()
                flash('Sesi tidak lagi valid (password diubah atau logout paksa). Silakan login lagi.', 'warning')
                return redirect(url_for('auth.login'))
        except (TypeError, ValueError):
            session['user_session_version'] = int(ver_db)

    @app.before_request
    def _subscription_access_gate():
        from flask_login import current_user, logout_user
        from .models import Tenant
        from .subscription import tenant_session_policy

        if not request.endpoint or str(request.endpoint).startswith('static'):
            return
        if not current_user.is_authenticated:
            return
        if getattr(current_user, 'is_superadmin', False):
            return
        tid = getattr(current_user, 'tenant_id', None)
        if not tid:
            return
        tenant = Tenant.query.get(tid)
        if not tenant:
            return
        policy, meta = tenant_session_policy(tenant, app.config)
        if policy == 'block_login':
            logout_user()
            session.clear()
            flash(
                'Akses tenant dihentikan karena masa langganan berakhir (kebijakan: blokir login). '
                'Hubungi penyedia layanan.',
                'danger',
            )
            return redirect(url_for('auth.login'))
        if policy == 'read_only':
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                if (
                    request.endpoint == 'superadmin.stop_impersonate'
                    and session.get('impersonator_id')
                ):
                    return None
                flash(
                    'Tenant dalam mode baca saja (langganan berakhir). '
                    'Hanya penjelajahan halaman yang diizinkan.',
                    'warning',
                )
                return redirect(url_for('dashboard.index'))

    @app.context_processor
    def _subscription_banner():
        from flask_login import current_user
        from .models import Tenant
        from .subscription import subscription_state

        if not current_user.is_authenticated or getattr(current_user, 'is_superadmin', False):
            return {'subscription_banner': None}
        tid = getattr(current_user, 'tenant_id', None)
        if not tid:
            return {'subscription_banner': None}
        tenant = Tenant.query.get(tid)
        if not tenant:
            return {'subscription_banner': None}
        phase, meta = subscription_state(tenant, app.config)
        if phase == 'grace':
            return {
                'subscription_banner': {
                    'kind': 'grace',
                    'grace_ends_at': meta.get('grace_ends_at'),
                },
            }
        if phase == 'enforced' and meta.get('policy') == 'read_only':
            return {'subscription_banner': {'kind': 'read_only'}}
        if phase == 'active' and tenant.tanggal_expired:
            from datetime import datetime, timedelta
            if tenant.tanggal_expired - datetime.utcnow() <= timedelta(days=14):
                return {
                    'subscription_banner': {
                        'kind': 'expiring_soon',
                        'until': tenant.tanggal_expired,
                    },
                }
        return {'subscription_banner': None}

    @app.context_processor
    def _template_time():
        from datetime import datetime
        return {'utc_now': datetime.utcnow}

    @app.context_processor
    def _marketplace_pending_count():
        from flask_login import current_user
        from .models import MarketplaceOrder
        if not current_user.is_authenticated:
            return {'marketplace_pending_count': 0}
        if not getattr(current_user, 'is_superadmin', False):
            return {'marketplace_pending_count': 0}
        try:
            count = MarketplaceOrder.query.filter_by(status='pending').count()
        except Exception:
            count = 0
        return {'marketplace_pending_count': count}

    @app.context_processor
    def _inject_permissions():
        from flask_login import current_user
        from .permissions import user_can, effective_permission_codes, PERMISSION_MODULES

        def perm(code):
            if not current_user.is_authenticated:
                return False
            return user_can(current_user, code)

        def perm_summary_for(u):
            if getattr(u, 'is_superadmin', False):
                return '—'
            if not getattr(u, 'tenant_id', None):
                return '—'
            if not (u.permissions_json or '').strip():
                return 'Default penuh' if u.role == 'admin' else 'Default kasir'
            n = len(effective_permission_codes(u))
            return f'{n} modul'

        return {
            'perm': perm,
            'perm_summary_for': perm_summary_for,
            'PERMISSION_MODULES': PERMISSION_MODULES,
        }

    @app.context_processor
    def _inject_timezone_label():
        from flask_login import current_user
        from .models import Tenant
        from .timezones import DEFAULT_TIMEZONE, normalize_timezone_id, timezone_short_label

        if not current_user.is_authenticated:
            return {'app_timezone_label': None}
        if getattr(current_user, 'is_superadmin', False):
            tid = normalize_timezone_id(getattr(current_user, 'timezone', None))
        elif getattr(current_user, 'tenant_id', None):
            t = getattr(current_user, 'tenant', None)
            if t is None:
                t = Tenant.query.get(current_user.tenant_id)
            tid = normalize_timezone_id(getattr(t, 'timezone', None)) if t else DEFAULT_TIMEZONE
        else:
            tid = DEFAULT_TIMEZONE
        return {'app_timezone_label': timezone_short_label(tid)}

    @app.template_filter('local_dt')
    def _local_dt_filter(dt, fmt='%d/%m/%Y %H:%M'):
        from flask_login import current_user
        from .timezones import format_utc_naive_as_local, resolve_effective_timezone_id

        return format_utc_naive_as_local(dt, resolve_effective_timezone_id(current_user), fmt)

    @app.template_filter('local_dt_tz')
    def _local_dt_tz_filter(dt, tz_id, fmt='%d/%m/%Y %H:%M'):
        from .timezones import format_utc_naive_as_local, normalize_timezone_id

        return format_utc_naive_as_local(dt, normalize_timezone_id(tz_id), fmt)

    return app


def _ensure_branch_tenant_kode_unique_index():
    """SQLite: unik (tenant_id, kode) jika belum ada index (abaikan jika duplikat data)."""
    from sqlalchemy import inspect, text
    try:
        if db.engine.dialect.name != 'sqlite':
            return
        insp = inspect(db.engine)
        if 'branches' not in insp.get_table_names():
            return
        with db.engine.begin() as conn:
            conn.execute(text(
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_branch_tenant_kode '
                'ON branches (tenant_id, kode)'
            ))
    except Exception:
        pass


def _seed_tenant_packages():
    from .models import Tenant, TenantPackage
    try:
        if TenantPackage.query.count() == 0:
            db.session.add_all([
                TenantPackage(
                    kode='basic',
                    nama='Basic',
                    deskripsi='Paket pemula — kuota cabang & user terbatas.',
                    max_cabang=3,
                    max_user=5,
                    modules_json=None,
                    harga_bulanan=99000,
                    harga_tahunan=990000,
                    aktif=True,
                    sort_order=10,
                ),
                TenantPackage(
                    kode='pro',
                    nama='Pro',
                    deskripsi='Bisnis berkembang — lebih banyak cabang & pengguna.',
                    max_cabang=10,
                    max_user=20,
                    modules_json=None,
                    harga_bulanan=299000,
                    harga_tahunan=2990000,
                    aktif=True,
                    sort_order=20,
                ),
                TenantPackage(
                    kode='enterprise',
                    nama='Enterprise',
                    deskripsi='Skala besar — kuota praktis tak terbatas.',
                    max_cabang=9999,
                    max_user=9999,
                    modules_json=None,
                    harga_bulanan=0,
                    harga_tahunan=0,
                    aktif=True,
                    sort_order=30,
                ),
            ])
            db.session.commit()
        for t in Tenant.query.filter(Tenant.paket_id.is_(None)).all():
            pkg = TenantPackage.query.filter_by(kode=(t.paket or 'basic').lower()).first()
            if pkg:
                t.paket_id = pkg.id
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_product_price_tiers_columns():
    """Compat schema lama: tambah kolom tier harga grosir jika belum ada."""
    from sqlalchemy import inspect, text
    try:
        insp = inspect(db.engine)
        if 'products' not in insp.get_table_names():
            return
        cols = {c['name'] for c in insp.get_columns('products')}
        statements = []
        if 'min_qty_grosir_1' not in cols:
            statements.append('ALTER TABLE products ADD COLUMN min_qty_grosir_1 FLOAT')
        if 'harga_jual_grosir_1' not in cols:
            statements.append('ALTER TABLE products ADD COLUMN harga_jual_grosir_1 FLOAT')
        if 'min_qty_grosir_2' not in cols:
            statements.append('ALTER TABLE products ADD COLUMN min_qty_grosir_2 FLOAT')
        if 'harga_jual_grosir_2' not in cols:
            statements.append('ALTER TABLE products ADD COLUMN harga_jual_grosir_2 FLOAT')
        if not statements:
            return
        with db.engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
    except Exception:
        pass


def _ensure_timezone_columns():
    """Compat schema: kolom timezone di tenants & users."""
    from sqlalchemy import inspect, text
    try:
        insp = inspect(db.engine)
        if 'tenants' in insp.get_table_names():
            tcols = {c['name'] for c in insp.get_columns('tenants')}
            if 'timezone' not in tcols:
                stmt = "ALTER TABLE tenants ADD COLUMN timezone VARCHAR(30) NOT NULL DEFAULT 'Asia/Jakarta'"
                with db.engine.begin() as conn:
                    conn.execute(text(stmt))
        if 'users' in insp.get_table_names():
            ucols = {c['name'] for c in insp.get_columns('users')}
            if 'timezone' not in ucols:
                stmt = 'ALTER TABLE users ADD COLUMN timezone VARCHAR(30)'
                with db.engine.begin() as conn:
                    conn.execute(text(stmt))
    except Exception:
        pass


def _ensure_tenant_location_columns():
    """Compat schema lama: tambah kolom lokasi tenant jika belum ada."""
    from sqlalchemy import inspect, text
    try:
        insp = inspect(db.engine)
        if 'tenants' not in insp.get_table_names():
            return
        cols = {c['name'] for c in insp.get_columns('tenants')}
        statements = []
        if 'provinsi' not in cols:
            statements.append('ALTER TABLE tenants ADD COLUMN provinsi VARCHAR(100)')
        if 'kab_kota' not in cols:
            statements.append('ALTER TABLE tenants ADD COLUMN kab_kota VARCHAR(100)')
        if 'kecamatan' not in cols:
            statements.append('ALTER TABLE tenants ADD COLUMN kecamatan VARCHAR(100)')
        if 'desa' not in cols:
            statements.append('ALTER TABLE tenants ADD COLUMN desa VARCHAR(100)')
        if not statements:
            return
        with db.engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
    except Exception:
        pass
