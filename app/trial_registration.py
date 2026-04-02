"""
Logika bersama pendaftaran trial (landing publik & formulir afiliasi).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from typing import Optional

from flask import current_app
from flask_login import login_user

from . import db
from .models import (
    Branch,
    LeadCapture,
    Tenant,
    TenantAffiliateAttribution,
    TenantPackage,
    TenantPlanHistory,
    User,
)

@dataclass
class TrialRegistrationResult:
    ok: bool
    error_message: str | None = None
    admin_username: str | None = None
    plain_password: str | None = None
    tenant_kode: str | None = None
    nama_toko: str | None = None
    trial_expired_label: str | None = None
    tenant_id: int | None = None
    admin_user_id: int | None = None


def _digits_only(s):
    return re.sub(r'\D', '', (s or '').strip())


def _generate_trial_kode():
    import secrets
    import string

    chars = string.ascii_uppercase + string.digits
    for _ in range(80):
        kode = 'TRIAL' + ''.join(secrets.choice(chars) for _ in range(4))
        if not Tenant.query.filter_by(kode=kode).first():
            return kode
    raise RuntimeError('tidak dapat membuat kode trial unik')


def _generate_trial_password(simple_animal_words):
    import secrets

    return f"{secrets.choice(simple_animal_words)}{secrets.randbelow(100):02d}"


def _username_from_store_name(store_name: str, max_len: int = 50) -> str:
    import secrets
    import re

    raw = (store_name or '').strip().lower()
    base = re.sub(r'[^a-z0-9]+', '_', raw)
    base = re.sub(r'_+', '_', base).strip('_')
    if not base:
        base = 'toko'
    if len(base) < 3:
        base = (base + f'{secrets.randbelow(100):02d}')[:max_len]
    if len(base) > max_len:
        base = base[:max_len]

    candidate = base
    counter = 1
    while User.query.filter_by(username=candidate).first():
        suffix = f'_{counter:02d}'
        candidate = (base[: max_len - len(suffix)] + suffix)[:max_len]
        counter += 1
        if counter > 999:
            return candidate
    return candidate


def _system_actor_user_id():
    u = User.query.filter_by(role='superadmin', aktif=True).order_by(User.id).first()
    return u.id if u else None


def _record_trial_plan_history(tenant_id, pkg, max_cabang, max_user, actor_id):
    if not actor_id:
        return
    db.session.add(
        TenantPlanHistory(
            tenant_id=tenant_id,
            actor_user_id=actor_id,
            event='tenant_create',
            old_paket_id=None,
            new_paket_id=pkg.id,
            old_paket_kode=None,
            new_paket_kode=pkg.kode,
            old_max_cabang=None,
            new_max_cabang=max_cabang,
            old_max_user=None,
            new_max_user=max_user,
        )
    )


def run_trial_registration(
    *,
    nama: str,
    no_wa: str,
    nama_toko: str,
    jenis_usaha: str,
    provinsi: str,
    kabupaten: str,
    kecamatan: str,
    desa: str,
    affiliate_id: Optional[int],
    attribution_source: str,
    lead_source: str,
    auto_login: bool,
    session_obj,
    simple_animal_words: tuple,
) -> TrialRegistrationResult:
    """
    affiliate_id: jika set, TenantAffiliateAttribution + LeadCapture.affiliate_id.
    attribution_source: 'trial' | 'affiliate_form' | ...
    lead_source: 'trial_landing' | 'affiliate_portal' | 'affiliate_tenant_admin'
    """
    from .subscription import tenant_login_allowed

    now = datetime.utcnow()
    wa_key = _digits_only(no_wa)
    if len(wa_key) < 10:
        return TrialRegistrationResult(ok=False, error_message='Nomor WhatsApp tidak valid (minimal 10 digit).')

    existing_leads = LeadCapture.query.filter(
        LeadCapture.trial_tenant_id.isnot(None),
        LeadCapture.trial_expired_at.isnot(None),
        LeadCapture.trial_expired_at > now,
    ).all()
    for lead in existing_leads:
        if _digits_only(lead.no_wa) == wa_key:
            return TrialRegistrationResult(
                ok=False,
                error_message='Nomor ini sudah memiliki trial aktif. Silakan login atau hubungi kami jika butuh bantuan.',
            )

    pkg = TenantPackage.query.filter_by(kode='basic', aktif=True).first()
    if not pkg:
        pkg = TenantPackage.query.filter_by(aktif=True).order_by(
            TenantPackage.sort_order, TenantPackage.id,
        ).first()
    if not pkg:
        return TrialRegistrationResult(ok=False, error_message='Layanan trial sedang tidak tersedia (belum ada paket aktif). Hubungi administrator.')

    max_cabang = pkg.max_cabang
    max_user = pkg.max_user
    actor_id = _system_actor_user_id()
    if not actor_id:
        return TrialRegistrationResult(ok=False, error_message='Trial tidak dapat diproses: belum ada akun super admin di sistem.')

    kode = _generate_trial_kode()
    trial_end = now + timedelta(days=30)
    trial_end_eod = datetime.combine(trial_end.date(), time(23, 59, 59))

    admin_username = _username_from_store_name(nama_toko)
    plain_password = _generate_trial_password(simple_animal_words)

    try:
        tenant = Tenant(
            nama=nama_toko[:100],
            kode=kode,
            alamat='',
            provinsi=provinsi[:100] if provinsi else None,
            kab_kota=kabupaten[:100] if kabupaten else None,
            kecamatan=kecamatan[:100] if kecamatan else None,
            desa=desa[:100] if desa else None,
            telepon=no_wa[:20] if no_wa else '',
            email=None,
            paket_id=pkg.id,
            paket=pkg.kode,
            max_cabang=max_cabang,
            max_user=max_user,
            tanggal_expired=trial_end_eod,
            timezone='Asia/Jakarta',
        )
        db.session.add(tenant)
        db.session.flush()

        branch = Branch(
            tenant_id=tenant.id,
            nama='Cabang Utama',
            kode='MAIN',
            alamat='',
        )
        db.session.add(branch)
        db.session.flush()

        admin = User(
            tenant_id=tenant.id,
            branch_id=branch.id,
            nama='Admin ' + nama_toko[:80],
            username=admin_username,
            role='admin',
        )
        admin.set_password(plain_password)
        db.session.add(admin)

        _record_trial_plan_history(tenant.id, pkg, max_cabang, max_user, actor_id)

        if affiliate_id:
            db.session.add(
                TenantAffiliateAttribution(
                    tenant_id=tenant.id,
                    affiliate_id=affiliate_id,
                    sumber=attribution_source,
                )
            )

        lead = LeadCapture(
            nama=nama[:120],
            no_wa=no_wa[:30],
            jenis_usaha=jenis_usaha[:120],
            catatan=f'Toko: {nama_toko}',
            source=lead_source,
            status='converted',
            provinsi=provinsi[:100] if provinsi else None,
            kabupaten=kabupaten[:100] if kabupaten else None,
            kecamatan=kecamatan[:100] if kecamatan else None,
            desa=desa[:100] if desa else None,
            trial_tenant_id=tenant.id,
            trial_username=admin_username,
            trial_password=plain_password,
            trial_expired_at=trial_end_eod,
            trial_created_at=now,
            affiliate_id=affiliate_id,
        )
        db.session.add(lead)

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return TrialRegistrationResult(ok=False, error_message=f'Gagal membuat akun trial: {str(e)[:200]}')

    if auto_login and session_obj is not None:
        try:
            db.session.refresh(admin)
            tenant_check = db.session.get(Tenant, tenant.id)
            if tenant_check:
                ok_login, _sub = tenant_login_allowed(tenant_check, current_app.config)
                if ok_login:
                    login_user(admin, remember=False)
                    admin.last_login = datetime.utcnow()
                    db.session.commit()
                    session_obj['user_session_version'] = int(getattr(admin, 'session_version', 0) or 0)
                    session_obj['tenant_id'] = admin.tenant_id
                    session_obj['branch_id'] = admin.branch_id
        except Exception:
            db.session.rollback()

    expired_label = trial_end_eod.strftime('%d/%m/%Y %H:%M') + ' UTC'
    return TrialRegistrationResult(
        ok=True,
        admin_username=admin_username,
        plain_password=plain_password,
        tenant_kode=kode,
        nama_toko=nama_toko,
        trial_expired_label=expired_label,
        tenant_id=tenant.id,
        admin_user_id=admin.id,
    )
