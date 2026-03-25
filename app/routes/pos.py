from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, session
from flask_login import login_required, current_user
from datetime import datetime
from sqlalchemy import or_
from .. import db
from ..models import (
    Product,
    Transaction,
    TransactionItem,
    StockMovement,
    Branch,
    ProductCategory,
    Member,
    Debt,
)
from ..shifts_util import get_open_shift

pos_bp = Blueprint('pos', __name__, url_prefix='/pos')


def generate_nomor_transaksi(tenant_id, branch_id):
    today = datetime.utcnow()
    prefix = f"TRX-{today.strftime('%Y%m%d')}-{branch_id:04d}"
    last = Transaction.query.filter(
        Transaction.nomor.like(f"{prefix}%")
    ).order_by(Transaction.id.desc()).first()
    if last:
        last_num = int(last.nomor.split('-')[-1]) + 1
    else:
        last_num = 1
    return f"{prefix}-{last_num:04d}"


def _branch_for_user(tenant_id):
    if current_user.branch_id:
        return Branch.query.get(current_user.branch_id)
    return Branch.query.filter_by(tenant_id=tenant_id, aktif=True).first()


def _price_for_qty(product, qty):
    picked = product.price_for_qty(qty)
    return float(picked.get('harga', product.harga_jual or 0)), picked.get('label', 'ecer')


@pos_bp.route('/')
@login_required
def index():
    tenant_id = current_user.tenant_id
    products = Product.query.filter_by(
        tenant_id=tenant_id, aktif=True
    ).filter(Product.stok > 0).order_by(Product.nama).all()

    branch = _branch_for_user(tenant_id)

    categories = (
        db.session.query(ProductCategory)
        .join(Product, Product.category_id == ProductCategory.id)
        .filter(
            Product.tenant_id == tenant_id,
            Product.aktif == True,
            Product.stok > 0,
        )
        .distinct()
        .order_by(ProductCategory.nama)
        .all()
    )

    members = Member.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Member.nama).all()

    can_override_price = current_user.role in ('superadmin', 'admin')

    return render_template(
        'pos.html',
        products=products,
        branch=branch,
        categories=categories,
        members=members,
        can_override_price=can_override_price,
    )


@pos_bp.route('/search-products')
@login_required
def search_products():
    q = (request.args.get('q') or '').strip()
    tenant_id = current_user.tenant_id
    if not q:
        return jsonify([])

    like = f'%{q}%'
    products = Product.query.filter(
        Product.tenant_id == tenant_id,
        Product.aktif == True,
        Product.stok > 0,
        or_(Product.nama.ilike(like), Product.barcode.ilike(like)),
    ).order_by(Product.nama).limit(40).all()

    return jsonify([{
        'id': p.id,
        'nama': p.nama,
        'harga': p.harga_jual,
        'harga_ecer': p.harga_jual,
        'min_qty_grosir_1': p.min_qty_grosir_1,
        'harga_jual_grosir_1': p.harga_jual_grosir_1,
        'min_qty_grosir_2': p.min_qty_grosir_2,
        'harga_jual_grosir_2': p.harga_jual_grosir_2,
        'stok': p.stok,
        'satuan': p.satuan,
        'barcode': p.barcode or '',
        'gambar': p.gambar or '',
        'category_id': p.category_id or 0,
        'stok_minimum': p.stok_minimum,
    } for p in products])


@pos_bp.route('/members')
@login_required
def get_members():
    tenant_id = current_user.tenant_id
    members = Member.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Member.nama).all()
    return jsonify([{
        'id': m.id,
        'nama': m.nama,
        'telepon': m.telepon,
        'poin': m.poin,
        'diskon_persen': m.diskon_persen or 0,
    } for m in members])


@pos_bp.route('/recent-transactions')
@login_required
def recent_transactions():
    tenant_id = current_user.tenant_id
    branch_id = current_user.branch_id
    if not branch_id:
        b = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).first()
        branch_id = b.id if b else None
    if not branch_id:
        return jsonify([])

    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        Transaction.query.filter(
            Transaction.tenant_id == tenant_id,
            Transaction.branch_id == branch_id,
            Transaction.status == 'selesai',
            Transaction.created_at >= start,
        )
        .order_by(Transaction.created_at.desc())
        .limit(12)
        .all()
    )
    return jsonify([{
        'id': t.id,
        'nomor': t.nomor,
        'total': t.total,
        'created_at': t.created_at.strftime('%H:%M'),
    } for t in rows])


@pos_bp.route('/transaction/<int:tid>/items')
@login_required
def transaction_items(tid):
    tenant_id = current_user.tenant_id
    trx = Transaction.query.filter_by(id=tid, tenant_id=tenant_id).first_or_404()
    out = []
    for ti in trx.items:
        p = Product.query.filter_by(id=ti.product_id, tenant_id=tenant_id, aktif=True).first()
        if not p or p.stok <= 0:
            continue
        out.append({
            'id': p.id,
            'nama': p.nama,
            'harga': p.harga_jual,
            'harga_ecer': p.harga_jual,
            'min_qty_grosir_1': p.min_qty_grosir_1,
            'harga_jual_grosir_1': p.harga_jual_grosir_1,
            'min_qty_grosir_2': p.min_qty_grosir_2,
            'harga_jual_grosir_2': p.harga_jual_grosir_2,
            'stok': p.stok,
            'satuan': p.satuan,
            'qty': ti.qty,
        })
    return jsonify(out)


@pos_bp.route('/checkout', methods=['POST'])
@login_required
def checkout():
    data = request.get_json()
    items = data.get('items', [])
    bayar = float(data.get('bayar', 0))
    metode = data.get('metode_bayar', 'tunai')
    diskon_manual = max(0, float(data.get('diskon', 0)))
    catatan = data.get('catatan', '')
    member_id = data.get('member_id')

    if not items:
        return jsonify({'success': False, 'message': 'Keranjang kosong!'}), 400

    tenant_id = current_user.tenant_id
    branch_id = current_user.branch_id
    if not branch_id:
        branch = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).first()
        branch_id = branch.id if branch else None

    if not branch_id:
        return jsonify({'success': False, 'message': 'Cabang tidak ditemukan!'}), 400

    shift = get_open_shift(tenant_id, branch_id, current_user.id)
    if not shift:
        return jsonify({
            'success': False,
            'message': 'Shift kasir belum dibuka. Buka shift terlebih dahulu di halaman POS.',
        }), 400

    allow_price_override = current_user.role in ('superadmin', 'admin')

    # Bangun baris dengan harga efektif dari DB (override hanya admin)
    resolved = []
    subtotal = 0.0
    for raw in items:
        try:
            pid = int(raw['id'])
            qty = float(raw['qty'])
        except (KeyError, TypeError, ValueError):
            return jsonify({'success': False, 'message': 'Data item tidak valid.'}), 400
        if qty <= 0:
            return jsonify({'success': False, 'message': 'Qty harus lebih dari 0.'}), 400

        product = Product.query.filter_by(id=pid, tenant_id=tenant_id).first()
        if not product or not product.aktif:
            return jsonify({'success': False, 'message': 'Produk tidak ditemukan atau nonaktif.'}), 400

        price_mode = str(raw.get('price_mode') or 'auto').strip().lower()
        if price_mode not in ('auto', 'ecer', 'manual'):
            price_mode = 'auto'

        if price_mode == 'ecer':
            harga = float(product.harga_jual or 0)
        else:
            harga, _ = _price_for_qty(product, qty)

        if price_mode == 'manual':
            if not allow_price_override:
                return jsonify({
                    'success': False,
                    'message': f'Mode harga manual untuk "{product.nama}" hanya untuk admin.',
                }), 400
        if allow_price_override and price_mode == 'manual' and raw.get('harga') is not None:
            try:
                h = float(raw['harga'])
            except (TypeError, ValueError):
                h = harga
            floor_p = max(0.0, float(product.harga_beli or 0))
            if h < floor_p:
                return jsonify({
                    'success': False,
                    'message': f'Harga jual "{product.nama}" tidak boleh di bawah harga beli.',
                }), 400
            if h > float(product.harga_jual) * 10:
                return jsonify({
                    'success': False,
                    'message': f'Harga "{product.nama}" terlalu tinggi, periksa input.',
                }), 400
            harga = h

        line_sub = harga * qty
        subtotal += line_sub
        resolved.append({
            'product': product,
            'qty': qty,
            'harga': harga,
            'line_sub': line_sub,
            'price_mode': price_mode,
        })

    member = None
    if member_id not in (None, '', 0, '0'):
        try:
            mid = int(member_id)
            if mid:
                member = Member.query.filter_by(id=mid, tenant_id=tenant_id).first()
        except (TypeError, ValueError):
            pass

    diskon_member = 0.0
    if member and (member.diskon_persen or 0) > 0:
        diskon_member = subtotal * (float(member.diskon_persen) / 100.0)

    total_diskon = diskon_manual + diskon_member
    total = subtotal - total_diskon
    if total < 0:
        total = 0.0

    kembalian = bayar - total

    if metode == 'kredit':
        if not member:
            return jsonify({'success': False, 'message': 'Transaksi kredit wajib memilih Member!'}), 400
        bayar = 0
        kembalian = 0
    elif bayar < total:
        return jsonify({'success': False, 'message': f'Uang bayar kurang! Total: {total:.0f}'}), 400

    # Cek stok terkini (satu transaksi DB)
    for row in resolved:
        p = row['product']
        if float(p.stok) < row['qty']:
            return jsonify({
                'success': False,
                'message': f"Stok {p.nama} tidak cukup! (sisa: {p.stok} {p.satuan})",
            }), 400

    nomor = generate_nomor_transaksi(tenant_id, branch_id)
    trx = Transaction(
        tenant_id=tenant_id,
        branch_id=branch_id,
        user_id=current_user.id,
        member_id=member.id if member else None,
        nomor=nomor,
        subtotal=subtotal,
        diskon=total_diskon,
        total=total,
        bayar=bayar,
        kembalian=max(0, kembalian),
        metode_bayar=metode,
        catatan=catatan,
        status='selesai',
        shift_id=shift.id,
    )
    db.session.add(trx)
    db.session.flush()

    stock_updates = []

    for row in resolved:
        product = row['product']
        qty = row['qty']
        harga = row['harga']

        db.session.refresh(product)
        if float(product.stok) < qty:
            db.session.rollback()
            return jsonify({
                'success': False,
                'message': f"Stok {product.nama} berubah, silakan ulangi. (sisa: {product.stok})",
            }), 400

        stok_sebelum = float(product.stok)

        trx_item = TransactionItem(
            transaction_id=trx.id,
            product_id=product.id,
            nama_produk=product.nama,
            harga=harga,
            qty=qty,
            subtotal=harga * qty,
        )
        db.session.add(trx_item)

        product.stok -= qty
        movement = StockMovement(
            product_id=product.id,
            user_id=current_user.id,
            tipe='keluar',
            qty=qty,
            stok_sebelum=stok_sebelum,
            stok_sesudah=product.stok,
            keterangan=f'Penjualan #{nomor}',
        )
        db.session.add(movement)
        stock_updates.append({'id': product.id, 'stok': product.stok})

    if metode == 'kredit' and member:
        jt_raw = (data.get('debt_jatuh_tempo') or data.get('jatuh_tempo') or '').strip()
        jt_parsed = None
        if jt_raw and len(jt_raw) >= 10:
            try:
                jt_parsed = datetime.strptime(jt_raw[:10], '%Y-%m-%d')
            except ValueError:
                jt_parsed = None
        debt = Debt(
            tenant_id=tenant_id,
            member_id=member.id,
            transaction_id=trx.id,
            jumlah=total,
            sisa=total,
            keterangan=f'Pembelian #{nomor}',
            jatuh_tempo=jt_parsed,
        )
        db.session.add(debt)
        member.total_hutang += total

    poin_didapat = 0
    if member:
        poin_didapat = int(total // 10000)
        member.poin += poin_didapat
        member.total_belanja += total

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Gagal menyimpan transaksi. Coba lagi.'}), 500

    return jsonify({
        'success': True,
        'message': 'Transaksi berhasil!',
        'nomor': nomor,
        'transaction_id': trx.id,
        'total': total,
        'bayar': bayar,
        'kembalian': max(0, kembalian),
        'poin': poin_didapat,
        'stock_updates': stock_updates,
        'subtotal': subtotal,
        'diskon_member': diskon_member,
        'diskon_manual': diskon_manual,
    })
