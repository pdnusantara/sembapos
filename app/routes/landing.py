import json
import re
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
    make_response,
)
from flask_login import current_user

from .. import db
from ..models import (
    TenantPackage,
    User,
    TutorialPageConfig,
    AffiliateApplication,
)
from ..affiliate_service import (
    get_affiliate_by_code,
    normalize_ref_code,
    log_affiliate_click,
    hash_ip_for_log,
    load_affiliate_settings,
)
from ..rate_limiting import allow_request, client_key
from ..trial_registration import run_trial_registration
from ..trial_constants import (
    SIMPLE_ANIMAL_PASSWORD_WORDS,
    TRIAL_JENIS_USAHA_CHOICES,
    JENIS_USAHA_KEY_TO_LABEL,
)
from ..permissions import PERMISSION_MODULES, _parse_tenant_package_modules
from ..tutorial_content import (
    TUTORIAL_CONFIG_SLUG,
    ensure_tutorial_page_config_default,
)

landing_bp = Blueprint('landing', __name__)

# Samakan ambang "tak terbatas" dengan superadmin (paket_index).
UNLIMITED_THRESHOLD = 9000

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
        ref_q = (request.args.get('ref') or '').strip()
        aff_ref = ref_q or (request.cookies.get('aff_ref') or '').strip()
        if ref_q:
            norm = normalize_ref_code(ref_q)
            if norm:
                aff_ref = norm
                aff = get_affiliate_by_code(norm)
                if aff:
                    log_affiliate_click(
                        aff.id,
                        norm,
                        hash_ip_for_log(request.remote_addr or ''),
                        request.path,
                    )
        html = render_template(
            'trial_register.html',
            jenis_usaha_choices=TRIAL_JENIS_USAHA_CHOICES,
            aff_ref=aff_ref,
            form_action=url_for('landing.trial_register'),
            show_aff_ref=True,
        )
        resp = make_response(html)
        if ref_q:
            norm = normalize_ref_code(ref_q)
            if norm:
                resp.set_cookie(
                    'aff_ref',
                    norm,
                    max_age=60 * 60 * 24 * 30,
                    samesite='Lax',
                    path='/',
                )
        return resp

    st = load_affiliate_settings()
    lim = int(st.get('trial_rate_per_hour') or 30)
    if not allow_request(client_key('trial', request.remote_addr or ''), lim, 3600):
        flash('Terlalu banyak percobaan pendaftaran. Silakan tunggu dan coba lagi.', 'danger')
        return redirect(url_for('landing.trial_register'))

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

    aff_ref_raw = (request.form.get('aff_ref') or request.cookies.get('aff_ref') or '').strip()
    resolved_affiliate = get_affiliate_by_code(aff_ref_raw) if aff_ref_raw else None
    affiliate_id_for_lead = resolved_affiliate.id if resolved_affiliate else None

    res = run_trial_registration(
        nama=nama,
        no_wa=no_wa,
        nama_toko=nama_toko,
        jenis_usaha=jenis_usaha,
        provinsi=provinsi,
        kabupaten=kabupaten,
        kecamatan=kecamatan,
        desa=desa,
        affiliate_id=affiliate_id_for_lead,
        attribution_source='trial',
        lead_source='trial_landing',
        auto_login=True,
        session_obj=session,
        simple_animal_words=SIMPLE_ANIMAL_PASSWORD_WORDS,
    )
    if not res.ok:
        flash(res.error_message or 'Gagal mendaftar.', 'danger')
        return redirect(url_for('landing.trial_register'))

    session['trial_success'] = {
        'username': res.admin_username,
        'password': res.plain_password,
        'tenant_nama': res.nama_toko,
        'kode_tenant': res.tenant_kode,
        'expired_label': res.trial_expired_label,
    }
    return redirect(url_for('landing.trial_success'))


@landing_bp.route('/daftar-afiliasi', methods=['GET', 'POST'])
def affiliate_apply():
    """Pendaftaran afiliasi eksternal (publik) — persetujuan Super Admin."""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
    settings = load_affiliate_settings()
    terms = (settings.get('terms_application_text') or '').strip()
    if request.method == 'GET':
        return render_template('landing/affiliate_apply.html', terms=terms)

    if terms and request.form.get('terms_ok') != '1':
        flash('Anda harus menyetujui syarat pendaftaran.', 'danger')
        return redirect(url_for('landing.affiliate_apply'))

    nama = (request.form.get('nama') or '').strip()
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    password2 = request.form.get('password2') or ''
    email = (request.form.get('email') or '').strip()
    telepon = (request.form.get('telepon') or '').strip()
    alasan = (request.form.get('alasan') or '').strip()

    if not nama or not username or len(password) < 8:
        flash('Nama, username, dan password (min 8 karakter) wajib diisi.', 'danger')
        return redirect(url_for('landing.affiliate_apply'))
    if password != password2:
        flash('Konfirmasi password tidak sama.', 'danger')
        return redirect(url_for('landing.affiliate_apply'))

    from ..routes.admin import validate_username

    u_ok, u_err = validate_username(username)
    if u_err:
        flash(u_err, 'danger')
        return redirect(url_for('landing.affiliate_apply'))

    if User.query.filter_by(username=u_ok).first() or AffiliateApplication.query.filter_by(
        username=u_ok, status='pending'
    ).first():
        flash('Username sudah dipakai atau sedang dalam antrean.', 'danger')
        return redirect(url_for('landing.affiliate_apply'))

    tmp = User(
        tenant_id=None,
        branch_id=None,
        nama=nama[:100],
        username=u_ok,
        role='affiliate',
    )
    tmp.set_password(password)

    db.session.add(
        AffiliateApplication(
            nama=nama[:120],
            email=email[:120] if email else None,
            telepon=telepon[:30] if telepon else None,
            username=u_ok,
            password_hash=tmp.password_hash,
            alasan=alasan[:2000] if alasan else None,
            status='pending',
        )
    )
    db.session.commit()
    flash('Lamaran afiliasi terkirim. Anda akan dihubungi setelah disetujui.', 'success')
    return redirect(url_for('landing.affiliate_apply'))


@landing_bp.route('/daftar-trial/sukses')
def trial_success():
    data = session.pop('trial_success', None)
    if data:
        return render_template('trial_success.html', **data)
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
    flash('Sesi pendaftaran tidak valid. Silakan daftar lagi.', 'warning')
    return redirect(url_for('landing.trial_register'))
