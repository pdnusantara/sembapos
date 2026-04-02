"""
Program afiliasi: pengaturan (AppSetting JSON), resolusi kode referral, komisi invoice, webhook.
"""
import hashlib
import json
import re
import secrets
import string
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import db
from .models import (
    Affiliate,
    AffiliateClick,
    AffiliateCommission,
    AppSetting,
    Tenant,
    TenantAffiliateAttribution,
    TenantInvoice,
    User,
)

AFFILIATE_SETTINGS_KEY = 'affiliate_settings_json'

DEFAULT_SETTINGS: Dict[str, Any] = {
    'program_enabled': True,
    'pct_tenant': 5.0,
    'pct_eksternal': 10.0,
    'min_payout': 0.0,
    'catatan_platform': '',
    'require_commission_approval': False,
    'webhook_url': '',
    'webhook_secret': '',
    'trial_rate_per_hour': 30,
    'affiliate_form_rate_per_hour': 15,
    'terms_affiliate_text': '',
    'terms_application_text': '',
    'abuse_wa_lookback_days': 7,
}


def load_affiliate_settings():
    raw = AppSetting.get(AFFILIATE_SETTINGS_KEY)
    data = dict(DEFAULT_SETTINGS)
    if raw and str(raw).strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if k not in data:
                        continue
                    if k in ('program_enabled', 'require_commission_approval'):
                        data[k] = bool(v)
                    elif k in ('pct_tenant', 'pct_eksternal', 'min_payout'):
                        try:
                            data[k] = float(v)
                        except (TypeError, ValueError):
                            pass
                    elif k in ('trial_rate_per_hour', 'affiliate_form_rate_per_hour', 'abuse_wa_lookback_days'):
                        try:
                            data[k] = int(float(v))
                        except (TypeError, ValueError):
                            pass
                    elif k in ('catatan_platform', 'webhook_url', 'webhook_secret', 'terms_affiliate_text', 'terms_application_text'):
                        data[k] = str(v) if v is not None else ''
        except (json.JSONDecodeError, TypeError):
            pass
    return data


def save_affiliate_settings(data: dict):
    merged = dict(DEFAULT_SETTINGS)
    merged.update(load_affiliate_settings())
    merged.update(data)
    AppSetting.set(AFFILIATE_SETTINGS_KEY, json.dumps(merged, ensure_ascii=False))
    return merged


def normalize_ref_code(code):
    if not code:
        return None
    s = str(code).strip().upper()
    if len(s) < 2 or len(s) > 32:
        return None
    if not re.match(r'^[A-Z0-9_-]+$', s):
        return None
    return s


def get_affiliate_by_code(code):
    norm = normalize_ref_code(code)
    if not norm:
        return None
    aff = Affiliate.query.filter(
        Affiliate.kode == norm,
        Affiliate.aktif.is_(True),
    ).first()
    if not aff:
        return None
    if aff.campaign_expires_at and datetime.utcnow() > aff.campaign_expires_at:
        return None
    return aff


def _random_suffix(n=4):
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(n))


def ensure_tenant_affiliate(tenant: Tenant) -> Affiliate:
    """Satu baris Affiliate jenis tenant per Tenant; buat jika belum ada."""
    existing = Affiliate.query.filter_by(
        jenis=Affiliate.JENIS_TENANT,
        tenant_id=tenant.id,
    ).first()
    if existing:
        return existing
    base = re.sub(r'[^A-Z0-9]', '', (tenant.kode or '').upper())[:8] or f'T{tenant.id}'
    for _ in range(40):
        kode = (f'{base}-{_random_suffix(4)}').upper()
        if len(kode) > 32:
            kode = kode[:32]
        if not Affiliate.query.filter_by(kode=kode).first():
            aff = Affiliate(
                kode=kode,
                jenis=Affiliate.JENIS_TENANT,
                tenant_id=tenant.id,
                user_id=None,
                nama_tampilan=tenant.nama[:120] if tenant.nama else '',
                aktif=True,
            )
            db.session.add(aff)
            db.session.flush()
            return aff
    raise RuntimeError('tidak dapat membuat kode affiliate unik')


def record_commission_for_paid_invoice(invoice: TenantInvoice) -> Optional[AffiliateCommission]:
    """
    Panggil saat invoice status menjadi paid. Idempoten per invoice.
    """
    settings = load_affiliate_settings()
    if not settings.get('program_enabled'):
        return None

    if invoice.status != 'paid':
        return None

    existing = AffiliateCommission.query.filter_by(tenant_invoice_id=invoice.id).first()
    if existing:
        return existing

    attr = TenantAffiliateAttribution.query.filter_by(tenant_id=invoice.tenant_id).first()
    if not attr:
        return None

    aff = db.session.get(Affiliate, attr.affiliate_id)
    if not aff or not aff.aktif:
        return None

    pct = (
        float(settings['pct_tenant'])
        if aff.jenis == Affiliate.JENIS_TENANT
        else float(settings['pct_eksternal'])
    )
    base = float(invoice.nominal or 0)
    amount = round(base * (pct / 100.0), 2)

    need_appr = bool(settings.get('require_commission_approval'))
    now = datetime.utcnow()
    if need_appr:
        st = 'menunggu'
        appr_at = None
    else:
        st = 'disetujui'
        appr_at = now

    row = AffiliateCommission(
        affiliate_id=aff.id,
        tenant_id=invoice.tenant_id,
        tenant_invoice_id=invoice.id,
        base_amount=base,
        commission_pct=pct,
        commission_amount=amount,
        status=st,
        created_at=now,
        approved_at=appr_at,
    )
    db.session.add(row)
    return row


def notify_commission_created(commission_id: int):
    """Panggil setelah commit DB (mis. dari billing_pay)."""
    settings = load_affiliate_settings()
    url = (settings.get('webhook_url') or '').strip()
    if not url:
        return
    row = db.session.get(AffiliateCommission, commission_id)
    if not row:
        return
    secret = (settings.get('webhook_secret') or '').strip()
    payload = {
        'event': 'affiliate.commission_created',
        'commission_id': row.id,
        'affiliate_id': row.affiliate_id,
        'tenant_id': row.tenant_id,
        'tenant_invoice_id': row.tenant_invoice_id,
        'commission_amount': row.commission_amount,
        'status': row.status,
    }
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    sig = hashlib.sha256(f'{secret}|{body.decode("utf-8")}'.encode()).hexdigest() if secret else ''
    headers = {'Content-Type': 'application/json', 'User-Agent': 'sembapos-affiliate/1.0'}
    if sig:
        headers['X-Affiliate-Signature'] = sig
    try:
        req = Request(url, data=body, headers=headers, method='POST')
        with urlopen(req, timeout=8) as resp:
            resp.read()
    except (URLError, OSError, TimeoutError, ValueError):
        pass


def create_external_affiliate_user(
    nama_tampilan: str,
    username: str,
    password: str,
    email: Optional[str] = None,
    telepon: Optional[str] = None,
    catatan: Optional[str] = None,
) -> Tuple[Optional[Affiliate], Optional[str]]:
    """Buat User role affiliate + baris Affiliate eksternal. Return (aff, error)."""
    if User.query.filter_by(username=username.strip()).first():
        return None, 'Username sudah dipakai.'
    u = User(
        tenant_id=None,
        branch_id=None,
        nama=nama_tampilan.strip()[:100] or username.strip()[:100],
        username=username.strip()[:50],
        role='affiliate',
        aktif=True,
    )
    u.set_password(password)
    db.session.add(u)
    db.session.flush()

    for _ in range(40):
        kode = f'EXT-{_random_suffix(6)}'
        if not Affiliate.query.filter_by(kode=kode).first():
            aff = Affiliate(
                kode=kode,
                jenis=Affiliate.JENIS_EKSTERNAL,
                tenant_id=None,
                user_id=u.id,
                nama_tampilan=nama_tampilan.strip()[:120],
                email=(email or '').strip()[:120] or None,
                telepon=(telepon or '').strip()[:30] or None,
                aktif=True,
                catatan=catatan,
            )
            db.session.add(aff)
            db.session.flush()
            return aff, None

    db.session.rollback()
    return None, 'Gagal membuat kode unik.'


def create_external_affiliate_user_hashed(
    nama_tampilan: str,
    username: str,
    password_hash: str,
    email: Optional[str] = None,
    telepon: Optional[str] = None,
) -> Tuple[Optional[Affiliate], Optional[str]]:
    """Buat affiliate dari hash password yang sudah ada (persetujuan lamaran)."""
    if User.query.filter_by(username=username.strip()).first():
        return None, 'Username sudah dipakai.'
    u = User(
        tenant_id=None,
        branch_id=None,
        nama=nama_tampilan.strip()[:100] or username.strip()[:100],
        username=username.strip()[:50],
        role='affiliate',
        aktif=True,
        password_hash=password_hash,
    )
    db.session.add(u)
    db.session.flush()

    for _ in range(40):
        kode = f'EXT-{_random_suffix(6)}'
        if not Affiliate.query.filter_by(kode=kode).first():
            aff = Affiliate(
                kode=kode,
                jenis=Affiliate.JENIS_EKSTERNAL,
                tenant_id=None,
                user_id=u.id,
                nama_tampilan=nama_tampilan.strip()[:120],
                email=(email or '').strip()[:120] or None,
                telepon=(telepon or '').strip()[:30] or None,
                aktif=True,
            )
            db.session.add(aff)
            db.session.flush()
            return aff, None

    db.session.rollback()
    return None, 'Gagal membuat kode unik.'


def hash_ip_for_log(remote_addr: str) -> str:
    return hashlib.sha256((remote_addr or '0').encode('utf-8')).hexdigest()[:32]


def log_affiliate_click(affiliate_id: Optional[int], kode: str, ip_hash: Optional[str], path: Optional[str] = None):
    """Catat klik referral (commit terpisah agar tidak mengganggu alur utama)."""
    try:
        db.session.add(
            AffiliateClick(
                affiliate_id=affiliate_id,
                kode_snapshot=(kode or '')[:32],
                ip_hash=(ip_hash or '')[:64] if ip_hash else None,
                path=(path or '')[:120] if path else None,
                created_at=datetime.utcnow(),
            )
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
