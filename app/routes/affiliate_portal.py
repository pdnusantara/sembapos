"""Dashboard afiliasi eksternal & pendaftaran tenant oleh affiliate."""
from flask import Blueprint, render_template, request, url_for, redirect, flash, session, current_app
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from .. import db
from ..models import Affiliate, AffiliateCommission, Tenant, TenantAffiliateAttribution, TenantInvoice
from ..affiliate_service import load_affiliate_settings
from ..subscription import subscription_state
from ..rate_limiting import allow_request, client_key
from ..trial_registration import run_trial_registration
from ..trial_constants import (
    SIMPLE_ANIMAL_PASSWORD_WORDS,
    TRIAL_JENIS_USAHA_CHOICES,
    JENIS_USAHA_KEY_TO_LABEL,
)

affiliate_bp = Blueprint('affiliate', __name__, url_prefix='/affiliate')

AFFILIATE_TENANT_PAGE_SIZE = 25

_SUB_PHASE_LABELS = {
    'none': 'Tanpa tgl. kedaluwarsa',
    'active': 'Langganan aktif',
    'grace': 'Masa tenggang',
    'enforced': 'Berakhir / dibatasi',
}


def _get_profile():
    return Affiliate.query.filter_by(user_id=current_user.id).first()


def _affiliate_tenant_rows_and_stats(affiliate_id: int, config):
    """
    Muat semua TenantAffiliateAttribution untuk affiliate + metrik per tenant.
    Return (stats_dict, list of row dicts).
    """
    attrs = (
        TenantAffiliateAttribution.query.filter_by(affiliate_id=affiliate_id)
        .options(
            joinedload(TenantAffiliateAttribution.tenant).joinedload(Tenant.subscription),
        )
        .order_by(TenantAffiliateAttribution.attributed_at.desc())
        .all()
    )
    if not attrs:
        empty_stats = {
            'n_total': 0,
            'n_langganan_aktif': 0,
            'n_grace': 0,
            'n_kedaluwarsa': 0,
            'n_tenant_nonaktif': 0,
            'n_pernah_bayar': 0,
            'n_belum_pernah_bayar': 0,
        }
        return empty_stats, []

    tenant_ids = [a.tenant_id for a in attrs]

    paid_rows = (
        db.session.query(TenantInvoice.tenant_id, func.count(TenantInvoice.id))
        .filter(
            TenantInvoice.tenant_id.in_(tenant_ids),
            TenantInvoice.status == 'paid',
        )
        .group_by(TenantInvoice.tenant_id)
        .all()
    )
    paid_map = {tid: int(c) for tid, c in paid_rows}

    sub_latest = (
        db.session.query(
            TenantInvoice.tenant_id,
            func.max(TenantInvoice.id).label('mid'),
        )
        .filter(TenantInvoice.tenant_id.in_(tenant_ids))
        .group_by(TenantInvoice.tenant_id)
        .subquery()
    )
    latest_invoices = (
        TenantInvoice.query.join(
            sub_latest,
            (TenantInvoice.tenant_id == sub_latest.c.tenant_id)
            & (TenantInvoice.id == sub_latest.c.mid),
        )
        .all()
    )
    latest_map = {}
    for inv in latest_invoices:
        latest_map.setdefault(inv.tenant_id, inv)

    comm_rows = (
        db.session.query(
            AffiliateCommission.tenant_id,
            func.coalesce(func.sum(AffiliateCommission.commission_amount), 0),
        )
        .filter(
            AffiliateCommission.affiliate_id == affiliate_id,
            AffiliateCommission.tenant_id.in_(tenant_ids),
        )
        .group_by(AffiliateCommission.tenant_id)
        .all()
    )
    comm_map = {tid: float(s or 0) for tid, s in comm_rows}

    rows = []
    n_langganan_aktif = n_grace = n_kedaluwarsa = n_tenant_nonaktif = 0
    n_pernah_bayar = n_belum_pernah_bayar = 0

    for attr in attrs:
        t = attr.tenant
        if not t:
            continue
        phase, _meta = subscription_state(t, config)
        if not t.aktif:
            n_tenant_nonaktif += 1
        if phase in ('active', 'none'):
            n_langganan_aktif += 1
        elif phase == 'grace':
            n_grace += 1
        elif phase == 'enforced':
            n_kedaluwarsa += 1

        pc = paid_map.get(t.id, 0)
        if pc > 0:
            n_pernah_bayar += 1
        else:
            n_belum_pernah_bayar += 1

        pkg = (t.subscription.nama if getattr(t, 'subscription', None) else None) or (t.paket or '—')
        latest = latest_map.get(t.id)
        rows.append(
            {
                'attr': attr,
                'tenant': t,
                'phase': phase,
                'phase_label': _SUB_PHASE_LABELS.get(phase, phase),
                'paket_label': pkg,
                'paid_count': pc,
                'latest_invoice_status': (latest.status if latest else None),
                'total_commission': comm_map.get(t.id, 0.0),
            }
        )

    stats = {
        'n_total': len(rows),
        'n_langganan_aktif': n_langganan_aktif,
        'n_grace': n_grace,
        'n_kedaluwarsa': n_kedaluwarsa,
        'n_tenant_nonaktif': n_tenant_nonaktif,
        'n_pernah_bayar': n_pernah_bayar,
        'n_belum_pernah_bayar': n_belum_pernah_bayar,
    }
    return stats, rows


def _filter_affiliate_tenant_rows(rows, status_f: str, q: str):
    out = list(rows)
    st = (status_f or '').strip()
    if st == 'langganan_aktif':
        out = [r for r in out if r['phase'] in ('active', 'none')]
    elif st == 'grace':
        out = [r for r in out if r['phase'] == 'grace']
    elif st == 'kedaluwarsa':
        out = [r for r in out if r['phase'] == 'enforced']
    elif st == 'pernah_bayar':
        out = [r for r in out if r['paid_count'] > 0]
    elif st == 'belum_pernah_bayar':
        out = [r for r in out if r['paid_count'] == 0]
    elif st == 'tenant_nonaktif':
        out = [r for r in out if not r['tenant'].aktif]

    qn = (q or '').strip()
    if qn:
        ql = qn.lower()
        out = [
            r
            for r in out
            if ql in (r['tenant'].nama or '').lower()
            or ql in (r['tenant'].kode or '').lower()
        ]
    return out


@affiliate_bp.route('/')
@login_required
def dashboard():
    if current_user.role != 'affiliate':
        return redirect(url_for('dashboard.index'))
    prof = _get_profile()
    if not prof:
        flash('Profil afiliasi tidak ditemukan. Hubungi administrator.', 'danger')
        return redirect(url_for('auth.logout'))
    settings = load_affiliate_settings()
    total_disetujui = (
        db.session.query(func.coalesce(func.sum(AffiliateCommission.commission_amount), 0))
        .filter(
            AffiliateCommission.affiliate_id == prof.id,
            AffiliateCommission.status.in_(('disetujui', 'dibayar', 'menunggu')),
        )
        .scalar()
        or 0
    )
    total_dibayar = (
        db.session.query(func.coalesce(func.sum(AffiliateCommission.commission_amount), 0))
        .filter(
            AffiliateCommission.affiliate_id == prof.id,
            AffiliateCommission.status == 'dibayar',
        )
        .scalar()
        or 0
    )
    menunggu_bayar = (
        db.session.query(func.coalesce(func.sum(AffiliateCommission.commission_amount), 0))
        .filter(
            AffiliateCommission.affiliate_id == prof.id,
            AffiliateCommission.status == 'disetujui',
        )
        .scalar()
        or 0
    )
    rows = (
        AffiliateCommission.query.filter_by(affiliate_id=prof.id)
        .options(joinedload(AffiliateCommission.invoice))
        .order_by(AffiliateCommission.created_at.desc())
        .limit(50)
        .all()
    )
    base = request.url_root.rstrip('/')
    link = f'{base}{url_for("landing.trial_register")}?ref={prof.kode}'
    tenant_stats, _ = _affiliate_tenant_rows_and_stats(prof.id, current_app.config)
    return render_template(
        'affiliate/dashboard.html',
        profile=prof,
        settings=settings,
        referral_link=link,
        rows=rows,
        total_disetujui=float(total_disetujui),
        total_dibayar=float(total_dibayar),
        menunggu_bayar=float(menunggu_bayar),
        tenant_stats=tenant_stats,
    )


@affiliate_bp.route('/tenant')
@login_required
def tenant_monitor():
    if current_user.role != 'affiliate':
        return redirect(url_for('dashboard.index'))
    prof = _get_profile()
    if not prof:
        flash('Profil afiliasi tidak ditemukan. Hubungi administrator.', 'danger')
        return redirect(url_for('auth.logout'))
    if not prof.aktif:
        flash('Profil afiliasi tidak aktif.', 'danger')
        return redirect(url_for('affiliate.dashboard'))

    settings = load_affiliate_settings()
    status_f = (request.args.get('status') or 'semua').strip()
    q = (request.args.get('q') or '').strip()
    page = max(1, request.args.get('page', 1, type=int))

    stats, all_rows = _affiliate_tenant_rows_and_stats(prof.id, current_app.config)
    filtered = _filter_affiliate_tenant_rows(all_rows, status_f if status_f != 'semua' else '', q)
    total_filtered = len(filtered)
    total_pages = max(1, (total_filtered + AFFILIATE_TENANT_PAGE_SIZE - 1) // AFFILIATE_TENANT_PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * AFFILIATE_TENANT_PAGE_SIZE
    page_rows = filtered[offset : offset + AFFILIATE_TENANT_PAGE_SIZE]

    return render_template(
        'affiliate/tenant_monitor.html',
        profile=prof,
        settings=settings,
        stats=stats,
        rows=page_rows,
        page=page,
        total_pages=total_pages,
        total_filtered=total_filtered,
        status_f=status_f,
        q=q,
    )


@affiliate_bp.route('/daftar-tenant', methods=['GET', 'POST'])
@login_required
def register_tenant():
    """Afiliator mendaftarkan tenant baru (sama seperti trial publik, tanpa login ke tenant baru)."""
    if current_user.role != 'affiliate':
        return redirect(url_for('dashboard.index'))
    prof = _get_profile()
    if not prof or not prof.aktif:
        flash('Profil afiliasi tidak aktif.', 'danger')
        return redirect(url_for('affiliate.dashboard'))

    settings = load_affiliate_settings()
    terms_txt = (settings.get('terms_affiliate_text') or '').strip()

    if request.method == 'GET':
        return render_template(
            'trial_register.html',
            jenis_usaha_choices=TRIAL_JENIS_USAHA_CHOICES,
            aff_ref='',
            form_action=url_for('affiliate.register_tenant'),
            show_aff_ref=False,
            terms_affiliate_text=terms_txt,
            wizard_title='Daftarkan tenant (trial)',
            wizard_intro='Isi data calon tenant — akun trial 30 hari; komisi mengikuti aturan platform.',
            affiliate_app=True,
            compact_tenant_wizard=True,
        )

    lim = int(settings.get('affiliate_form_rate_per_hour') or 15)
    if not allow_request(
        client_key('aff_tenant', request.remote_addr or '', str(prof.id)),
        lim,
        3600,
    ):
        flash('Terlalu banyak pendaftaran. Silakan tunggu dan coba lagi.', 'danger')
        return redirect(url_for('affiliate.register_tenant'))

    if terms_txt and request.form.get('terms_ok') != '1':
        flash('Anda harus menyetujui syarat program afiliasi.', 'danger')
        return redirect(url_for('affiliate.register_tenant'))

    nama = (request.form.get('nama') or '').strip()
    no_wa = (request.form.get('no_wa') or '').strip()
    nama_toko = (request.form.get('nama_toko') or '').strip()
    jenis_key = (request.form.get('jenis_usaha') or '').strip()
    provinsi = (request.form.get('provinsi') or '').strip()
    kabupaten = (request.form.get('kabupaten') or '').strip()
    kecamatan = (request.form.get('kecamatan') or '').strip()
    desa = (request.form.get('desa') or '').strip()
    jenis_usaha = JENIS_USAHA_KEY_TO_LABEL.get(jenis_key, '')

    if not nama or not no_wa or not nama_toko or not jenis_usaha:
        flash('Data diri, jenis usaha, dan lokasi wajib dilengkapi.', 'danger')
        return redirect(url_for('affiliate.register_tenant'))
    if not provinsi or not kabupaten or not kecamatan:
        flash('Provinsi, kabupaten/kota, dan kecamatan wajib dipilih.', 'danger')
        return redirect(url_for('affiliate.register_tenant'))

    res = run_trial_registration(
        nama=nama,
        no_wa=no_wa,
        nama_toko=nama_toko,
        jenis_usaha=jenis_usaha,
        provinsi=provinsi,
        kabupaten=kabupaten,
        kecamatan=kecamatan,
        desa=desa,
        affiliate_id=prof.id,
        attribution_source='affiliate_form',
        lead_source='affiliate_portal',
        auto_login=False,
        session_obj=None,
        simple_animal_words=SIMPLE_ANIMAL_PASSWORD_WORDS,
    )
    if not res.ok:
        flash(res.error_message or 'Gagal mendaftarkan tenant.', 'danger')
        return redirect(url_for('affiliate.register_tenant'))

    session['affiliate_trial_success'] = {
        'username': res.admin_username,
        'password': res.plain_password,
        'tenant_nama': res.nama_toko,
        'kode_tenant': res.tenant_kode,
        'expired_label': res.trial_expired_label,
    }
    return redirect(url_for('affiliate.trial_success'))


@affiliate_bp.route('/daftar-tenant/sukses')
@login_required
def trial_success():
    if current_user.role != 'affiliate':
        return redirect(url_for('dashboard.index'))
    data = session.pop('affiliate_trial_success', None)
    if data:
        return render_template(
            'trial_success.html',
            affiliate_app=True,
            trial_success_mode='affiliate_portal',
            **data,
        )
    flash('Sesi tidak valid.', 'warning')
    return redirect(url_for('affiliate.register_tenant'))
