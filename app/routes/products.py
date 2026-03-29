import csv
import io
import os
import secrets
import uuid
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, current_app, jsonify
from flask_login import login_required, current_user
from sqlalchemy import or_, func, nullslast

from .. import db
from ..models import Product, ProductCategory, Etalase, StockMovement, Supplier, ProductAuditLog, InventoryCostLayer
from ..fifo_costing import create_cost_layer
from ..timezones import local_today_date, resolve_effective_timezone_id

products_bp = Blueprint('products', __name__, url_prefix='/products')


def require_admin():
    if current_user.role not in ['superadmin', 'admin']:
        flash('Akses ditolak!', 'danger')
        return False
    return True


def _etalases_for_tenant(tenant_id):
    return Etalase.query.filter_by(tenant_id=tenant_id).order_by(Etalase.nama).all()


def _etalase_id_from_post(tenant_id):
    raw = (request.form.get('etalase_id') or '').strip()
    if not raw:
        return None
    try:
        eid = int(raw)
    except ValueError:
        return None
    if eid <= 0:
        return None
    if not Etalase.query.filter_by(id=eid, tenant_id=tenant_id).first():
        return None
    return eid


def _harga_coret_from_form():
    raw = (request.form.get('harga_coret') or '').strip()
    if not raw:
        return None
    try:
        v = float(str(raw).replace(',', '.'))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _norm_barcode(val):
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def barcode_taken(tenant_id, barcode, exclude_id=None):
    bc = _norm_barcode(barcode)
    if not bc:
        return False
    q = Product.query.filter_by(tenant_id=tenant_id, barcode=bc)
    if exclude_id:
        q = q.filter(Product.id != exclude_id)
    return q.first() is not None


def _generate_unique_barcode(tenant_id, exclude_id=None):
    """Kode angka 12 digit (awalan 2) untuk scan CODE128/EAN-style internal, unik per tenant."""
    for _ in range(120):
        body = secrets.randbelow(10**11)
        code = '2' + f'{body:011d}'
        if not barcode_taken(tenant_id, code, exclude_id=exclude_id):
            return code
    return None


def _to_float_or_none(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace(',', '.')
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _extract_price_tiers(form, base_price):
    """Normalisasi input tier harga grosir dari form."""
    min1 = _to_float_or_none(form.get('min_qty_grosir_1'))
    price1 = _to_float_or_none(form.get('harga_jual_grosir_1'))
    min2 = _to_float_or_none(form.get('min_qty_grosir_2'))
    price2 = _to_float_or_none(form.get('harga_jual_grosir_2'))

    # Jika salah satu kosong, anggap tier itu tidak aktif
    if min1 is None or price1 is None:
        min1 = None
        price1 = None
    if min2 is None or price2 is None:
        min2 = None
        price2 = None

    if min1 is not None and min1 <= 1:
        raise ValueError('Min qty grosir 1 harus lebih dari 1.')
    if min2 is not None and min2 <= 1:
        raise ValueError('Min qty grosir 2 harus lebih dari 1.')
    if price1 is not None and price1 < 0:
        raise ValueError('Harga grosir 1 tidak valid.')
    if price2 is not None and price2 < 0:
        raise ValueError('Harga grosir 2 tidak valid.')

    # Tier 2 harus dimulai di qty lebih besar dari tier 1
    if min1 is not None and min2 is not None and min2 <= min1:
        raise ValueError('Min qty grosir 2 harus lebih besar dari grosir 1.')

    # Guardrail harga: makin besar qty, harga biasanya tidak naik
    if price1 is not None and price1 > float(base_price):
        raise ValueError('Harga grosir 1 tidak boleh lebih tinggi dari harga ecer.')
    if price1 is not None and price2 is not None and price2 > price1:
        raise ValueError('Harga grosir 2 tidak boleh lebih tinggi dari grosir 1.')

    return {
        'min_qty_grosir_1': min1,
        'harga_jual_grosir_1': price1,
        'min_qty_grosir_2': min2,
        'harga_jual_grosir_2': price2,
    }


def _save_product_image(file_storage, tenant_id):
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit('.', 1)[-1].lower() if '.' in file_storage.filename else ''
    if ext not in current_app.config['PRODUCT_IMAGE_ALLOWED']:
        raise ValueError('Format gambar tidak didukung (png, jpg, webp, gif).')
    sub = os.path.join(str(tenant_id))
    folder = os.path.join(current_app.static_folder, 'uploads', 'products', sub)
    os.makedirs(folder, exist_ok=True)
    fname = f'{uuid.uuid4().hex}.{ext}'
    path_abs = os.path.join(folder, fname)
    file_storage.save(path_abs)
    return f'uploads/products/{sub}/{fname}'


def _delete_image_file(relative_path):
    if not relative_path:
        return
    abs_path = os.path.join(current_app.static_folder, relative_path)
    if os.path.isfile(abs_path):
        try:
            os.remove(abs_path)
        except OSError:
            pass


def _tenant_stats(tenant_id):
    base = Product.query.filter_by(tenant_id=tenant_id)
    aktif = base.filter_by(aktif=True)
    count_aktif = aktif.count()
    count_menipis = aktif.filter(Product.stok > 0, Product.stok <= Product.stok_minimum).count()
    count_habis = aktif.filter(Product.stok <= 0).count()
    products = aktif.all()
    layer_map = dict(
        db.session.query(
            InventoryCostLayer.product_id,
            func.coalesce(func.sum(InventoryCostLayer.qty_remaining * InventoryCostLayer.unit_cost), 0.0),
        )
        .filter(
            InventoryCostLayer.tenant_id == tenant_id,
            InventoryCostLayer.qty_remaining > 0,
        )
        .group_by(InventoryCostLayer.product_id)
        .all()
    )
    nilai = 0.0
    for p in products:
        layer_val = float(layer_map.get(p.id, 0.0) or 0.0)
        if layer_val > 0:
            nilai += layer_val
        else:
            nilai += float(p.stok or 0) * float(p.harga_beli or 0)
    return {
        'count_aktif': count_aktif,
        'count_menipis': count_menipis,
        'count_habis': count_habis,
        'nilai_persediaan': float(nilai),
    }


def _log_product_audit(
    tenant_id,
    actor_user_id,
    product_id,
    action,
    old_harga_jual=None,
    new_harga_jual=None,
    old_stok_minimum=None,
    new_stok_minimum=None,
    detail=None,
):
    db.session.add(ProductAuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        product_id=product_id,
        action=action,
        old_harga_jual=old_harga_jual,
        new_harga_jual=new_harga_jual,
        old_stok_minimum=old_stok_minimum,
        new_stok_minimum=new_stok_minimum,
        detail=(detail[:2000] if detail else None),
    ))


def _filtered_query(tenant_id, default_status='all'):
    search = request.args.get('q', '').strip()
    cat_id = request.args.get('category', '')
    etalase_id = request.args.get('etalase', '')
    status = request.args.get('status', default_status)
    stock_filter = request.args.get('stock', 'all')
    sort = request.args.get('sort', 'nama')

    q = Product.query.filter_by(tenant_id=tenant_id)
    if search:
        like = f'%{search}%'
        q = q.filter(or_(Product.nama.ilike(like), Product.barcode.ilike(like)))
    if cat_id:
        q = q.filter_by(category_id=int(cat_id))
    if etalase_id == '0':
        q = q.filter(Product.etalase_id.is_(None))
    elif etalase_id:
        q = q.filter_by(etalase_id=int(etalase_id))
    if status == 'aktif':
        q = q.filter_by(aktif=True)
    elif status == 'nonaktif':
        q = q.filter_by(aktif=False)
    if stock_filter == 'habis':
        q = q.filter(Product.stok <= 0)
    elif stock_filter == 'menipis':
        q = q.filter(Product.stok > 0).filter(Product.stok <= Product.stok_minimum)

    if sort == 'harga_jual_desc':
        q = q.order_by(Product.harga_jual.desc(), Product.nama)
    elif sort == 'harga_jual_asc':
        q = q.order_by(Product.harga_jual.asc(), Product.nama)
    elif sort == 'stok_asc':
        q = q.order_by(Product.stok.asc(), Product.nama)
    elif sort == 'stok_desc':
        q = q.order_by(Product.stok.desc(), Product.nama)
    elif sort == 'etalase':
        q = q.outerjoin(Etalase, Product.etalase_id == Etalase.id).order_by(
            nullslast(Etalase.nama.asc()),
            Product.nama,
        )
    else:
        q = q.order_by(Product.nama)
    return q


@products_bp.route('/')
@login_required
def index():
    tenant_id = current_user.tenant_id
    page = request.args.get('page', 1, type=int)
    search = request.args.get('q', '')
    cat_id = request.args.get('category', '')
    etalase_id = request.args.get('etalase', '')
    status = request.args.get('status', 'all')
    stock_filter = request.args.get('stock', 'all')
    sort = request.args.get('sort', 'nama')
    focus = (request.args.get('focus') or '').strip().lower()

    q = _filtered_query(tenant_id)
    products = q.paginate(page=page, per_page=20, error_out=False)
    categories = ProductCategory.query.filter_by(tenant_id=tenant_id).order_by(ProductCategory.nama).all()
    etalases = _etalases_for_tenant(tenant_id)
    stats = _tenant_stats(tenant_id)

    return render_template(
        'products/index.html',
        products=products,
        categories=categories,
        etalases=etalases,
        search=search,
        cat_id=cat_id,
        etalase_id=etalase_id,
        status=status,
        stock_filter=stock_filter,
        sort=sort,
        focus=focus,
        stats=stats,
    )


@products_bp.route('/etalase')
@login_required
def etalase():
    from itertools import groupby

    tenant_id = current_user.tenant_id
    page = request.args.get('page', 1, type=int)
    search = request.args.get('q', '')
    cat_id = request.args.get('category', '')
    etalase_id = request.args.get('etalase', '')
    status = request.args.get('status', 'aktif')
    stock_filter = request.args.get('stock', 'all')
    sort = request.args.get('sort', 'nama')
    print_all = request.args.get('print_all', '0') == '1'
    grouped = sort == 'etalase'

    q = _filtered_query(tenant_id, default_status='aktif')

    if print_all:
        rows = q.all()
        pagination = None
        total_count = len(rows)
    else:
        pagination = q.paginate(page=page, per_page=24, error_out=False)
        rows = pagination.items
        total_count = pagination.total

    etalase_groups = None
    if grouped:
        etalase_groups = []
        if rows:
            for key, g in groupby(
                rows,
                key=lambda p: p.etalase.nama if p.etalase else None,
            ):
                title = key if key else 'Belum ditentukan'
                etalase_groups.append((title, list(g)))

    categories = ProductCategory.query.filter_by(tenant_id=tenant_id).order_by(ProductCategory.nama).all()
    etalases = _etalases_for_tenant(tenant_id)
    tenant_nama = current_user.tenant.nama if getattr(current_user, 'tenant', None) else ''

    tanggal_cetak = local_today_date(resolve_effective_timezone_id(current_user)).strftime('%d/%m/%Y')

    return render_template(
        'products/etalase.html',
        products=pagination,
        etalase_product_list=rows,
        etalase_groups=etalase_groups,
        etalase_total=total_count,
        print_all=print_all,
        grouped=grouped,
        categories=categories,
        etalases=etalases,
        search=search,
        cat_id=cat_id,
        etalase_id=etalase_id,
        status=status,
        stock_filter=stock_filter,
        sort=sort,
        tenant_nama=tenant_nama,
        tanggal_cetak=tanggal_cetak,
    )


@products_bp.route('/export.csv')
@login_required
def export_csv():
    tenant_id = current_user.tenant_id
    q = _filtered_query(tenant_id)
    rows = q.all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        'nama', 'barcode', 'kategori', 'etalase', 'satuan', 'harga_beli', 'harga_jual', 'harga_coret',
        'min_qty_grosir_1', 'harga_jual_grosir_1', 'min_qty_grosir_2', 'harga_jual_grosir_2',
        'stok', 'stok_minimum', 'supplier', 'aktif',
    ])
    for p in rows:
        w.writerow([
            p.nama,
            p.barcode or '',
            p.category.nama if p.category else '',
            p.etalase.nama if p.etalase else '',
            p.satuan,
            p.harga_beli,
            p.harga_jual,
            p.harga_coret or '',
            p.min_qty_grosir_1 or '',
            p.harga_jual_grosir_1 or '',
            p.min_qty_grosir_2 or '',
            p.harga_jual_grosir_2 or '',
            p.stok,
            p.stok_minimum,
            p.supplier.nama if p.supplier else '',
            'ya' if p.aktif else 'tidak',
        ])

    out = io.BytesIO()
    out.write(buf.getvalue().encode('utf-8-sig'))
    out.seek(0)
    return Response(
        out.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=produk_export.csv'},
    )


@products_bp.route('/import/sample.csv')
@login_required
def import_sample():
    if not require_admin():
        return redirect(url_for('products.index'))
    sample = (
        'nama,barcode,kategori,satuan,harga_beli,harga_jual,harga_coret,min_qty_grosir_1,harga_jual_grosir_1,min_qty_grosir_2,harga_jual_grosir_2,stok_awal,stok_minimum,supplier\n'
        'Beras Premium 5kg,899111,BERAS,kg,65000,72000,78000,5,70000,10,68000,10,2,CV Sumber Padi\n'
        'Gula Pasir 1kg,,GULA,kg,12000,13500,,,,,,20,5,\n'
    )
    return Response(
        sample.encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=contoh_import_produk.csv'},
    )


@products_bp.route('/import', methods=['GET', 'POST'])
@login_required
def import_products():
    if not require_admin():
        return redirect(url_for('products.index'))
    tenant_id = current_user.tenant_id
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('Pilih file CSV.', 'warning')
            return redirect(url_for('products.import_products'))
        try:
            raw = f.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            flash('File harus UTF-8.', 'danger')
            return redirect(url_for('products.import_products'))

        reader = csv.DictReader(io.StringIO(raw))
        fnames = {h.strip().lower() for h in (reader.fieldnames or []) if h and h.strip()}
        if not fnames or 'nama' not in fnames or 'harga_jual' not in fnames:
            flash('CSV wajib punya kolom: nama, harga_jual (dan opsional lainnya).', 'danger')
            return redirect(url_for('products.import_products'))

        def norm_row(row):
            return {(k or '').strip().lower(): (v or '').strip() for k, v in row.items() if k is not None}

        ok, err = 0, 0
        seen_bc = set()
        try:
            for row in reader:
                row = norm_row(row)
                nama = row.get('nama', '')
                if not nama:
                    err += 1
                    continue
                try:
                    hj = float(row.get('harga_jual', '0').replace(',', '.') or 0)
                except ValueError:
                    err += 1
                    continue
                tier_form_like = {
                    'min_qty_grosir_1': row.get('min_qty_grosir_1'),
                    'harga_jual_grosir_1': row.get('harga_jual_grosir_1'),
                    'min_qty_grosir_2': row.get('min_qty_grosir_2'),
                    'harga_jual_grosir_2': row.get('harga_jual_grosir_2'),
                }
                try:
                    tiers = _extract_price_tiers(tier_form_like, hj)
                except ValueError:
                    err += 1
                    continue
                bc = _norm_barcode(row.get('barcode'))
                if bc:
                    if bc in seen_bc or barcode_taken(tenant_id, bc):
                        err += 1
                        continue
                    seen_bc.add(bc)

                cat_name = row.get('kategori', '')
                category_id = None
                if cat_name:
                    cat = ProductCategory.query.filter_by(tenant_id=tenant_id, nama=cat_name).first()
                    if not cat:
                        cat = ProductCategory(tenant_id=tenant_id, nama=cat_name)
                        db.session.add(cat)
                        db.session.flush()
                    category_id = cat.id

                sup_name = row.get('supplier', '')
                supplier_id = None
                if sup_name:
                    sup = Supplier.query.filter_by(tenant_id=tenant_id, nama=sup_name).first()
                    if sup:
                        supplier_id = sup.id

                satuan = row.get('satuan', '') or 'pcs'
                try:
                    hb = float((row.get('harga_beli', '') or '0').replace(',', '.') or 0)
                except ValueError:
                    hb = 0
                try:
                    stok = float((row.get('stok_awal') or row.get('stok') or '0').replace(',', '.') or 0)
                except ValueError:
                    stok = 0
                try:
                    stok_min = float((row.get('stok_minimum', '') or '5').replace(',', '.') or 5)
                except ValueError:
                    stok_min = 5
                hc_raw = (row.get('harga_coret') or '').strip()
                harga_coret_imp = None
                if hc_raw:
                    try:
                        hcv = float(hc_raw.replace(',', '.'))
                        harga_coret_imp = hcv if hcv > 0 else None
                    except ValueError:
                        harga_coret_imp = None

                product = Product(
                    tenant_id=tenant_id,
                    category_id=category_id,
                    supplier_id=supplier_id,
                    nama=nama,
                    barcode=bc,
                    satuan=satuan,
                    harga_beli=hb,
                    harga_jual=hj,
                    harga_coret=harga_coret_imp,
                    min_qty_grosir_1=tiers['min_qty_grosir_1'],
                    harga_jual_grosir_1=tiers['harga_jual_grosir_1'],
                    min_qty_grosir_2=tiers['min_qty_grosir_2'],
                    harga_jual_grosir_2=tiers['harga_jual_grosir_2'],
                    stok=stok,
                    stok_minimum=stok_min,
                )
                db.session.add(product)
                if stok > 0:
                    db.session.flush()
                    db.session.add(StockMovement(
                        product_id=product.id,
                        user_id=current_user.id,
                        tipe='masuk',
                        qty=stok,
                        stok_sebelum=0,
                        stok_sesudah=stok,
                        keterangan='Import CSV',
                    ))
                    create_cost_layer(
                        tenant_id=tenant_id,
                        product_id=product.id,
                        qty_in=stok,
                        unit_cost=float(product.harga_beli or 0),
                        source_type='import_csv',
                        source_id=product.id,
                    )
                ok += 1

            db.session.commit()
        except Exception:
            db.session.rollback()
            flash('Import gagal (database error). Periksa format CSV dan coba lagi.', 'danger')
            return redirect(url_for('products.import_products'))

        flash(f'Import selesai: {ok} produk ditambah, {err} baris dilewati.', 'success' if ok else 'warning')
        return redirect(url_for('products.index'))

    return render_template('products/import.html')


@products_bp.route('/generate-barcode')
@login_required
def generate_barcode():
    if current_user.role not in ('superadmin', 'admin'):
        return jsonify({'error': 'forbidden'}), 403
    tenant_id = current_user.tenant_id
    exclude_id = request.args.get('exclude_id', type=int)
    code = _generate_unique_barcode(tenant_id, exclude_id=exclude_id)
    if not code:
        return jsonify({'error': 'gagal_membuat_kode'}), 500
    return jsonify({'barcode': code})


@products_bp.route('/add', methods=['GET', 'POST'])
@login_required
def add():
    if not require_admin():
        return redirect(url_for('products.index'))
    tenant_id = current_user.tenant_id
    categories = ProductCategory.query.filter_by(tenant_id=tenant_id).order_by(ProductCategory.nama).all()
    etalases = _etalases_for_tenant(tenant_id)
    suppliers = Supplier.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Supplier.nama).all()

    if request.method == 'POST':
        bc = _norm_barcode(request.form.get('barcode'))
        if barcode_taken(tenant_id, bc):
            flash('Barcode sudah dipakai produk lain.', 'danger')
            return render_template('products/form.html', product=None, categories=categories, etalases=etalases, suppliers=suppliers, action='Tambah')

        gambar = None
        try:
            if request.files.get('gambar'):
                gambar = _save_product_image(request.files.get('gambar'), tenant_id)
        except ValueError as e:
            flash(str(e), 'danger')
            return render_template('products/form.html', product=None, categories=categories, etalases=etalases, suppliers=suppliers, action='Tambah')

        sid = request.form.get('supplier_id')
        supplier_id = int(sid) if sid else None
        if supplier_id:
            if not Supplier.query.filter_by(id=supplier_id, tenant_id=tenant_id).first():
                supplier_id = None
        try:
            harga_jual = float(request.form['harga_jual'])
            tiers = _extract_price_tiers(request.form, harga_jual)
        except TypeError:
            flash('Format harga tidak valid.', 'danger')
            return render_template('products/form.html', product=None, categories=categories, etalases=etalases, suppliers=suppliers, action='Tambah')
        except ValueError as e:
            flash(str(e) or 'Format harga tidak valid.', 'danger')
            return render_template('products/form.html', product=None, categories=categories, etalases=etalases, suppliers=suppliers, action='Tambah')

        product = Product(
            tenant_id=tenant_id,
            category_id=request.form.get('category_id') or None,
            etalase_id=_etalase_id_from_post(tenant_id),
            supplier_id=supplier_id,
            nama=request.form['nama'],
            barcode=bc,
            satuan=request.form.get('satuan', 'pcs'),
            harga_beli=float(request.form.get('harga_beli', 0)),
            harga_jual=harga_jual,
            harga_coret=_harga_coret_from_form(),
            min_qty_grosir_1=tiers['min_qty_grosir_1'],
            harga_jual_grosir_1=tiers['harga_jual_grosir_1'],
            min_qty_grosir_2=tiers['min_qty_grosir_2'],
            harga_jual_grosir_2=tiers['harga_jual_grosir_2'],
            stok=float(request.form.get('stok', 0)),
            stok_minimum=float(request.form.get('stok_minimum', 5)),
            gambar=gambar,
        )
        if product.harga_jual < product.harga_beli:
            flash(
                f'Peringatan: harga jual "{product.nama}" lebih rendah dari harga beli.',
                'warning',
            )
        db.session.add(product)

        if product.stok > 0:
            db.session.flush()
            movement = StockMovement(
                product_id=product.id,
                user_id=current_user.id,
                tipe='masuk',
                qty=product.stok,
                stok_sebelum=0,
                stok_sesudah=product.stok,
                keterangan='Stok awal',
            )
            db.session.add(movement)
            create_cost_layer(
                tenant_id=tenant_id,
                product_id=product.id,
                qty_in=float(product.stok or 0),
                unit_cost=float(product.harga_beli or 0),
                source_type='opening_stock',
                source_id=product.id,
            )

        db.session.flush()
        _log_product_audit(
            tenant_id=tenant_id,
            actor_user_id=current_user.id,
            product_id=product.id,
            action='product_created',
            old_harga_jual=None,
            new_harga_jual=float(product.harga_jual or 0),
            old_stok_minimum=None,
            new_stok_minimum=float(product.stok_minimum or 0),
            detail='Produk dibuat.',
        )

        db.session.commit()
        flash(f'Produk "{product.nama}" berhasil ditambahkan!', 'success')
        return redirect(url_for('products.index', focus='search', q=(product.barcode or product.nama or '').strip()))

    return render_template('products/form.html', product=None, categories=categories, etalases=etalases, suppliers=suppliers, action='Tambah')


@products_bp.route('/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit(id):
    if not require_admin():
        return redirect(url_for('products.index'))
    tenant_id = current_user.tenant_id
    product = Product.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    categories = ProductCategory.query.filter_by(tenant_id=tenant_id).order_by(ProductCategory.nama).all()
    etalases = _etalases_for_tenant(tenant_id)
    suppliers = Supplier.query.filter(
        Supplier.tenant_id == tenant_id,
        or_(Supplier.aktif == True, Supplier.id == product.supplier_id),
    ).order_by(Supplier.nama).all()

    if request.method == 'POST':
        old_harga_jual = float(product.harga_jual or 0)
        old_stok_minimum = float(product.stok_minimum or 0)

        bc = _norm_barcode(request.form.get('barcode'))
        if barcode_taken(tenant_id, bc, exclude_id=product.id):
            flash('Barcode sudah dipakai produk lain.', 'danger')
            return render_template('products/form.html', product=product, categories=categories, etalases=etalases, suppliers=suppliers, action='Edit')

        if request.files.get('gambar') and request.files.get('gambar').filename:
            try:
                new_g = _save_product_image(request.files.get('gambar'), tenant_id)
                _delete_image_file(product.gambar)
                product.gambar = new_g
            except ValueError as e:
                flash(str(e), 'danger')
                return render_template('products/form.html', product=product, categories=categories, etalases=etalases, suppliers=suppliers, action='Edit')

        if request.form.get('hapus_gambar'):
            _delete_image_file(product.gambar)
            product.gambar = None

        sid = request.form.get('supplier_id')
        supplier_id = int(sid) if sid else None
        if supplier_id and not Supplier.query.filter_by(id=supplier_id, tenant_id=tenant_id).first():
            supplier_id = None
        product.supplier_id = supplier_id
        try:
            harga_jual = float(request.form['harga_jual'])
            tiers = _extract_price_tiers(request.form, harga_jual)
        except TypeError:
            flash('Format harga tidak valid.', 'danger')
            return render_template('products/form.html', product=product, categories=categories, etalases=etalases, suppliers=suppliers, action='Edit')
        except ValueError as e:
            flash(str(e) or 'Format harga tidak valid.', 'danger')
            return render_template('products/form.html', product=product, categories=categories, etalases=etalases, suppliers=suppliers, action='Edit')

        product.category_id = request.form.get('category_id') or None
        product.etalase_id = _etalase_id_from_post(tenant_id)
        product.nama = request.form['nama']
        product.barcode = bc
        product.satuan = request.form.get('satuan', 'pcs')
        product.harga_beli = float(request.form.get('harga_beli', 0))
        product.harga_jual = harga_jual
        product.harga_coret = _harga_coret_from_form()
        product.min_qty_grosir_1 = tiers['min_qty_grosir_1']
        product.harga_jual_grosir_1 = tiers['harga_jual_grosir_1']
        product.min_qty_grosir_2 = tiers['min_qty_grosir_2']
        product.harga_jual_grosir_2 = tiers['harga_jual_grosir_2']
        product.stok_minimum = float(request.form.get('stok_minimum', 5))
        product.aktif = 'aktif' in request.form

        if product.harga_jual < product.harga_beli:
            flash(
                f'Peringatan: harga jual "{product.nama}" lebih rendah dari harga beli.',
                'warning',
            )

        changed_harga = float(product.harga_jual or 0) != old_harga_jual
        changed_stok_minimum = float(product.stok_minimum or 0) != old_stok_minimum
        if changed_harga or changed_stok_minimum:
            _log_product_audit(
                tenant_id=tenant_id,
                actor_user_id=current_user.id,
                product_id=product.id,
                action='product_pricing_or_minstock_updated',
                old_harga_jual=old_harga_jual,
                new_harga_jual=float(product.harga_jual or 0),
                old_stok_minimum=old_stok_minimum,
                new_stok_minimum=float(product.stok_minimum or 0),
                detail='Update harga jual / stok minimum dari form produk.',
            )

        db.session.commit()
        flash(f'Produk "{product.nama}" berhasil diupdate!', 'success')
        return redirect(url_for('products.index', focus='search', q=(product.barcode or product.nama or '').strip()))

    return render_template('products/form.html', product=product, categories=categories, etalases=etalases, suppliers=suppliers, action='Edit')


@products_bp.route('/duplicate/<int:id>', methods=['POST'])
@login_required
def duplicate(id):
    if not require_admin():
        return redirect(url_for('products.index'))
    tenant_id = current_user.tenant_id
    src = Product.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    copy = Product(
        tenant_id=tenant_id,
        category_id=src.category_id,
        etalase_id=src.etalase_id,
        supplier_id=src.supplier_id,
        nama=f'{src.nama} (Salinan)',
        barcode=None,
        satuan=src.satuan,
        harga_beli=src.harga_beli,
        harga_jual=src.harga_jual,
        harga_coret=src.harga_coret,
        min_qty_grosir_1=src.min_qty_grosir_1,
        harga_jual_grosir_1=src.harga_jual_grosir_1,
        min_qty_grosir_2=src.min_qty_grosir_2,
        harga_jual_grosir_2=src.harga_jual_grosir_2,
        stok=0,
        stok_minimum=src.stok_minimum,
        gambar=None,
        aktif=True,
    )
    db.session.add(copy)
    db.session.commit()
    flash(f'Duplikat dibuat. Sesuaikan nama/barcode lalu simpan.', 'success')
    return redirect(url_for('products.edit', id=copy.id))


@products_bp.route('/delete/<int:id>', methods=['POST'])
@login_required
def delete(id):
    if not require_admin():
        return redirect(url_for('products.index'))
    tenant_id = current_user.tenant_id
    product = Product.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    product.aktif = False
    db.session.commit()
    flash(f'Produk "{product.nama}" dinonaktifkan.', 'warning')
    return redirect(url_for('products.index'))


@products_bp.route('/stock-in/<int:id>', methods=['GET', 'POST'])
@login_required
def stock_in(id):
    """Dinonaktifkan: stok masuk hanya lewat pembelian; opname terpisah."""
    flash(
        'Stok masuk manual dinonaktifkan. Tambah stok melalui menu Pembelian. '
        'Penyesuaian stok opname akan tersedia sebagai fitur terpisah.',
        'info',
    )
    return redirect(url_for('products.index'))


@products_bp.route('/stock-adjust/<int:id>', methods=['GET', 'POST'])
@login_required
def stock_adjust(id):
    """Dinonaktifkan: koreksi manual diganti alur pembelian + opname."""
    flash(
        'Koreksi stok manual dinonaktifkan. Untuk selisih fisik nanti gunakan fitur stok opname. '
        'Stok bertambah melalui penerimaan pembelian.',
        'info',
    )
    return redirect(url_for('products.index'))


@products_bp.route('/history/<int:id>')
@login_required
def stock_history(id):
    tenant_id = current_user.tenant_id
    product = Product.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    page = request.args.get('page', 1, type=int)
    q = StockMovement.query.filter_by(product_id=product.id).order_by(StockMovement.created_at.desc())
    movements = q.paginate(page=page, per_page=30, error_out=False)
    return render_template('products/stock_history.html', product=product, movements=movements)


@products_bp.route('/history-price/<int:id>')
@login_required
def price_history(id):
    tenant_id = current_user.tenant_id
    product = Product.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    page = request.args.get('page', 1, type=int)
    q = ProductAuditLog.query.filter_by(tenant_id=tenant_id, product_id=product.id).order_by(ProductAuditLog.created_at.desc())
    logs = q.paginate(page=page, per_page=30, error_out=False)
    return render_template('products/price_history.html', product=product, logs=logs)


@products_bp.route('/etalases')
@login_required
def etalases():
    tenant_id = current_user.tenant_id
    rows = Etalase.query.filter_by(tenant_id=tenant_id).order_by(Etalase.nama).all()
    return render_template('products/etalases.html', etalases=rows)


@products_bp.route('/etalases/add', methods=['POST'])
@login_required
def add_etalase():
    if not require_admin():
        return redirect(url_for('products.etalases'))
    tenant_id = current_user.tenant_id
    nama = (request.form.get('nama') or '').strip()
    keterangan = (request.form.get('keterangan') or '').strip() or None
    if nama:
        db.session.add(Etalase(tenant_id=tenant_id, nama=nama, keterangan=keterangan))
        db.session.commit()
        flash(f'Etalase "{nama}" berhasil ditambahkan.', 'success')
    else:
        flash('Nama etalase wajib diisi.', 'warning')
    return redirect(url_for('products.etalases'))


@products_bp.route('/etalases/edit/<int:id>', methods=['POST'])
@login_required
def edit_etalase(id):
    if not require_admin():
        return redirect(url_for('products.etalases'))
    tenant_id = current_user.tenant_id
    row = Etalase.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    nama = (request.form.get('nama') or '').strip()
    keterangan = (request.form.get('keterangan') or '').strip() or None
    if not nama:
        flash('Nama etalase tidak boleh kosong.', 'danger')
        return redirect(url_for('products.etalases'))
    row.nama = nama
    row.keterangan = keterangan
    db.session.commit()
    flash('Etalase diperbarui.', 'success')
    return redirect(url_for('products.etalases'))


@products_bp.route('/etalases/delete/<int:id>', methods=['POST'])
@login_required
def delete_etalase(id):
    if not require_admin():
        return redirect(url_for('products.etalases'))
    tenant_id = current_user.tenant_id
    row = Etalase.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    Product.query.filter_by(tenant_id=tenant_id, etalase_id=row.id).update({'etalase_id': None}, synchronize_session=False)
    db.session.delete(row)
    db.session.commit()
    flash('Etalase dihapus. Produk terkait tidak lagi memiliki lokasi etalase.', 'warning')
    return redirect(url_for('products.etalases'))


@products_bp.route('/categories')
@login_required
def categories():
    tenant_id = current_user.tenant_id
    cats = ProductCategory.query.filter_by(tenant_id=tenant_id).all()
    return render_template('products/categories.html', categories=cats)


@products_bp.route('/categories/add', methods=['POST'])
@login_required
def add_category():
    if not require_admin():
        return redirect(url_for('products.categories'))
    tenant_id = current_user.tenant_id
    nama = request.form.get('nama', '').strip()
    if nama:
        cat = ProductCategory(tenant_id=tenant_id, nama=nama)
        db.session.add(cat)
        db.session.commit()
        flash(f'Kategori "{nama}" berhasil ditambahkan!', 'success')
    return redirect(url_for('products.categories'))


@products_bp.route('/categories/delete/<int:id>', methods=['POST'])
@login_required
def delete_category(id):
    if not require_admin():
        return redirect(url_for('products.categories'))
    tenant_id = current_user.tenant_id
    cat = ProductCategory.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    db.session.delete(cat)
    db.session.commit()
    flash('Kategori dihapus.', 'warning')
    return redirect(url_for('products.categories'))
