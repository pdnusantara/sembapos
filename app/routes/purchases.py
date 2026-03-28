from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from datetime import datetime
from .. import db
from ..models import PurchaseOrder, PurchaseOrderItem, Supplier, Product, StockMovement
from ..fifo_costing import create_cost_layer

purchases_bp = Blueprint('purchases', __name__, url_prefix='/purchases')


def require_admin():
    if current_user.role not in ['superadmin', 'admin']:
        flash('Akses ditolak!', 'danger')
        return False
    return True


def generate_po_number(tenant_id, branch_id):
    today = datetime.utcnow()
    prefix = f"PO-{today.strftime('%Y%m%d')}-{branch_id:04d}"
    last = PurchaseOrder.query.filter(
        PurchaseOrder.nomor.like(f"{prefix}%")
    ).order_by(PurchaseOrder.id.desc()).first()
    
    if last:
        last_num = int(last.nomor.split('-')[-1]) + 1
    else:
        last_num = 1
    return f"{prefix}-{last_num:04d}"


@purchases_bp.route('/')
@login_required
def index():
    tenant_id = current_user.tenant_id
    pos = PurchaseOrder.query.filter_by(tenant_id=tenant_id).order_by(PurchaseOrder.tanggal_pesan.desc()).all()
    return render_template('purchases/index.html', pos=pos)


@purchases_bp.route('/add', methods=['GET', 'POST'])
@login_required
def add():
    if not require_admin():
        return redirect(url_for('purchases.index'))
        
    tenant_id = current_user.tenant_id
    branch_id = current_user.branch_id or 1  # Fallback to 1 if no branch assigned
    
    if request.method == 'POST':
        supplier_id = request.form.get('supplier_id')
        catatan = request.form.get('catatan', '')
        
        # Parse items (product_id, qty, harga)
        product_ids = request.form.getlist('product_id[]')
        qtys = request.form.getlist('qty[]')
        hargas = request.form.getlist('harga[]')
        
        if not product_ids or not supplier_id:
            flash('Pilih supplier dan minimal 1 produk!', 'danger')
            return redirect(url_for('purchases.add'))
            
        nomor = generate_po_number(tenant_id, branch_id)
        po = PurchaseOrder(
            tenant_id=tenant_id,
            branch_id=branch_id,
            supplier_id=supplier_id,
            user_id=current_user.id,
            nomor=nomor,
            status='dipesan',
            catatan=catatan
        )
        db.session.add(po)
        db.session.flush()
        
        total = 0
        for pid, qty_str, harga_str in zip(product_ids, qtys, hargas):
            if not pid or not qty_str or float(qty_str) <= 0:
                continue
                
            qty = float(qty_str)
            harga = float(harga_str)
            product = Product.query.get(int(pid))
            
            subtotal = qty * harga
            total += subtotal
            
            item = PurchaseOrderItem(
                po_id=po.id,
                product_id=product.id,
                nama_produk=product.nama,
                harga_beli=harga,
                qty_pesan=qty,
                subtotal=subtotal
            )
            db.session.add(item)
            
            # Update harga beli terakhir di master produk
            product.harga_beli = harga
            
        po.total = total
        db.session.commit()
        
        flash(f'Purchase Order {nomor} berhasil dibuat!', 'success')
        return redirect(url_for('purchases.detail', id=po.id))
        
    suppliers = Supplier.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Supplier.nama).all()
    products = Product.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(Product.nama).all()
    
    return render_template('purchases/form.html', suppliers=suppliers, products=products)


@purchases_bp.route('/<int:id>')
@login_required
def detail(id):
    tenant_id = current_user.tenant_id
    po = PurchaseOrder.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    return render_template('purchases/detail.html', po=po)


@purchases_bp.route('/<int:id>/receive', methods=['POST'])
@login_required
def receive(id):
    if not require_admin():
        return redirect(url_for('purchases.index'))
        
    tenant_id = current_user.tenant_id
    po = PurchaseOrder.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    
    if po.status == 'diterima':
        flash('Purchase Order ini sudah diterima!', 'warning')
        return redirect(url_for('purchases.detail', id=po.id))
        
    po.status = 'diterima'
    po.tanggal_terima = datetime.utcnow()
    
    for item in po.items:
        # Get actual received qty from form, default to ordered qty
        terima_str = request.form.get(f'terima_{item.id}', item.qty_pesan)
        try:
            qty_terima = float(terima_str)
        except ValueError:
            qty_terima = item.qty_pesan
            
        item.qty_terima = qty_terima
        
        # Update product stock
        if qty_terima > 0:
            product = Product.query.get(item.product_id)
            if product:
                stok_sebelum = product.stok
                product.stok += qty_terima
                create_cost_layer(
                    tenant_id=tenant_id,
                    product_id=product.id,
                    qty_in=qty_terima,
                    unit_cost=float(item.harga_beli or 0),
                    source_type='po_receive',
                    source_id=po.id,
                    received_at=po.tanggal_terima or datetime.utcnow(),
                )
                
                # Record movement
                movement = StockMovement(
                    product_id=product.id,
                    user_id=current_user.id,
                    tipe='masuk',
                    qty=qty_terima,
                    stok_sebelum=stok_sebelum,
                    stok_sesudah=product.stok,
                    keterangan=f'Penerimaan PO #{po.nomor}'
                )
                db.session.add(movement)
                
    db.session.commit()
    flash(f'Barang dari PO {po.nomor} berhasil diterima dan stok terupdate!', 'success')
    return redirect(url_for('purchases.detail', id=po.id))


@purchases_bp.route('/<int:id>/cancel', methods=['POST'])
@login_required
def cancel(id):
    if not require_admin():
        return redirect(url_for('purchases.index'))
        
    po = PurchaseOrder.query.filter_by(id=id, tenant_id=current_user.tenant_id).first_or_404()
    if po.status != 'dipesan':
        flash('Hanya PO dengan status Dipesan yang bisa dibatalkan.', 'danger')
    else:
        po.status = 'batal'
        db.session.commit()
        flash(f'Purchase Order {po.nomor} dibatalkan.', 'warning')
        
    return redirect(url_for('purchases.detail', id=po.id))
