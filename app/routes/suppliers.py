from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from .. import db
from ..models import Supplier

suppliers_bp = Blueprint('suppliers', __name__, url_prefix='/suppliers')


def require_admin():
    if current_user.role not in ['superadmin', 'admin']:
        flash('Akses ditolak!', 'danger')
        return False
    return True


@suppliers_bp.route('/')
@login_required
def index():
    tenant_id = current_user.tenant_id
    search = request.args.get('q', '')
    
    q = Supplier.query.filter_by(tenant_id=tenant_id)
    if search:
        q = q.filter(Supplier.nama.ilike(f'%{search}%'))
    
    suppliers = q.order_by(Supplier.nama).all()
    return render_template('suppliers/index.html', suppliers=suppliers, search=search)


@suppliers_bp.route('/add', methods=['GET', 'POST'])
@login_required
def add():
    if not require_admin():
        return redirect(url_for('suppliers.index'))
        
    if request.method == 'POST':
        supplier = Supplier(
            tenant_id=current_user.tenant_id,
            nama=request.form['nama'].strip(),
            kontak=request.form.get('kontak', ''),
            telepon=request.form.get('telepon', ''),
            email=request.form.get('email', ''),
            alamat=request.form.get('alamat', '')
        )
        db.session.add(supplier)
        db.session.commit()
        flash(f'Supplier "{supplier.nama}" berhasil ditambahkan!', 'success')
        return redirect(url_for('suppliers.index'))
        
    return render_template('suppliers/form.html', supplier=None)


@suppliers_bp.route('/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit(id):
    if not require_admin():
        return redirect(url_for('suppliers.index'))
        
    supplier = Supplier.query.filter_by(id=id, tenant_id=current_user.tenant_id).first_or_404()
    
    if request.method == 'POST':
        supplier.nama = request.form['nama'].strip()
        supplier.kontak = request.form.get('kontak', '')
        supplier.telepon = request.form.get('telepon', '')
        supplier.email = request.form.get('email', '')
        supplier.alamat = request.form.get('alamat', '')
        supplier.aktif = 'aktif' in request.form
        db.session.commit()
        flash(f'Supplier "{supplier.nama}" berhasil diupdate!', 'success')
        return redirect(url_for('suppliers.index'))
        
    return render_template('suppliers/form.html', supplier=supplier)


@suppliers_bp.route('/delete/<int:id>', methods=['POST'])
@login_required
def delete(id):
    if not require_admin():
        return redirect(url_for('suppliers.index'))
        
    supplier = Supplier.query.filter_by(id=id, tenant_id=current_user.tenant_id).first_or_404()
    supplier.aktif = False
    db.session.commit()
    flash(f'Supplier "{supplier.nama}" dinonaktifkan.', 'warning')
    return redirect(url_for('suppliers.index'))
