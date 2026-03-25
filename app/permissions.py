"""
Hak akses modul per pengguna (tenant). JSON di User.permissions_json;
jika NULL = pakai default sesuai role (admin / kasir = akses penuh modul operasional).
"""
import json
from functools import wraps

# Kode modul (selain dashboard — selalu boleh untuk user tenant)
PERMISSION_MODULES = (
    ('dashboard', 'Dashboard'),
    ('pos', 'Kasir / POS'),
    ('cash_shifts', 'Shift kasir'),
    ('transactions', 'Riwayat transaksi'),
    ('returns', 'Retur penjualan'),
    ('products', 'Produk & kategori'),
    ('suppliers', 'Supplier'),
    ('purchases', 'Pembelian (PO)'),
    ('members', 'Member'),
    ('debts', 'Hutang piutang'),
    ('reports', 'Laporan penjualan'),
    ('operating_expenses', 'Biaya operasional'),
)

MODULE_CODES = frozenset(k for k, _ in PERMISSION_MODULES)
DEFAULT_ADMIN_CODES = MODULE_CODES
DEFAULT_KASIR_CODES = MODULE_CODES  # sama seperti perilaku lama: kasir akses semua modul operasional


def _parse_json(raw):
    if not raw or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            return None
        out = [str(x).strip() for x in data if str(x).strip() in MODULE_CODES]
        return frozenset(out) if out else frozenset()
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_tenant_package_modules(raw):
    """None = tidak membatasi modul (semua sesuai hak user)."""
    if not raw or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            return None
        out = frozenset(str(x).strip() for x in data if str(x).strip() in MODULE_CODES)
        if not out:
            return None
        if 'dashboard' not in out:
            out = out | {'dashboard'}
        return out
    except (json.JSONDecodeError, TypeError):
        return None


def effective_codes_for_package_role(modules_json, role):
    """Himpunan modul efektif jika user role punya default penuh dan hanya dibatasi paket."""
    cap = _parse_tenant_package_modules(modules_json)
    if role == 'admin':
        base = DEFAULT_ADMIN_CODES
    elif role == 'kasir':
        base = DEFAULT_KASIR_CODES
    else:
        base = frozenset()
    if cap is None:
        return base
    return base & cap


def blocked_module_labels_for_package_role(modules_json, role):
    """Label modul yang tidak akan pernah bisa diakses (default penuh ∩ batas paket)."""
    eff = effective_codes_for_package_role(modules_json, role)
    if role == 'admin':
        base = DEFAULT_ADMIN_CODES
    elif role == 'kasir':
        base = DEFAULT_KASIR_CODES
    else:
        base = frozenset()
    blocked = base - eff
    return [label for code, label in PERMISSION_MODULES if code in blocked]


def tenant_package_module_cap(tenant_id):
    """Batas modul dari paket tenant; None jika tidak ada batas."""
    if not tenant_id:
        return None
    from .models import Tenant, TenantPackage
    t = Tenant.query.get(tenant_id)
    if not t or not t.paket_id:
        return None
    pkg = TenantPackage.query.get(t.paket_id)
    if not pkg or not pkg.aktif:
        return None
    return _parse_tenant_package_modules(pkg.modules_json)


def effective_permission_codes(user):
    """Himpunan kode modul yang boleh diakses user."""
    if user is None:
        return frozenset()
    if getattr(user, 'is_superadmin', False):
        return MODULE_CODES
    if getattr(user, 'tenant_id', None) is None:
        return frozenset()

    parsed = _parse_json(getattr(user, 'permissions_json', None))
    if parsed is None:
        if user.role == 'admin':
            user_codes = DEFAULT_ADMIN_CODES
        elif user.role == 'kasir':
            user_codes = DEFAULT_KASIR_CODES
        else:
            user_codes = frozenset()
    else:
        # Kustom: minimal dashboard jika daftar tidak kosong tapi lupa dashboard
        if parsed and 'dashboard' not in parsed:
            user_codes = frozenset(parsed) | {'dashboard'}
        else:
            user_codes = parsed or frozenset({'dashboard'})

    cap = tenant_package_module_cap(getattr(user, 'tenant_id', None))
    if cap is not None:
        user_codes = user_codes & cap
    return user_codes


def user_can(user, code):
    if code == 'dashboard':
        if user is None:
            return False
        if getattr(user, 'is_superadmin', False):
            return True
        return getattr(user, 'tenant_id', None) is not None
    if user is None:
        return False
    if getattr(user, 'is_superadmin', False):
        return True
    if getattr(user, 'tenant_id', None) is None:
        return False
    return code in effective_permission_codes(user)


def normalize_permissions_json(role, selected_codes):
    """selected_codes: iterable of str. Return None jika sama dengan default penuh."""
    valid = frozenset(x for x in selected_codes if x in MODULE_CODES)
    if not valid:
        return None
    if 'dashboard' not in valid:
        valid = valid | {'dashboard'}
    default = DEFAULT_ADMIN_CODES if role == 'admin' else DEFAULT_KASIR_CODES
    if valid == default:
        return None
    return json.dumps(sorted(valid))


def parse_perm_form(form):
    """Ambil daftar dari checkbox name=perm."""
    return [x for x in form.getlist('perm') if x in MODULE_CODES]


def register_module_guards():
    from flask import request, redirect, url_for, flash
    from flask_login import current_user

    from .routes.pos import pos_bp
    from .routes.transactions import transactions_bp
    from .routes.products import products_bp
    from .routes.suppliers import suppliers_bp
    from .routes.purchases import purchases_bp
    from .routes.members import members_bp
    from .routes.debts import debts_bp
    from .routes.reports import reports_bp
    from .routes.operating_expenses import operating_expenses_bp
    from .routes.shifts import shifts_bp
    from .routes.returns import returns_bp
    from .routes.receipt import receipt_bp

    def make_guard(code):
        def guard():
            ep = str(request.endpoint or '')
            if ep.startswith('static') or not ep:
                return None
            if not current_user.is_authenticated:
                return None
            if getattr(current_user, 'is_superadmin', False):
                return None
            if getattr(current_user, 'tenant_id', None) is None:
                return None
            if user_can(current_user, code):
                return None
            flash('Anda tidak memiliki akses ke modul ini. Hubungi admin.', 'warning')
            return redirect(url_for('dashboard.index'))
        return guard

    for bp, code in (
        (pos_bp, 'pos'),
        (shifts_bp, 'cash_shifts'),
        (transactions_bp, 'transactions'),
        (returns_bp, 'returns'),
        (products_bp, 'products'),
        (suppliers_bp, 'suppliers'),
        (purchases_bp, 'purchases'),
        (members_bp, 'members'),
        (debts_bp, 'debts'),
        (reports_bp, 'reports'),
        (operating_expenses_bp, 'operating_expenses'),
    ):
        bp.before_request(make_guard(code))

    def receipt_guard():
        ep = str(request.endpoint or '')
        if ep.startswith('static') or not ep:
            return None
        if not current_user.is_authenticated:
            return None
        if getattr(current_user, 'is_superadmin', False):
            return None
        if getattr(current_user, 'tenant_id', None) is None:
            return None
        if (
            user_can(current_user, 'transactions')
            or user_can(current_user, 'pos')
            or user_can(current_user, 'returns')
        ):
            return None
        flash('Anda tidak memiliki akses ke struk transaksi.', 'warning')
        return redirect(url_for('dashboard.index'))

    receipt_bp.before_request(receipt_guard)


def permission_required(code):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            from flask_login import current_user
            from flask import redirect, url_for, flash
            if not user_can(current_user, code):
                flash('Akses ditolak untuk modul ini.', 'warning')
                return redirect(url_for('dashboard.index'))
            return f(*args, **kwargs)
        return wrapped
    return decorator
