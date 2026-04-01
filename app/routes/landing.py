import json
import re
import secrets
import string
from datetime import datetime, timedelta, time
from urllib.error import URLError
from urllib.request import Request, urlopen

from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    session,
    jsonify,
    current_app,
)
from flask_login import current_user, login_user

from .. import db
from ..models import (
    Tenant,
    TenantPackage,
    TenantPlanHistory,
    User,
    Branch,
    LeadCapture,
    TutorialPageConfig,
)
from ..permissions import PERMISSION_MODULES, _parse_tenant_package_modules
from ..subscription import tenant_login_allowed
from ..tutorial_content import (
    TUTORIAL_CONFIG_SLUG,
    ensure_tutorial_page_config_default,
)

landing_bp = Blueprint('landing', __name__)

# Samakan ambang "tak terbatas" dengan superadmin (paket_index).
UNLIMITED_THRESHOLD = 9000

SIMPLE_ANIMAL_PASSWORD_WORDS = (
    'kucing', 'kelinci', 'beruang', 'harimau', 'gajah', 'zebra', 'panda',
    'koala', 'rusa', 'serigala', 'elang', 'lumba', 'paus', 'kuda', 'merpati',
    'kancil', 'komodo', 'badak', 'rubah', 'lebah',
)

# Dropdown jenis usaha (nilai disimpan ke lead / konsisten)
TRIAL_JENIS_USAHA_CHOICES = (
    ('warung_sembako', 'Warung sembako / grocery'),
    ('minimarket', 'Minimarket'),
    ('toko_kelontong', 'Toko kelontong'),
    ('retail_umum', 'Retail / toko serba ada'),
    ('distributor_grosir', 'Distributor / grosir'),
    ('frozen_snack', 'Frozen food & snack'),
    ('lainnya', 'Lainnya'),
)
JENIS_USAHA_KEY_TO_LABEL = dict(TRIAL_JENIS_USAHA_CHOICES)

WILAYAH_API_BASE = 'https://www.emsifa.com/api-wilayah-indonesia/api'


def _normalize_wilayah_items(items):
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
    req = Request(url, headers={'User-Agent': 'sembapos-landing/1.0'})
    try:
        with urlopen(req, timeout=12) as resp:
            data = resp.read().decode('utf-8')
            parsed = json.loads(data)
            if isinstance(parsed, list):
                return _normalize_wilayah_items(parsed)
            return []
    except (URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return []


def _format_rp(n):
    try:
        x = float(n or 0)
    except (TypeError, ValueError):
        return '—'
    if x <= 0:
        return '—'
    return 'Rp ' + f'{int(round(x)):,}'.replace(',', '.')


def _digits_only(s):
    return re.sub(r'\D', '', (s or '').strip())


def _generate_trial_kode():
    """TRIAL + 4 karakter alfanumerik, unik di tenants.kode."""
    chars = string.ascii_uppercase + string.digits
    for _ in range(80):
        kode = 'TRIAL' + ''.join(secrets.choice(chars) for _ in range(4))
        if not Tenant.query.filter_by(kode=kode).first():
            return kode
    raise RuntimeError('tidak dapat membuat kode trial unik')


def _generate_trial_password():
    # Format sederhana agar mudah dibacakan: <hewan><2digit>
    return f"{secrets.choice(SIMPLE_ANIMAL_PASSWORD_WORDS)}{secrets.randbelow(100):02d}"


def _username_from_store_name(store_name: str, max_len: int = 50) -> str:
    """
    Buat username dari nama toko:
      - normalisasi jadi [a-z0-9_]
      - panjang 3-50
      - unik: jika bentrok, tambahkan suffix _01, _02, ...
    """
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
    """TenantPlanHistory.actor_user_id wajib — pakai superadmin pertama."""
    u = User.query.filter_by(role='superadmin', aktif=True).order_by(User.id).first()
    return u.id if u else None


def _record_trial_plan_history(tenant_id, pkg, max_cabang, max_user, actor_id):
    if not actor_id:
        return
    db.session.add(TenantPlanHistory(
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
    ))


def _landing_package_rows():
    rows = []
    packages = (
        TenantPackage.query.filter_by(aktif=True)
        .order_by(TenantPackage.sort_order, TenantPackage.id)
        .all()
    )
    popular_id = None
    for p in packages:
        if p.kode and str(p.kode).lower() == 'pro':
            popular_id = p.id
            break
    if popular_id is None and len(packages) >= 2:
        popular_id = packages[1].id

    for p in packages:
        feats = []
        if p.max_cabang >= UNLIMITED_THRESHOLD:
            feats.append('Cabang tidak terbatas (kuota)')
        else:
            feats.append(f'Maksimal {p.max_cabang} cabang')
        if p.max_user >= UNLIMITED_THRESHOLD:
            feats.append('Pengguna tidak terbatas (kuota)')
        else:
            feats.append(f'Maksimal {p.max_user} pengguna')

        cap = _parse_tenant_package_modules(p.modules_json)
        if cap is None:
            feats.append('Semua modul aplikasi (sesuai izin per pengguna)')
        else:
            labels = [lbl for code, lbl in PERMISSION_MODULES if code in cap]
            if not labels:
                feats.append('Modul sesuai konfigurasi paket')
            else:
                max_show = 8
                shown = labels[:max_show]
                tail = len(labels) - max_show
                mod_txt = ', '.join(shown)
                if tail > 0:
                    mod_txt += f' (+{tail} lainnya)'
                feats.append(f'Modul: {mod_txt}')

        hb = _format_rp(p.harga_bulanan)
        ht = _format_rp(p.harga_tahunan)
        price_lines = []
        if hb != '—':
            price_lines.append(f'{hb} / bulan')
        if ht != '—':
            price_lines.append(f'{ht} / tahun')
        if not price_lines:
            price_lines.append('Harga: hubungi kami')

        rows.append({
            'id': p.id,
            'nama': p.nama,
            'kode': p.kode,
            'deskripsi': (p.deskripsi or '').strip() or f'Paket {p.nama}.',
            'features': feats,
            'price_lines': price_lines,
            'is_popular': p.id == popular_id,
        })
    return rows


@landing_bp.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
    landing_packages = _landing_package_rows()
    return render_template('landing.html', landing_packages=landing_packages)


@landing_bp.route('/tutorial')
def tutorial():
    try:
        cfg = ensure_tutorial_page_config_default(slug=TUTORIAL_CONFIG_SLUG, aktif=True)
        tutorial_data = json.loads(cfg.data_json or '{}')
        return render_template('tutorial_dynamic.html', tutorial=tutorial_data)
    except Exception:
        # Fallback: jika DB/seed gagal, tetap tampil versi statis.
        return render_template('tutorial.html')


@landing_bp.route('/daftar-trial/wilayah/provinsi')
def trial_wilayah_provinsi():
    return jsonify(_fetch_wilayah_json('provinces.json'))


@landing_bp.route('/daftar-trial/wilayah/kabupaten/<prov_id>')
def trial_wilayah_kabupaten(prov_id):
    return jsonify(_fetch_wilayah_json(f'regencies/{prov_id}.json'))


@landing_bp.route('/daftar-trial/wilayah/kecamatan/<kab_id>')
def trial_wilayah_kecamatan(kab_id):
    return jsonify(_fetch_wilayah_json(f'districts/{kab_id}.json'))


@landing_bp.route('/daftar-trial/wilayah/desa/<kec_id>')
def trial_wilayah_desa(kec_id):
    return jsonify(_fetch_wilayah_json(f'villages/{kec_id}.json'))


@landing_bp.route('/daftar-trial', methods=['GET', 'POST'])
def trial_register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    if request.method == 'GET':
        return render_template(
            'trial_register.html',
            jenis_usaha_choices=TRIAL_JENIS_USAHA_CHOICES,
        )

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
        return redirect(url_for('landing.trial_register'))
    if not provinsi or not kabupaten or not kecamatan:
        flash('Provinsi, kabupaten/kota, dan kecamatan wajib dipilih.', 'danger')
        return redirect(url_for('landing.trial_register'))

    wa_key = _digits_only(no_wa)
    if len(wa_key) < 10:
        flash('Nomor WhatsApp tidak valid (minimal 10 digit).', 'danger')
        return redirect(url_for('landing.trial_register'))

    now = datetime.utcnow()
    existing_leads = LeadCapture.query.filter(
        LeadCapture.trial_tenant_id.isnot(None),
        LeadCapture.trial_expired_at.isnot(None),
        LeadCapture.trial_expired_at > now,
    ).all()
    for lead in existing_leads:
        if _digits_only(lead.no_wa) == wa_key:
            flash(
                'Nomor ini sudah memiliki trial aktif. Silakan login atau hubungi kami jika butuh bantuan.',
                'warning',
            )
            return redirect(url_for('landing.trial_register'))

    pkg = TenantPackage.query.filter_by(kode='basic', aktif=True).first()
    if not pkg:
        pkg = TenantPackage.query.filter_by(aktif=True).order_by(
            TenantPackage.sort_order, TenantPackage.id,
        ).first()
    if not pkg:
        flash('Layanan trial sedang tidak tersedia (belum ada paket aktif). Hubungi administrator.', 'danger')
        return redirect(url_for('landing.trial_register'))

    max_cabang = pkg.max_cabang
    max_user = pkg.max_user
    actor_id = _system_actor_user_id()
    if not actor_id:
        flash('Trial tidak dapat diproses: belum ada akun super admin di sistem.', 'danger')
        return redirect(url_for('landing.trial_register'))

    kode = _generate_trial_kode()
    trial_end = now + timedelta(days=30)
    trial_end_eod = datetime.combine(trial_end.date(), time(23, 59, 59))

    admin_username = _username_from_store_name(nama_toko)

    plain_password = _generate_trial_password()

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

        lead = LeadCapture(
            nama=nama[:120],
            no_wa=no_wa[:30],
            jenis_usaha=jenis_usaha[:120],
            catatan=f'Toko: {nama_toko}',
            source='trial_landing',
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
        )
        db.session.add(lead)

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f'Gagal membuat akun trial: {str(e)[:200]}', 'danger')
        return redirect(url_for('landing.trial_register'))

    try:
        db.session.refresh(admin)
        tenant_check = Tenant.query.get(tenant.id)
        if tenant_check:
            ok_login, _sub = tenant_login_allowed(tenant_check, current_app.config)
            if ok_login:
                login_user(admin, remember=False)
                admin.last_login = datetime.utcnow()
                db.session.commit()
                session['user_session_version'] = int(getattr(admin, 'session_version', 0) or 0)
                session['tenant_id'] = admin.tenant_id
                session['branch_id'] = admin.branch_id
    except Exception:
        db.session.rollback()

    session['trial_success'] = {
        'username': admin_username,
        'password': plain_password,
        'tenant_nama': nama_toko,
        'kode_tenant': kode,
        'expired_label': trial_end_eod.strftime('%d/%m/%Y %H:%M') + ' UTC',
    }
    return redirect(url_for('landing.trial_success'))


@landing_bp.route('/daftar-trial/sukses')
def trial_success():
    data = session.pop('trial_success', None)
    if data:
        return render_template('trial_success.html', **data)
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
    flash('Sesi pendaftaran tidak valid. Silakan daftar lagi.', 'warning')
    return redirect(url_for('landing.trial_register'))
