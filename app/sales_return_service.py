"""Logika nota retur penjualan: stok masuk, batas qty, hutang/member, opsional transaksi tukar."""
from datetime import datetime

from sqlalchemy import func

from . import db
from .models import (
    SalesReturn,
    SalesReturnItem,
    Transaction,
    TransactionItem,
    Product,
    StockMovement,
    Debt,
    Member,
)
from .fifo_costing import consume_fifo_cost, restore_fifo_from_transaction_item


def qty_already_returned(source_transaction_item_id):
    q = (
        db.session.query(func.coalesce(func.sum(SalesReturnItem.qty_retur), 0))
        .join(SalesReturn, SalesReturn.id == SalesReturnItem.return_id)
        .filter(SalesReturnItem.source_transaction_item_id == source_transaction_item_id)
        .scalar()
    )
    return float(q or 0)


def generate_nomor_retur(tenant_id, branch_id):
    today = datetime.utcnow()
    prefix = f"RET-{today.strftime('%Y%m%d')}-{branch_id:04d}"
    last = (
        SalesReturn.query.filter(
            SalesReturn.tenant_id == tenant_id,
            SalesReturn.nomor.like(f"{prefix}%"),
        )
        .order_by(SalesReturn.id.desc())
        .first()
    )
    if last:
        last_num = int(last.nomor.split('-')[-1]) + 1
    else:
        last_num = 1
    return f"{prefix}-{last_num:04d}"


def generate_nomor_transaksi(tenant_id, branch_id):
    today = datetime.utcnow()
    prefix = f"TRX-{today.strftime('%Y%m%d')}-{branch_id:04d}"
    last = (
        Transaction.query.filter(Transaction.nomor.like(f"{prefix}%"))
        .order_by(Transaction.id.desc())
        .first()
    )
    if last:
        last_num = int(last.nomor.split('-')[-1]) + 1
    else:
        last_num = 1
    return f"{prefix}-{last_num:04d}"


def _price_for_qty(product, qty):
    picked = product.price_for_qty(qty)
    return float(picked.get('harga', product.harga_jual or 0)), picked.get('label', 'ecer')


REFUND_METHODS = frozenset(('tunai', 'transfer', 'qris', 'potong_hutang', 'tanpa_uang'))


def process_sales_return(
    *,
    user,
    tenant_id,
    branch_id,
    shift,
    source,
    line_inputs,
    alasan,
    catatan,
    metode_pengembalian,
    jenis,
    replacement=None,
):
    """
    line_inputs: list of dict with keys transaction_item_id, qty (float).
    replacement: None or dict items[{id, qty}], metode_bayar, bayar, diskon_manual, catatan, use_source_member bool
    shift: CashierShift instance or None
    """
    if source.tenant_id != tenant_id:
        raise ValueError('Transaksi tidak valid.')
    if source.status != 'selesai':
        raise ValueError('Hanya transaksi selesai yang bisa diretur.')
    if source.branch_id != branch_id:
        raise ValueError('Cabang retur harus sama dengan nota asli.')

    metode_pengembalian = (metode_pengembalian or 'tunai').strip().lower()
    if metode_pengembalian not in REFUND_METHODS:
        raise ValueError('Metode pengembalian tidak valid.')

    jenis = (jenis or 'retur').strip().lower()
    if jenis not in ('retur', 'tukar'):
        jenis = 'retur'

    if jenis == 'tukar':
        if not replacement or not replacement.get('items'):
            raise ValueError('Tukar barang wajib memilih produk pengganti.')
        if shift is None:
            raise ValueError('Buka shift kasir terlebih dahulu untuk transaksi tukar.')

    item_by_id = {it.id: it for it in source.items}
    resolved_lines = []
    total_retur = 0.0

    for row in line_inputs:
        try:
            ti_id = int(row['transaction_item_id'])
            qty = float(row['qty'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('Data baris retur tidak valid.')
        if qty <= 0:
            continue
        ti = item_by_id.get(ti_id)
        if not ti:
            raise ValueError('Baris nota tidak termasuk transaksi ini.')
        avail = float(ti.qty) - qty_already_returned(ti.id)
        if qty > avail + 1e-9:
            raise ValueError(
                f'Qty retur "{ti.nama_produk}" melebihi yang bisa diretur (tersisa {avail}).',
            )
        sub = float(ti.harga) * qty
        total_retur += sub
        resolved_lines.append({
            'ti': ti,
            'qty': qty,
            'sub': sub,
        })

    if not resolved_lines:
        raise ValueError('Pilih minimal satu baris dengan qty retur > 0.')

    debt = Debt.query.filter_by(transaction_id=source.id).first()
    if source.metode_bayar == 'kredit' and debt and float(debt.sisa or 0) > 0:
        if total_retur > float(debt.sisa) + 1e-6:
            raise ValueError(
                'Nilai retur tidak boleh melebihi sisa hutang pada nota ini.',
            )
        metode_pengembalian = 'potong_hutang'
    elif source.metode_bayar == 'kredit' and debt and float(debt.sisa or 0) <= 0:
        if metode_pengembalian == 'potong_hutang':
            metode_pengembalian = 'tunai'

    member = None
    if source.member_id:
        member = Member.query.filter_by(id=source.member_id, tenant_id=tenant_id).first()

    nomor = generate_nomor_retur(tenant_id, branch_id)
    sr = SalesReturn(
        tenant_id=tenant_id,
        branch_id=branch_id,
        user_id=user.id,
        shift_id=shift.id if shift else None,
        source_transaction_id=source.id,
        nomor=nomor,
        total_retur=total_retur,
        alasan=(alasan or '').strip() or None,
        catatan=(catatan or '').strip() or None,
        jenis=jenis,
        metode_pengembalian=metode_pengembalian,
        created_at=datetime.utcnow(),
    )
    db.session.add(sr)
    db.session.flush()

    for row in resolved_lines:
        ti = row['ti']
        qty = row['qty']
        sub = row['sub']
        db.session.add(
            SalesReturnItem(
                return_id=sr.id,
                source_transaction_item_id=ti.id,
                product_id=ti.product_id,
                qty_retur=qty,
                harga=float(ti.harga),
                subtotal=sub,
            )
        )
        product = Product.query.filter_by(id=ti.product_id, tenant_id=tenant_id).first()
        if not product:
            raise ValueError(f'Produk untuk baris "{ti.nama_produk}" tidak ditemukan.')
        db.session.refresh(product)
        stok_sebelum = float(product.stok)
        product.stok = stok_sebelum + qty
        db.session.add(
            StockMovement(
                product_id=product.id,
                user_id=user.id,
                tipe='masuk',
                qty=qty,
                stok_sebelum=stok_sebelum,
                stok_sesudah=product.stok,
                keterangan=f'Retur #{nomor}',
            )
        )
        restore_fifo_from_transaction_item(
            tenant_id=tenant_id,
            source_transaction_item=ti,
            qty_return=qty,
            actor_user_id=user.id,
        )

    if member:
        poin_cut = int(total_retur // 10000)
        member.poin = max(0, int(member.poin or 0) - poin_cut)
        member.total_belanja = max(0.0, float(member.total_belanja or 0) - total_retur)

    if source.metode_bayar == 'kredit' and debt and float(debt.sisa or 0) > 0:
        debt.sisa = max(0.0, float(debt.sisa) - total_retur)
        if member:
            member.total_hutang = max(0.0, float(member.total_hutang or 0) - total_retur)
        if debt.sisa <= 1e-6:
            debt.sisa = 0.0
            debt.status = 'lunas'

    repl_trx = None
    if jenis == 'tukar' and replacement:
        repl_trx = _create_replacement_sale(
            user=user,
            tenant_id=tenant_id,
            branch_id=branch_id,
            shift=shift,
            items_raw=replacement['items'],
            metode_bayar=replacement.get('metode_bayar', 'tunai'),
            bayar=float(replacement.get('bayar', 0)),
            diskon_manual=max(0.0, float(replacement.get('diskon_manual', 0))),
            catatan_repl=(replacement.get('catatan') or '').strip(),
            member=member if replacement.get('use_source_member') and member else None,
            debt_jt=replacement.get('debt_jatuh_tempo'),
        )
        sr.replacement_transaction_id = repl_trx.id

    db.session.flush()
    return sr, repl_trx


def _create_replacement_sale(
    *,
    user,
    tenant_id,
    branch_id,
    shift,
    items_raw,
    metode_bayar,
    bayar,
    diskon_manual,
    catatan_repl,
    member,
    debt_jt,
):
    allow_override = user.role in ('superadmin', 'admin')
    resolved = []
    subtotal = 0.0
    for raw in items_raw:
        try:
            pid = int(raw['id'])
            qty = float(raw['qty'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('Data produk tukar tidak valid.')
        if qty <= 0:
            raise ValueError('Qty produk tukar harus > 0.')
        product = Product.query.filter_by(id=pid, tenant_id=tenant_id).first()
        if not product or not product.aktif:
            raise ValueError('Produk tukar tidak ditemukan atau nonaktif.')
        price_mode = str(raw.get('price_mode') or 'auto').strip().lower()
        if price_mode not in ('auto', 'ecer', 'manual'):
            price_mode = 'auto'
        if price_mode == 'ecer':
            harga = float(product.harga_jual or 0)
        else:
            harga, _ = _price_for_qty(product, qty)
        if price_mode == 'manual':
            if not allow_override:
                raise ValueError('Harga manual tukar hanya untuk admin.')
            try:
                h = float(raw['harga'])
            except (TypeError, ValueError):
                h = harga
            floor_p = max(0.0, float(product.harga_beli or 0))
            if h < floor_p:
                raise ValueError(f'Harga "{product.nama}" di bawah harga beli.')
            if h > float(product.harga_jual) * 10:
                raise ValueError(f'Harga "{product.nama}" terlalu tinggi.')
            harga = h
        line_sub = harga * qty
        subtotal += line_sub
        resolved.append({'product': product, 'qty': qty, 'harga': harga, 'line_sub': line_sub})

    for row in resolved:
        if float(row['product'].stok) < row['qty']:
            raise ValueError(
                f"Stok {row['product'].nama} tidak cukup untuk tukar.",
            )

    diskon_member = 0.0
    if member and (member.diskon_persen or 0) > 0:
        diskon_member = subtotal * (float(member.diskon_persen) / 100.0)
    total_diskon = diskon_manual + diskon_member
    total = max(0.0, subtotal - total_diskon)
    kembalian = bayar - total
    metode = (metode_bayar or 'tunai').strip().lower()
    if metode not in ('tunai', 'transfer', 'qris', 'kredit'):
        metode = 'tunai'
    if metode == 'kredit':
        if not member:
            raise ValueError('Tukar dengan kredit wajib memakai member nota asli.')
        bayar = 0.0
        kembalian = 0.0
    elif bayar < total:
        raise ValueError(f'Pembayaran produk tukar kurang. Total: {total:.0f}')

    nomor = generate_nomor_transaksi(tenant_id, branch_id)
    trx = Transaction(
        tenant_id=tenant_id,
        branch_id=branch_id,
        user_id=user.id,
        member_id=member.id if member else None,
        nomor=nomor,
        subtotal=subtotal,
        diskon=total_diskon,
        total=total,
        bayar=bayar,
        kembalian=max(0.0, kembalian),
        metode_bayar=metode,
        catatan=catatan_repl or 'Penjualan pengganti (tukar)',
        status='selesai',
        shift_id=shift.id,
    )
    db.session.add(trx)
    db.session.flush()

    for row in resolved:
        product = row['product']
        qty = row['qty']
        harga = row['harga']
        db.session.refresh(product)
        if float(product.stok) < qty:
            raise ValueError(f"Stok {product.nama} berubah, ulangi.")
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
        db.session.flush()
        cost_info = consume_fifo_cost(
            tenant_id=tenant_id,
            product=product,
            transaction_item_id=trx_item.id,
            qty_needed=qty,
            actor_user_id=user.id,
        )
        total_cost = float(cost_info.get('total_cost') or 0)
        trx_item.modal_snapshot = total_cost
        trx_item.hpp_snapshot = (total_cost / qty) if qty > 0 else 0.0
        product.stok -= qty
        db.session.add(
            StockMovement(
                product_id=product.id,
                user_id=user.id,
                tipe='keluar',
                qty=qty,
                stok_sebelum=stok_sebelum,
                stok_sesudah=product.stok,
                keterangan=f'Penjualan (tukar) #{nomor}',
            )
        )

    if metode == 'kredit' and member:
        jt_parsed = None
        if debt_jt and len(str(debt_jt)) >= 10:
            try:
                jt_parsed = datetime.strptime(str(debt_jt)[:10], '%Y-%m-%d')
            except ValueError:
                jt_parsed = None
        db.session.add(
            Debt(
                tenant_id=tenant_id,
                member_id=member.id,
                transaction_id=trx.id,
                jumlah=total,
                sisa=total,
                keterangan=f'Pembelian (tukar) #{nomor}',
                jatuh_tempo=jt_parsed,
            )
        )
        member.total_hutang += total

    if member:
        poin_d = int(total // 10000)
        member.poin += poin_d
        member.total_belanja += total

    return trx
