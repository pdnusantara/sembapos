from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, session, current_app
from flask_login import login_required, current_user
from datetime import datetime
from sqlalchemy import or_
from .. import db
from ..models import (
    Product,
    Etalase,
    Transaction,
    TransactionItem,
    TransactionPayment,
    StockMovement,
    Branch,
    ProductCategory,
    Member,
    Debt,
    VoucherRedemption,
)
from ..shifts_util import get_open_shift
from ..loyalty_service import ensure_default_tiers, evaluate_member_tier, member_tier_discount_pct
from ..promo_service import validate_voucher, promo_payload_json
from ..fifo_costing import consume_fifo_cost
from ..doc_numbers import generate_nomor_transaksi
from ..timezones import (
    format_utc_naive_as_local,
    local_today_date,
    resolve_effective_timezone_id,
    utc_naive_bounds_for_local_date,
)

pos_bp = Blueprint('pos', __name__, url_prefix='/pos')


def _branch_for_user(tenant_id):
    if current_user.branch_id:
        return Branch.query.get(current_user.branch_id)
    return Branch.query.filter_by(tenant_id=tenant_id, aktif=True).first()


def _price_for_qty(product, qty):
    picked = product.price_for_qty(qty)
    return float(picked.get('harga', product.harga_jual or 0)), picked.get('label', 'ecer')


def _discount_decision_payload(*, diskon_manual=0.0, diskon_member=0.0, promo_discount=0.0, voucher_code=None):
    manual = float(diskon_manual or 0)
    member = float(diskon_member or 0)
    voucher = float(promo_discount or 0)
    winner = 'none'
    reason = 'Tidak ada promo aktif.'
    if voucher > member and voucher > 0:
        winner = 'voucher'
        reason = 'Voucher dipilih karena potongan lebih besar dari diskon member/tier.'
    elif member > 0:
        winner = 'member_tier'
        reason = 'Diskon member/tier dipilih karena lebih besar atau sama dengan voucher.'
    elif manual > 0:
        winner = 'manual_only'
        reason = 'Tidak ada promo member/voucher. Hanya diskon manual yang diterapkan.'
    return {
        'winner': winner,
        'reason': reason,
        'candidates': {
            'voucher_discount': voucher,
            'member_discount': member,
            'manual_discount': manual,
            'voucher_code': (voucher_code or '').strip().upper() or None,
        },
    }


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

    etalases = (
        db.session.query(Etalase)
        .join(Product, Product.etalase_id == Etalase.id)
        .filter(
            Product.tenant_id == tenant_id,
            Product.aktif == True,
            Product.stok > 0,
        )
        .distinct()
        .order_by(Etalase.nama)
        .all()
    )

    members = Member.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Member.nama).all()
    ensure_default_tiers(tenant_id)

    can_override_price = current_user.role in ('superadmin', 'admin')

    return render_template(
        'pos.html',
        products=products,
        branch=branch,
        categories=categories,
        etalases=etalases,
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
        'etalase_id': p.etalase_id or 0,
        'etalase_nama': p.etalase.nama if p.etalase else '',
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

    tz_id = resolve_effective_timezone_id(current_user)
    local_d = local_today_date(tz_id)
    day_start, day_end = utc_naive_bounds_for_local_date(local_d, tz_id)
    rows = (
        Transaction.query.filter(
            Transaction.tenant_id == tenant_id,
            Transaction.branch_id == branch_id,
            Transaction.created_at.between(day_start, day_end),
        )
        .order_by(Transaction.created_at.desc())
        .limit(12)
        .all()
    )
    status_reason = {
        'draft': 'Transaksi masih draft, belum bisa diproses retur/tukar.',
        'pending': 'Transaksi belum selesai, selesaikan dulu untuk bisa retur/tukar.',
        'batal': 'Transaksi dibatalkan sehingga tidak bisa diproses retur/tukar.',
    }
    return jsonify([{
        'id': t.id,
        'nomor': t.nomor,
        'total': t.total,
        'created_at': format_utc_naive_as_local(t.created_at, tz_id, '%H:%M'),
        'status': t.status,
        'can_return': t.status == 'selesai',
        'return_block_reason': '' if t.status == 'selesai' else status_reason.get(
            t.status,
            f"Status transaksi '{t.status}' belum bisa diproses retur/tukar.",
        ),
        'return_url': url_for(
            'returns.create_from_transaction',
            tid=t.id,
            back_to=url_for('pos.index', focus='search'),
        ),
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
    payments_raw = data.get('payments') or []
    diskon_manual = max(0, float(data.get('diskon', 0)))
    catatan = data.get('catatan', '')
    member_id = data.get('member_id')
    voucher_code = (data.get('voucher_code') or '').strip().upper()

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
    tier_discount_pct = 0.0
    if member:
        evaluate_member_tier(member, commit=False)
        tier_discount_pct = member_tier_discount_pct(member)
        explicit_member_pct = float(member.diskon_persen or 0)
        best_pct = max(tier_discount_pct, explicit_member_pct)
        if best_pct > 0:
            diskon_member = subtotal * (best_pct / 100.0)

    promo_discount = 0.0
    promo_payload = None
    chosen_promo_type = None
    chosen_promo_name = None
    chosen_promo_code = None
    applied_voucher = None

    if voucher_code:
        item_scope = []
        for row in resolved:
            p = row['product']
            item_scope.append({
                'category_id': int(p.category_id or 0),
                'line_sub': float(row['line_sub'] or 0),
            })
        vv = validate_voucher(
            tenant_id=tenant_id,
            voucher_code=voucher_code,
            member_id=(member.id if member else None),
            subtotal=subtotal,
            items=item_scope,
        )
        if not vv.get('ok'):
            return jsonify({'success': False, 'message': vv.get('message') or 'Voucher tidak valid.'}), 400
        if float(vv.get('discount') or 0) > 0:
            applied_voucher = vv['voucher']
            promo_discount = float(vv['discount'] or 0)
            promo_payload = vv.get('payload') or {}
            chosen_promo_type = f"voucher_{applied_voucher.discount_type}"
            chosen_promo_name = applied_voucher.nama
            chosen_promo_code = applied_voucher.kode

    # Non-stacking safeguard: pick best between member benefit and voucher.
    if promo_discount > diskon_member:
        total_diskon = diskon_manual + promo_discount
    else:
        promo_discount = 0.0
        total_diskon = diskon_manual + diskon_member
        if diskon_member > 0:
            chosen_promo_type = 'tier_percent'
            chosen_promo_name = member.tier.nama if member and member.tier else 'member_discount'
            chosen_promo_code = member.tier.kode.upper() if member and member.tier else 'MEMBER'
            promo_payload = {
                'type': 'tier_or_member_discount',
                'tier_discount_pct': tier_discount_pct,
                'member_discount_pct': float(member.diskon_persen or 0) if member else 0,
                'discount': diskon_member,
            }
    discount_decision = _discount_decision_payload(
        diskon_manual=diskon_manual,
        diskon_member=diskon_member,
        promo_discount=promo_discount,
        voucher_code=chosen_promo_code,
    )
    total = subtotal - total_diskon
    if total < 0:
        total = 0.0

    allowed_payment_methods = ('tunai', 'transfer', 'qris', 'kredit')
    payment_totals = {k: 0.0 for k in allowed_payment_methods}
    if isinstance(payments_raw, list) and payments_raw:
        for row in payments_raw:
            method = str((row or {}).get('method') or '').strip().lower()
            if method not in allowed_payment_methods:
                return jsonify({'success': False, 'message': f'Metode bayar tidak valid: {method or "-"}'}), 400
            try:
                amount = float((row or {}).get('amount') or 0)
            except (TypeError, ValueError):
                return jsonify({'success': False, 'message': f'Nominal metode {method} tidak valid.'}), 400
            if amount < 0:
                return jsonify({'success': False, 'message': f'Nominal metode {method} tidak boleh negatif.'}), 400
            payment_totals[method] += amount
    else:
        # Backward compatibility payload lama.
        method = str(metode or 'tunai').strip().lower()
        if method not in allowed_payment_methods:
            method = 'tunai'
        if method == 'kredit':
            payment_totals['kredit'] = max(0.0, float(total or 0))
        else:
            payment_totals[method] = max(0.0, float(bayar or 0))

    non_cash_paid = payment_totals['transfer'] + payment_totals['qris']
    credit_paid = payment_totals['kredit']
    required_tunai = max(0.0, total - non_cash_paid - credit_paid)
    tunai_paid = payment_totals['tunai']
    if tunai_paid + 1e-9 < required_tunai:
        kurang = required_tunai - tunai_paid
        return jsonify({'success': False, 'message': f'Pembayaran kurang Rp {kurang:.0f}.'}), 400
    if credit_paid > 0 and not member:
        return jsonify({'success': False, 'message': 'Pembayaran kredit (full/campuran) wajib memilih Member!'}), 400

    bayar = tunai_paid + non_cash_paid
    kembalian = max(0.0, tunai_paid - required_tunai)
    payment_components = [
        {'method': m, 'amount': float(a)}
        for m, a in payment_totals.items()
        if float(a) > 1e-9
    ]
    if not payment_components:
        return jsonify({'success': False, 'message': 'Isi minimal satu metode pembayaran.'}), 400
    metode = payment_components[0]['method'] if len(payment_components) == 1 else 'mixed'

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
        promo_code=chosen_promo_code,
        promo_type=chosen_promo_type,
        promo_name=chosen_promo_name,
        promo_discount=promo_discount,
        promo_payload=promo_payload_json(promo_payload) if promo_payload else None,
    )
    db.session.add(trx)
    db.session.flush()

    for comp in payment_components:
        db.session.add(TransactionPayment(
            tenant_id=tenant_id,
            transaction_id=trx.id,
            method=comp['method'],
            amount=float(comp['amount'] or 0),
        ))

    stock_updates = []

    for row in resolved:
        product = row['product']
        qty = row['qty']
        harga = row['harga']

        locked_product = (
            Product.query.filter_by(id=product.id, tenant_id=tenant_id)
            .with_for_update()
            .first()
        )
        if not locked_product or float(locked_product.stok) < qty:
            db.session.rollback()
            return jsonify({
                'success': False,
                'message': f"Stok {product.nama} berubah, silakan ulangi. (sisa: {locked_product.stok if locked_product else 0})",
            }), 400

        stok_sebelum = float(locked_product.stok)

        trx_item = TransactionItem(
            transaction_id=trx.id,
            product_id=locked_product.id,
            nama_produk=product.nama,
            harga=harga,
            qty=qty,
            subtotal=harga * qty,
        )
        db.session.add(trx_item)
        db.session.flush()

        if current_app.config.get('FIFO_HPP_ENABLED', True):
            cost_info = consume_fifo_cost(
                tenant_id=tenant_id,
                product=locked_product,
                transaction_item_id=trx_item.id,
                qty_needed=qty,
                actor_user_id=current_user.id,
            )
            total_cost = float(cost_info.get('total_cost') or 0)
        else:
            # Jalur lama: pakai harga beli master ketika FIFO dimatikan.
            total_cost = float(locked_product.harga_beli or 0) * qty
        trx_item.modal_snapshot = total_cost
        trx_item.hpp_snapshot = (total_cost / qty) if qty > 0 else 0.0

        locked_product.stok -= qty
        movement = StockMovement(
            product_id=locked_product.id,
            user_id=current_user.id,
            tipe='keluar',
            qty=qty,
            stok_sebelum=stok_sebelum,
            stok_sesudah=locked_product.stok,
            keterangan=f'Penjualan #{nomor}',
        )
        db.session.add(movement)
        stock_updates.append({'id': locked_product.id, 'stok': locked_product.stok})

    if credit_paid > 0 and member:
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
            jumlah=credit_paid,
            sisa=credit_paid,
            keterangan=f'Pembelian #{nomor}',
            jatuh_tempo=jt_parsed,
        )
        db.session.add(debt)
        member.total_hutang += credit_paid

    poin_didapat = 0
    if member:
        poin_didapat = int(total // 10000)
        member.poin += poin_didapat
        member.total_belanja += total
        member.last_transaction_at = datetime.utcnow()
        evaluate_member_tier(member, commit=False)

    if applied_voucher and promo_discount > 0:
        red = VoucherRedemption(
            tenant_id=tenant_id,
            voucher_id=applied_voucher.id,
            member_id=(member.id if member else None),
            transaction_id=trx.id,
            discount_amount=promo_discount,
        )
        db.session.add(red)

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
        'promo_discount': promo_discount,
        'diskon_manual': diskon_manual,
        'payments': payment_components,
        'discount_decision': discount_decision,
    })


@pos_bp.route('/validate-voucher')
@login_required
def validate_voucher_api():
    tenant_id = current_user.tenant_id
    code = (request.args.get('code') or '').strip().upper()
    subtotal = float(request.args.get('subtotal') or 0)
    manual_discount = float(request.args.get('manual_discount') or 0)
    member_id_raw = request.args.get('member_id')
    try:
        member_id = int(member_id_raw) if member_id_raw else None
    except (TypeError, ValueError):
        member_id = None
    member_discount = 0.0
    member = None
    if member_id:
        member = Member.query.filter_by(id=member_id, tenant_id=tenant_id).first()
        if member:
            evaluate_member_tier(member, commit=False)
            best_pct = max(member_tier_discount_pct(member), float(member.diskon_persen or 0))
            if best_pct > 0:
                member_discount = subtotal * (best_pct / 100.0)

    vv = validate_voucher(
        tenant_id=tenant_id,
        voucher_code=code,
        member_id=member_id,
        subtotal=subtotal,
        items=[],
    )
    if not vv.get('ok'):
        return jsonify({
            'ok': False,
            'message': vv.get('message') or 'Voucher tidak valid',
            'discount_decision': _discount_decision_payload(
                diskon_manual=manual_discount,
                diskon_member=member_discount,
                promo_discount=0,
                voucher_code=code,
            ),
        })
    voucher_discount = float(vv.get('discount') or 0)
    chosen_voucher_discount = voucher_discount if voucher_discount > member_discount else 0.0
    return jsonify({
        'ok': True,
        'code': vv['voucher'].kode,
        'name': vv['voucher'].nama,
        'discount_preview': voucher_discount,
        'discount_decision': _discount_decision_payload(
            diskon_manual=manual_discount,
            diskon_member=member_discount,
            promo_discount=chosen_voucher_discount,
            voucher_code=vv['voucher'].kode,
        ),
    })
