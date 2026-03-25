from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from .. import db
from ..models import Member

members_bp = Blueprint('members', __name__, url_prefix='/members')


@members_bp.route('/')
@login_required
def index():
    tenant_id = current_user.tenant_id
    search = request.args.get('q', '')
    
    q = Member.query.filter_by(tenant_id=tenant_id)
    if search:
        q = q.filter(Member.nama.ilike(f'%{search}%') | Member.telepon.ilike(f'%{search}%'))
    
    members = q.order_by(Member.nama).all()
    return render_template('members/index.html', members=members, search=search)


@members_bp.route('/add', methods=['GET', 'POST'])
@login_required
def add():
    tenant_id = current_user.tenant_id
    
    if request.method == 'POST':
        telepon = request.form.get('telepon', '').strip()
        
        # Check duplicate phone
        existing = Member.query.filter_by(tenant_id=tenant_id, telepon=telepon).first()
        if existing:
            flash(f'Nomor telepon {telepon} sudah terdaftar!', 'danger')
            return redirect(url_for('members.add'))
            
        member = Member(
            tenant_id=tenant_id,
            nama=request.form['nama'].strip(),
            telepon=telepon,
            email=request.form.get('email', ''),
            alamat=request.form.get('alamat', '')
        )
        db.session.add(member)
        db.session.commit()
        
        flash(f'Member "{member.nama}" berhasil didaftarkan!', 'success')
        return redirect(url_for('members.index'))
        
    return render_template('members/form.html', member=None)


@members_bp.route('/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit(id):
    tenant_id = current_user.tenant_id
    member = Member.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    
    if request.method == 'POST':
        telepon = request.form.get('telepon', '').strip()
        
        # Check duplicate phone excluding self
        existing = Member.query.filter(Member.tenant_id==tenant_id, Member.telepon==telepon, Member.id!=id).first()
        if existing:
            flash(f'Nomor telepon {telepon} sudah terdaftar di member lain!', 'danger')
            return render_template('members/form.html', member=member)
            
        member.nama = request.form['nama'].strip()
        member.telepon = telepon
        member.email = request.form.get('email', '')
        member.alamat = request.form.get('alamat', '')
        member.aktif = 'aktif' in request.form
        
        db.session.commit()
        flash(f'Data member "{member.nama}" berhasil diupdate!', 'success')
        return redirect(url_for('members.index'))
        
    return render_template('members/form.html', member=member)


@members_bp.route('/<int:id>')
@login_required
def detail(id):
    tenant_id = current_user.tenant_id
    member = Member.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    
    # Ambil 10 transaksi terakhir
    from ..models import Transaction
    transactions = Transaction.query.filter_by(member_id=id).order_by(Transaction.created_at.desc()).limit(10).all()
    
    return render_template('members/detail.html', member=member, transactions=transactions)
