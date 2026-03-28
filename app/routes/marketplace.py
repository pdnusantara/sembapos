import json
from datetime import datetime
from functools import wraps

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    jsonify,
)
from flask_login import login_required, current_user
from sqlalchemy import or_

from .. import db
from ..models import (
    MarketplaceSeller,
    MarketplaceCategory,
    MarketplaceProduct,
    MarketplaceOrder,
    MarketplaceOrderItem,
    MarketplaceOrderStatusHistory,
    MARKETPLACE_ORDER_STATUSES,
    MARKETPLACE_ORDER_STATUS_LABELS,
    Tenant,
)

marketplace_bp = Blueprint('marketplace', __name__, url_prefix='/marketplace')

CART_SESSION_KEY = 'marketplace_cart'


def kuningan_required(f):
    """Hanya tenant Kuningan yang bisa akses marketplace."""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if getattr(current_user, 'is_superadmin', False):
            flash('Marketplace hanya untuk tenant.', 'warning')
            return redirect(url_for('superadmin.index'))
        tenant = getattr(current_user, 'tenant', None)
        if not tenant:
            flash('Akses ditolak.', 'danger')
            return redirect(url_for('dashboard.index'))
        kab = (tenant.kab_kota or '').lower()
        if 'kuningan' not in kab:
            flash(
                'Fitur Marketplace hanya tersedia untuk tenant di Kabupaten/Kota Kuningan.',
                'warning',
            )
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated


def _get_cart():
    """Ambil cart dari session sebagai dict {product_id: {qty, ...}}."""
    raw = session.get(CART_SESSION_KEY, '{}')
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {}


def _save_cart(cart):
    session[CART_SESSION_KEY] = cart
    session.modified = True


def _cart_count():
    cart = _get_cart()
    return sum(int(v.get('qty', 0)) for v in cart.values())


def _build_cart_details(cart):
    """Kembalikan list detail item cart dengan data produk terkini."""
    if not cart:
        return [], 0
    items = []
    total = 0
    for pid_str, entry in cart.items():
        try:
            pid = int(pid_str)
        except (ValueError, TypeError):
            continue
        product = MarketplaceProduct.query.get(pid)
        if not product or not product.aktif:
            continue
        qty = int(entry.get('qty', 1))
        harga = product.harga
        if product.harga_grosir and product.min_qty_grosir and qty >= product.min_qty_grosir:
            harga = product.harga_grosir
        subtotal = harga * qty
        total += subtotal
        items.append({
            'product': product,
            'qty': qty,
            'harga': harga,
            'subtotal': subtotal,
        })
    return items, total


def _generate_order_number():
    from datetime import datetime
    now = datetime.utcnow()
    prefix = f"MKT{now.strftime('%y%m%d%H%M%S')}"
    import random
    suffix = random.randint(100, 999)
    return f"{prefix}{suffix}"


# ─── ROUTES ───────────────────────────────────────────────

@marketplace_bp.route('/')
@kuningan_required
def index():
    q = request.args.get('q', '').strip()
    category_id = request.args.get('category', type=int)
    seller_id = request.args.get('seller', type=int)
    sort = request.args.get('sort', 'terbaru')
    min_harga = request.args.get('min_harga', type=float)
    max_harga = request.args.get('max_harga', type=float)
    page = request.args.get('page', 1, type=int)

    categories = MarketplaceCategory.query.filter_by(aktif=True).order_by(
        MarketplaceCategory.sort_order
    ).all()
    sellers = MarketplaceSeller.query.filter_by(aktif=True).order_by(MarketplaceSeller.nama).all()

    query = MarketplaceProduct.query.join(MarketplaceSeller).filter(
        MarketplaceProduct.aktif == True,
        MarketplaceSeller.aktif == True,
    )

    if q:
        query = query.filter(
            or_(
                MarketplaceProduct.nama.ilike(f'%{q}%'),
                MarketplaceProduct.deskripsi.ilike(f'%{q}%'),
            )
        )
    if category_id:
        query = query.filter(MarketplaceProduct.category_id == category_id)
    if seller_id:
        query = query.filter(MarketplaceProduct.seller_id == seller_id)
    if min_harga is not None:
        query = query.filter(MarketplaceProduct.harga >= min_harga)
    if max_harga is not None:
        query = query.filter(MarketplaceProduct.harga <= max_harga)

    if sort == 'termurah':
        query = query.order_by(MarketplaceProduct.harga.asc())
    elif sort == 'termahal':
        query = query.order_by(MarketplaceProduct.harga.desc())
    else:
        query = query.order_by(MarketplaceProduct.created_at.desc())

    pagination = query.paginate(page=page, per_page=20, error_out=False)
    products = pagination.items

    return render_template(
        'marketplace/index.html',
        products=products,
        pagination=pagination,
        categories=categories,
        sellers=sellers,
        q=q,
        category_id=category_id,
        seller_id=seller_id,
        sort=sort,
        min_harga=min_harga,
        max_harga=max_harga,
        cart_count=_cart_count(),
    )


@marketplace_bp.route('/produk/<int:product_id>')
@kuningan_required
def product_detail(product_id):
    product = MarketplaceProduct.query.get_or_404(product_id)
    if not product.aktif or not product.seller.aktif:
        flash('Produk tidak tersedia.', 'warning')
        return redirect(url_for('marketplace.index'))

    related = MarketplaceProduct.query.filter(
        MarketplaceProduct.id != product_id,
        MarketplaceProduct.aktif == True,
        MarketplaceProduct.seller_id == product.seller_id,
    ).limit(6).all()

    cart = _get_cart()
    cart_qty = int(cart.get(str(product_id), {}).get('qty', 0))

    return render_template(
        'marketplace/detail.html',
        product=product,
        related=related,
        cart_qty=cart_qty,
        cart_count=_cart_count(),
    )


@marketplace_bp.route('/cart')
@kuningan_required
def cart():
    cart = _get_cart()
    items, total = _build_cart_details(cart)
    return render_template(
        'marketplace/cart.html',
        items=items,
        total=total,
        cart_count=_cart_count(),
    )


@marketplace_bp.route('/cart/add', methods=['POST'])
@kuningan_required
def cart_add():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    product_id = request.form.get('product_id', type=int)
    qty = request.form.get('qty', 1, type=int)
    if qty < 1:
        qty = 1

    product = MarketplaceProduct.query.get(product_id)
    if not product or not product.aktif:
        if is_ajax:
            return jsonify({'success': False, 'message': 'Produk tidak ditemukan.'}), 404
        flash('Produk tidak ditemukan.', 'danger')
        return redirect(url_for('marketplace.index'))
    if product.stok < qty:
        if is_ajax:
            return jsonify({
                'success': False,
                'message': f'Stok tidak mencukupi. Tersedia: {product.stok}',
            }), 400
        flash(f'Stok tidak mencukupi. Stok tersedia: {product.stok}', 'warning')
        return redirect(url_for('marketplace.product_detail', product_id=product_id))

    cart = _get_cart()
    pid_str = str(product_id)
    if pid_str in cart:
        new_qty = cart[pid_str]['qty'] + qty
        if product.stok < new_qty:
            new_qty = product.stok
        cart[pid_str]['qty'] = new_qty
    else:
        cart[pid_str] = {'qty': qty}
    _save_cart(cart)

    if is_ajax:
        return jsonify({
            'success': True,
            'message': f'"{product.nama}" ditambahkan ke keranjang.',
            'cart_count': _cart_count(),
        })

    flash(f'"{product.nama}" ditambahkan ke keranjang.', 'success')
    if request.form.get('redirect_to') == 'cart':
        return redirect(url_for('marketplace.cart'))
    return redirect(url_for('marketplace.product_detail', product_id=product_id))


@marketplace_bp.route('/cart/update', methods=['POST'])
@kuningan_required
def cart_update():
    product_id = request.form.get('product_id', type=int)
    action = request.form.get('action', 'update')
    qty = request.form.get('qty', 1, type=int)

    cart = _get_cart()
    pid_str = str(product_id)

    if action == 'remove' or qty < 1:
        cart.pop(pid_str, None)
    else:
        product = MarketplaceProduct.query.get(product_id)
        if product:
            qty = min(qty, product.stok)
        cart[pid_str] = {'qty': qty}

    _save_cart(cart)
    return redirect(url_for('marketplace.cart'))


@marketplace_bp.route('/checkout', methods=['GET', 'POST'])
@kuningan_required
def checkout():
    cart = _get_cart()
    if not cart:
        flash('Keranjang kosong.', 'warning')
        return redirect(url_for('marketplace.index'))

    items, total = _build_cart_details(cart)
    if not items:
        flash('Tidak ada item valid di keranjang.', 'warning')
        _save_cart({})
        return redirect(url_for('marketplace.index'))

    # Kelompokkan per seller
    seller_groups = {}
    for item in items:
        sid = item['product'].seller_id
        if sid not in seller_groups:
            seller_groups[sid] = {
                'seller': item['product'].seller,
                'items': [],
                'subtotal': 0,
            }
        seller_groups[sid]['items'].append(item)
        seller_groups[sid]['subtotal'] += item['subtotal']

    if request.method == 'POST':
        nama_penerima = request.form.get('nama_penerima', '').strip()
        telepon_penerima = request.form.get('telepon_penerima', '').strip()
        alamat_kirim = request.form.get('alamat_kirim', '').strip()
        catatan = request.form.get('catatan', '').strip()

        errors = []
        if not nama_penerima:
            errors.append('Nama penerima wajib diisi.')
        if not telepon_penerima:
            errors.append('Telepon penerima wajib diisi.')
        if not alamat_kirim:
            errors.append('Alamat kirim wajib diisi.')

        # Validasi stok terkini sebelum membuat order
        if not errors:
            for item in items:
                fresh = MarketplaceProduct.query.get(item['product'].id)
                if not fresh or fresh.stok < item['qty']:
                    avail = fresh.stok if fresh else 0
                    errors.append(
                        f'Stok "{item["product"].nama}" tidak mencukupi '
                        f'(diminta {item["qty"]}, tersedia {avail}).'
                    )

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template(
                'marketplace/checkout.html',
                items=items,
                total=total,
                seller_groups=list(seller_groups.values()),
                cart_count=_cart_count(),
                tenant=current_user.tenant,
            )

        try:
            created_orders = []
            for sid, grp in seller_groups.items():
                order_number = _generate_order_number()
                order = MarketplaceOrder(
                    tenant_id=current_user.tenant_id,
                    seller_id=sid,
                    nomor=order_number,
                    status='pending',
                    total=grp['subtotal'],
                    nama_penerima=nama_penerima,
                    telepon_penerima=telepon_penerima,
                    alamat_kirim=alamat_kirim,
                    catatan=catatan,
                )
                db.session.add(order)
                db.session.flush()

                for item in grp['items']:
                    oi = MarketplaceOrderItem(
                        order_id=order.id,
                        product_id=item['product'].id,
                        nama_produk=item['product'].nama,
                        harga=item['harga'],
                        qty=item['qty'],
                        subtotal=item['subtotal'],
                        satuan=item['product'].satuan or 'pcs',
                    )
                    db.session.add(oi)

                    # Kurangi stok produk
                    product = MarketplaceProduct.query.get(item['product'].id)
                    if product:
                        product.stok = max(0, product.stok - item['qty'])

                history = MarketplaceOrderStatusHistory(
                    order_id=order.id,
                    from_status=None,
                    to_status='pending',
                    catatan='Pesanan dibuat oleh tenant.',
                    changed_by_user_id=current_user.id,
                )
                db.session.add(history)
                created_orders.append(order)

            db.session.commit()
            _save_cart({})

            flash(
                f'Pesanan berhasil dibuat! {len(created_orders)} order dikirim ke seller.',
                'success',
            )
            return redirect(url_for('marketplace.orders'))

        except Exception as e:
            db.session.rollback()
            flash(f'Terjadi kesalahan: {str(e)}', 'danger')

    return render_template(
        'marketplace/checkout.html',
        items=items,
        total=total,
        seller_groups=list(seller_groups.values()),
        cart_count=_cart_count(),
        tenant=current_user.tenant,
    )


@marketplace_bp.route('/orders')
@kuningan_required
def orders():
    page = request.args.get('page', 1, type=int)
    status_filter = request.args.get('status', '')

    q = MarketplaceOrder.query.filter_by(tenant_id=current_user.tenant_id)
    if status_filter:
        q = q.filter_by(status=status_filter)
    q = q.order_by(MarketplaceOrder.created_at.desc())

    pagination = q.paginate(page=page, per_page=20, error_out=False)

    return render_template(
        'marketplace/orders.html',
        orders=pagination.items,
        pagination=pagination,
        status_filter=status_filter,
        all_statuses=MARKETPLACE_ORDER_STATUSES,
        cart_count=_cart_count(),
    )


@marketplace_bp.route('/orders/<int:order_id>')
@kuningan_required
def order_detail(order_id):
    order = MarketplaceOrder.query.filter_by(
        id=order_id, tenant_id=current_user.tenant_id
    ).first_or_404()

    return render_template(
        'marketplace/order_detail.html',
        order=order,
        status_labels=MARKETPLACE_ORDER_STATUS_LABELS,
        cart_count=_cart_count(),
    )


@marketplace_bp.route('/orders/<int:order_id>/cancel', methods=['POST'])
@kuningan_required
def order_cancel(order_id):
    order = MarketplaceOrder.query.filter_by(
        id=order_id, tenant_id=current_user.tenant_id
    ).first_or_404()

    if order.status not in ('pending',):
        flash('Pesanan tidak dapat dibatalkan pada status ini.', 'warning')
        return redirect(url_for('marketplace.order_detail', order_id=order_id))

    old_status = order.status
    order.status = 'cancelled'
    history = MarketplaceOrderStatusHistory(
        order_id=order.id,
        from_status=old_status,
        to_status='cancelled',
        catatan=request.form.get('alasan', 'Dibatalkan oleh tenant.'),
        changed_by_user_id=current_user.id,
    )
    db.session.add(history)

    # Kembalikan stok produk
    for item in order.items:
        if item.product_id:
            product = MarketplaceProduct.query.get(item.product_id)
            if product:
                product.stok += item.qty

    db.session.commit()
    flash('Pesanan berhasil dibatalkan dan stok telah dikembalikan.', 'success')
    return redirect(url_for('marketplace.orders'))


@marketplace_bp.route('/orders/<int:order_id>/repeat', methods=['POST'])
@kuningan_required
def order_repeat(order_id):
    order = MarketplaceOrder.query.filter_by(
        id=order_id, tenant_id=current_user.tenant_id
    ).first_or_404()

    cart = _get_cart()
    added = []
    skipped = []

    for item in order.items:
        if not item.product_id:
            skipped.append(item.nama_produk)
            continue
        product = MarketplaceProduct.query.get(item.product_id)
        if not product or not product.aktif:
            skipped.append(item.nama_produk)
            continue
        if product.stok <= 0:
            skipped.append(item.nama_produk)
            continue

        pid_str = str(item.product_id)
        desired_qty = item.qty
        if pid_str in cart:
            desired_qty = cart[pid_str]['qty'] + item.qty
        capped_qty = min(desired_qty, product.stok)
        cart[pid_str] = {'qty': capped_qty}
        added.append(item.nama_produk)

    _save_cart(cart)

    if added:
        flash(f'{len(added)} produk ditambahkan ke keranjang.', 'success')
    if skipped:
        flash(f'{len(skipped)} produk dilewati (tidak tersedia/stok habis): {", ".join(skipped)}.', 'warning')

    return redirect(url_for('marketplace.cart'))


@marketplace_bp.route('/orders/<int:order_id>/invoice')
@kuningan_required
def order_invoice(order_id):
    order = MarketplaceOrder.query.filter_by(
        id=order_id, tenant_id=current_user.tenant_id
    ).first_or_404()
    return render_template('marketplace/invoice.html', order=order, is_admin=False,
                           now=datetime.utcnow())


@marketplace_bp.context_processor
def inject_cart_count():
    return {'cart_count': _cart_count()}
