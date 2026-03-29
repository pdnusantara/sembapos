from flask import Blueprint, render_template, redirect, url_for
from flask_login import current_user

from ..models import TenantPackage
from ..permissions import PERMISSION_MODULES, _parse_tenant_package_modules

landing_bp = Blueprint('landing', __name__)

# Samakan ambang "tak terbatas" dengan superadmin (paket_index).
UNLIMITED_THRESHOLD = 9000


def _format_rp(n):
    try:
        x = float(n or 0)
    except (TypeError, ValueError):
        return '—'
    if x <= 0:
        return '—'
    return 'Rp ' + f'{int(round(x)):,}'.replace(',', '.')


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
    return render_template('tutorial.html')
