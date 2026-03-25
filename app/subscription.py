"""
Kebijakan langganan tenant setelah tanggal_expired.

SUBSCRIPTION_GRACE_DAYS: hari tambahan setelah tanggal_expired dengan akses penuh + peringatan.
Setelah lewat tanggal_expired + grace, terapkan SUBSCRIPTION_EXPIRED_MODE:
  - block_login: tidak bisa login; sesi aktif dilogout.
  - read_only: hanya GET/HEAD/OPTIONS (mutasi diblokir).
"""
from datetime import datetime, timedelta


def _grace_days(config):
    try:
        return max(0, int(getattr(config, 'SUBSCRIPTION_GRACE_DAYS', 0) or 0))
    except (TypeError, ValueError):
        return 0


def _expired_mode(config):
    m = (getattr(config, 'SUBSCRIPTION_EXPIRED_MODE', None) or 'block_login').strip().lower()
    return m if m in ('read_only', 'block_login') else 'block_login'


def subscription_state(tenant, config):
    """
    Return (phase, meta) where phase is:
      none — tanpa tanggal_expired
      active — belum lewat expired
      grace — lewat expired tapi masih dalam masa tenggang (akses penuh)
      enforced — kebijakan akhir diterapkan (read_only atau block_login)
    meta: dict dengan kunci grace_ends_at, policy (read_only|block_login) bila relevan
    """
    if tenant is None:
        return 'none', {}
    exp = tenant.tanggal_expired
    if exp is None:
        return 'none', {}
    now = datetime.utcnow()
    if now <= exp:
        return 'active', {}
    gd = _grace_days(config)
    if gd > 0:
        grace_end = exp + timedelta(days=gd)
        if now <= grace_end:
            return 'grace', {'grace_ends_at': grace_end}
    policy = _expired_mode(config)
    return 'enforced', {'policy': policy}


def tenant_login_allowed(tenant, config):
    phase, meta = subscription_state(tenant, config)
    if phase in ('none', 'active', 'grace'):
        return True, None
    if meta.get('policy') == 'block_login':
        return False, 'Masa langganan telah berakhir. Hubungi penyedia layanan untuk perpanjang.'
    return True, None


def tenant_session_policy(tenant, config):
    """Untuk user yang sudah login: 'ok' | 'read_only' | 'block_login'."""
    phase, meta = subscription_state(tenant, config)
    if phase in ('none', 'active', 'grace'):
        return 'ok', meta
    if meta.get('policy') == 'block_login':
        return 'block_login', meta
    if meta.get('policy') == 'read_only':
        return 'read_only', meta
    return 'ok', meta
