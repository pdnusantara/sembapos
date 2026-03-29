"""Zona waktu Indonesia yang didukung aplikasi (IANA + label singkat)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None  # type: ignore[misc, assignment]

DEFAULT_TIMEZONE = 'Asia/Jakarta'

# (value IANA, label UI)
INDONESIA_TIMEZONE_CHOICES = (
    ('Asia/Jakarta', 'WIB — Waktu Indonesia Barat (Jakarta)'),
    ('Asia/Makassar', 'WITA — Waktu Indonesia Tengah (Makassar)'),
    ('Asia/Jayapura', 'WIT — Waktu Indonesia Timur (Jayapura)'),
)

ALLOWED_TIMEZONE_IDS = frozenset(v for v, _ in INDONESIA_TIMEZONE_CHOICES)

SHORT_LABELS = {
    'Asia/Jakarta': 'WIB',
    'Asia/Makassar': 'WITA',
    'Asia/Jayapura': 'WIT',
}


def normalize_timezone_id(raw: str | None) -> str:
    s = (raw or '').strip()
    if s in ALLOWED_TIMEZONE_IDS:
        return s
    return DEFAULT_TIMEZONE


def timezone_short_label(tz_id: str | None) -> str:
    return SHORT_LABELS.get(normalize_timezone_id(tz_id), 'WIB')


def timezone_display_label(tz_id: str | None) -> str:
    tid = normalize_timezone_id(tz_id)
    return f'{SHORT_LABELS.get(tid, "WIB")} ({tid})'


def get_zoneinfo(tz_id: str | None):
    """Return ZoneInfo atau None jika tidak tersedia (tzdata hilang)."""
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(normalize_timezone_id(tz_id))
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def get_zoneinfo_required(tz_id: str | None):
    """ZoneInfo untuk tz_id; fallback aman ke DEFAULT_TIMEZONE."""
    z = get_zoneinfo(normalize_timezone_id(tz_id))
    if z is not None:
        return z
    if ZoneInfo is None:
        raise RuntimeError('zoneinfo tidak tersedia')
    return ZoneInfo(DEFAULT_TIMEZONE)


def resolve_effective_timezone_id(user) -> str:
    """Zona tampilan/filter: superadmin → user.timezone; user tenant → tenant.timezone."""
    if user is None:
        return normalize_timezone_id(None)
    if getattr(user, 'is_authenticated', True) is False:
        return normalize_timezone_id(None)
    if getattr(user, 'is_superadmin', False):
        return normalize_timezone_id(getattr(user, 'timezone', None))
    t = getattr(user, 'tenant', None)
    raw = getattr(t, 'timezone', None) if t is not None else None
    return normalize_timezone_id(raw)


def resolve_effective_zoneinfo(user):
    """(ZoneInfo, tz_id) untuk user saat ini."""
    tz_id = resolve_effective_timezone_id(user)
    return get_zoneinfo_required(tz_id), tz_id


def utc_naive_bounds_for_local_date(local_date: date, tz_id: str) -> tuple[datetime, datetime]:
    """Batas hari kalender di tz_id sebagai UTC-naive (kolom DB = UTC naive)."""
    zi = get_zoneinfo_required(tz_id)
    start_local = datetime.combine(local_date, time.min, tzinfo=zi)
    end_local = datetime.combine(local_date, time(23, 59, 59, 999999), tzinfo=zi)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


def local_today_date(tz_id: str) -> date:
    aware_utc = datetime.now(timezone.utc)
    zi = get_zoneinfo_required(tz_id)
    return aware_utc.astimezone(zi).date()


def local_yyyymmdd_for_tenant_id(tenant_id) -> str:
    """Segmen tanggal untuk prefix nomor dokumen (kalender zona tenant)."""
    from .models import Tenant

    t = Tenant.query.get(tenant_id) if tenant_id else None
    tz_id = normalize_timezone_id(getattr(t, 'timezone', None)) if t else None
    return local_today_date(tz_id).strftime('%Y%m%d')


def format_utc_naive_as_local(dt, tz_id: str | None, fmt: str = '%d/%m/%Y %H:%M') -> str:
    """Format datetime UTC-naive ke string di zona tz_id."""
    if dt is None:
        return ''
    tid = normalize_timezone_id(tz_id)
    zi = get_zoneinfo_required(tid)
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return aware.astimezone(zi).strftime(fmt)


def parse_ymd_to_date(s: str | None) -> date | None:
    if not s or not str(s).strip():
        return None
    try:
        return datetime.strptime(str(s).strip()[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def utc_naive_bounds_for_report_period(
    mode: str, tanggal: str | None, bulan: str | None, tz_id: str
) -> tuple[datetime, datetime]:
    """Periode laporan penjualan: harian / bulanan → UTC-naive start & end."""
    m = (mode or 'harian').strip().lower()
    if m == 'bulanan':
        try:
            base = datetime.strptime((bulan or '')[:7], '%Y-%m')
            y, mo = base.year, base.month
        except ValueError:
            td = local_today_date(tz_id)
            y, mo = td.year, td.month
        return utc_naive_bounds_for_local_month(y, mo, tz_id)
    d = parse_ymd_to_date(tanggal)
    if d is None:
        d = local_today_date(tz_id)
    return utc_naive_bounds_for_local_date(d, tz_id)


def utc_naive_bounds_for_local_month(year: int, month: int, tz_id: str) -> tuple[datetime, datetime]:
    """Awal bulan sampai akhir bulan (kalender lokal tz_id) sebagai UTC-naive."""
    zi = get_zoneinfo_required(tz_id)
    start_local = datetime(year, month, 1, 0, 0, 0, 0, tzinfo=zi)
    if month == 12:
        next_month = datetime(year + 1, 1, 1, 0, 0, 0, 0, tzinfo=zi)
    else:
        next_month = datetime(year, month + 1, 1, 0, 0, 0, 0, tzinfo=zi)
    end_local = next_month - timedelta(microseconds=1)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


def utc_naive_bounds_for_local_date_range(
    date_from: date | None,
    date_to: date | None,
    tz_id: str,
    *,
    default_days_back: int = 29,
) -> tuple[datetime, datetime]:
    """Rentang filter: tanggal kalender lokal → between(utc_start, utc_end)."""
    today = local_today_date(tz_id)
    if date_to is None:
        d_end = today
    else:
        d_end = date_to
    if date_from is None:
        d_start = d_end - timedelta(days=default_days_back)
    else:
        d_start = date_from
    if d_start > d_end:
        d_start, d_end = d_end, d_start
    start_utc, _ = utc_naive_bounds_for_local_date(d_start, tz_id)
    _, end_utc = utc_naive_bounds_for_local_date(d_end, tz_id)
    return start_utc, end_utc
