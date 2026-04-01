from datetime import datetime, timedelta, timezone as dt_timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from .. import db
from ..models import (
    Member,
    MemberTier,
    Tenant,
    Voucher,
    VoucherCategoryScope,
    VoucherRedemption,
    ProductCategory,
    Product,
    Transaction,
    TransactionItem,
)
from ..loyalty_service import ensure_default_tiers, evaluate_member_tier, member_top_products
from ..timezones import get_zoneinfo_required, normalize_timezone_id

members_bp = Blueprint('members', __name__, url_prefix='/members')


@members_bp.route('/')
@login_required
def index():
    tenant_id = current_user.tenant_id
    search = request.args.get('q', '')

    ensure_default_tiers(tenant_id)
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
        evaluate_member_tier(member, commit=False)
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
        evaluate_member_tier(member, commit=False)
        db.session.commit()
        flash(f'Data member "{member.nama}" berhasil diupdate!', 'success')
        return redirect(url_for('members.index'))
        
    return render_template('members/form.html', member=member)


@members_bp.route('/<int:id>')
@login_required
def detail(id):
    tenant_id = current_user.tenant_id
    member = Member.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()

    # Rolling performance metrics (365d default)
    rolling_days = int(member.rolling_last_days or 365)
    window_start = datetime.utcnow() - timedelta(days=rolling_days)
    trx_base_q = Transaction.query.filter(
        Transaction.tenant_id == tenant_id,
        Transaction.member_id == id,
        Transaction.status == 'selesai',
    )
    trx_rolling_q = trx_base_q.filter(Transaction.created_at >= window_start)

    total_tx = int(trx_base_q.with_entities(func.count(Transaction.id)).scalar() or 0)
    total_tx_rolling = int(trx_rolling_q.with_entities(func.count(Transaction.id)).scalar() or 0)
    rolling_spend = float(trx_rolling_q.with_entities(func.coalesce(func.sum(Transaction.total), 0)).scalar() or 0)
    aov_rolling = (rolling_spend / total_tx_rolling) if total_tx_rolling else 0
    first_tx = trx_base_q.order_by(Transaction.created_at.asc()).first()
    last_tx = trx_base_q.order_by(Transaction.created_at.desc()).first()

    # Favorite products
    top_products_rows = member_top_products(id, tenant_id, limit=5)
    top_products = [{
        'nama': r.nama_produk,
        'qty': float(r.qty_total or 0),
        'omzet': float(r.omzet_total or 0),
    } for r in top_products_rows]

    # Favorite categories
    top_categories_rows = (
        db.session.query(
            ProductCategory.nama.label('nama'),
            func.coalesce(func.sum(TransactionItem.subtotal), 0).label('omzet'),
        )
        .join(Product, Product.category_id == ProductCategory.id, isouter=True)
        .join(TransactionItem, TransactionItem.product_id == Product.id, isouter=True)
        .join(Transaction, Transaction.id == TransactionItem.transaction_id)
        .filter(
            Transaction.tenant_id == tenant_id,
            Transaction.member_id == id,
            Transaction.status == 'selesai',
        )
        .group_by(ProductCategory.nama)
        .order_by(func.sum(TransactionItem.subtotal).desc())
        .limit(5)
        .all()
    )
    top_categories = [{
        'nama': r.nama or 'Tanpa kategori',
        'omzet': float(r.omzet or 0),
    } for r in top_categories_rows]

    transactions = trx_base_q.order_by(Transaction.created_at.desc()).limit(20).all()
    voucher_history = (
        VoucherRedemption.query.filter_by(tenant_id=tenant_id, member_id=id)
        .order_by(VoucherRedemption.created_at.desc())
        .limit(20)
        .all()
    )
    segment = 'Aktif'
    if total_tx_rolling == 0:
        segment = 'Dormant'
    elif total_tx_rolling <= 2:
        segment = 'Occasional'
    elif total_tx_rolling >= 8:
        segment = 'Loyal'

    return render_template(
        'members/detail.html',
        member=member,
        transactions=transactions,
        total_tx=total_tx,
        total_tx_rolling=total_tx_rolling,
        rolling_spend=rolling_spend,
        aov_rolling=aov_rolling,
        first_tx=first_tx,
        last_tx=last_tx,
        top_products=top_products,
        top_categories=top_categories,
        voucher_history=voucher_history,
        segment=segment,
    )


@members_bp.route('/tiers', methods=['GET', 'POST'])
@login_required
def tiers():
    tenant_id = current_user.tenant_id
    if current_user.role not in ('superadmin', 'admin'):
        flash('Hanya admin yang dapat mengelola tier member.', 'danger')
        return redirect(url_for('members.index'))

    ensure_default_tiers(tenant_id)
    if request.method == 'POST':
        tier_id = request.form.get('tier_id')
        kode = (request.form.get('kode') or '').strip().lower()
        nama = (request.form.get('nama') or '').strip()
        min_spend = max(0, float(request.form.get('min_spend') or 0))
        benefit_discount_pct = max(0, float(request.form.get('benefit_discount_pct') or 0))
        sort_order = int(request.form.get('sort_order') or 0)
        aktif = 'aktif' in request.form
        if not kode or not nama:
            flash('Kode dan nama tier wajib diisi.', 'danger')
            return redirect(url_for('members.tiers'))
        try:
            if tier_id:
                tier = MemberTier.query.filter_by(id=int(tier_id), tenant_id=tenant_id).first_or_404()
            else:
                tier = MemberTier(tenant_id=tenant_id, kode=kode)
                db.session.add(tier)
            tier.kode = kode
            tier.nama = nama
            tier.min_spend = min_spend
            tier.benefit_discount_pct = benefit_discount_pct
            tier.sort_order = sort_order
            tier.aktif = aktif
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash('Gagal menyimpan tier. Pastikan kode tier unik.', 'danger')
            return redirect(url_for('members.tiers'))
        flash('Tier member tersimpan.', 'success')
        return redirect(url_for('members.tiers'))

    tiers = MemberTier.query.filter_by(tenant_id=tenant_id).order_by(MemberTier.min_spend.asc()).all()
    return render_template('members/tiers.html', tiers=tiers)


@members_bp.route('/tiers/<int:id>/delete', methods=['POST'])
@login_required
def delete_tier(id):
    tenant_id = current_user.tenant_id
    if current_user.role not in ('superadmin', 'admin'):
        flash('Hanya admin yang dapat menghapus tier.', 'danger')
        return redirect(url_for('members.tiers'))
    tier = MemberTier.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    used_count = Member.query.filter_by(tenant_id=tenant_id, tier_id=tier.id).count()
    if used_count > 0:
        flash(f'Tier "{tier.nama}" tidak bisa dihapus karena dipakai {used_count} member.', 'warning')
        return redirect(url_for('members.tiers'))
    db.session.delete(tier)
    db.session.commit()
    flash('Tier berhasil dihapus.', 'success')
    return redirect(url_for('members.tiers'))


@members_bp.route('/vouchers')
@login_required
def vouchers():
    tenant_id = current_user.tenant_id
    if not tenant_id:
        flash('Voucher hanya tersedia untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))
    if current_user.role not in ('superadmin', 'admin'):
        flash('Hanya admin yang dapat mengelola voucher.', 'danger')
        return redirect(url_for('members.index'))
    vouchers = Voucher.query.filter_by(tenant_id=tenant_id).order_by(Voucher.created_at.desc()).all()
    return render_template('promotions/vouchers_index.html', vouchers=vouchers)


@members_bp.route('/vouchers/new', methods=['GET', 'POST'])
@members_bp.route('/vouchers/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def voucher_form(id=None):
    tenant_id = current_user.tenant_id
    if not tenant_id:
        flash('Voucher hanya tersedia untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))
    if current_user.role not in ('superadmin', 'admin'):
        flash('Hanya admin yang dapat mengelola voucher.', 'danger')
        return redirect(url_for('members.index'))
    voucher = Voucher.query.filter_by(id=id, tenant_id=tenant_id).first() if id else None
    categories = ProductCategory.query.filter_by(tenant_id=tenant_id).order_by(ProductCategory.nama).all()
    tenant_row = Tenant.query.get(tenant_id)
    tz_id = normalize_timezone_id(getattr(tenant_row, 'timezone', None)) if tenant_row else None
    zi = get_zoneinfo_required(tz_id)

    def _default_voucher_datetimes():
        now_local = datetime.now(zi).replace(second=0, microsecond=0)
        end_local = (now_local + timedelta(days=30)).replace(second=0, microsecond=0)
        return (
            now_local.strftime('%Y-%m-%dT%H:%M'),
            end_local.strftime('%Y-%m-%dT%H:%M'),
        )

    default_start_at, default_end_at = _default_voucher_datetimes()

    def _render_form(voucher_obj, selected_scope_obj):
        return render_template(
            'promotions/voucher_form.html',
            voucher=voucher_obj,
            categories=categories,
            selected_scope=selected_scope_obj,
            default_start_at=default_start_at,
            default_end_at=default_end_at,
        )

    if request.method == 'POST':
        def _parse_float(name, default=0.0, allow_none=False):
            raw = (request.form.get(name) or '').strip()
            if raw == '':
                return None if allow_none else float(default)
            try:
                return float(raw.replace(',', '.'))
            except ValueError:
                raise ValueError(f'Nilai {name} tidak valid.')

        def _parse_int(name, allow_none=False):
            raw = (request.form.get(name) or '').strip()
            if raw == '':
                return None if allow_none else 0
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f'Nilai {name} tidak valid.')

        code = (request.form.get('kode') or '').strip().upper()
        if len(code) < 3:
            flash('Kode voucher minimal 3 karakter.', 'danger')
            return _render_form(voucher, set())
        nama_voucher = (request.form.get('nama') or '').strip()
        if len(nama_voucher) < 3:
            flash('Nama voucher minimal 3 karakter.', 'danger')
            return _render_form(voucher, set())

        if not voucher:
            voucher = Voucher(tenant_id=tenant_id, kode=code, created_by=current_user.id)
            db.session.add(voucher)
        voucher.kode = code
        voucher.nama = nama_voucher
        voucher.deskripsi = (request.form.get('deskripsi') or '').strip()
        voucher.discount_type = (request.form.get('discount_type') or 'fixed').strip()
        try:
            voucher.discount_value = max(0, _parse_float('discount_value', default=0))
            voucher.max_discount = _parse_float('max_discount', allow_none=True)
            voucher.min_spend = max(0, _parse_float('min_spend', default=0))
            voucher.max_usage_global = _parse_int('max_usage_global', allow_none=True)
            voucher.max_usage_per_member = _parse_int('max_usage_per_member', allow_none=True)
        except ValueError as e:
            flash(str(e), 'danger')
            return _render_form(voucher, set())
        if voucher.discount_type == 'percent' and voucher.discount_value > 100:
            flash('Diskon persen maksimal 100.', 'danger')
            return _render_form(voucher, set())
        if voucher.max_usage_global is not None and voucher.max_usage_global < 0:
            flash('Kuota global tidak boleh negatif.', 'danger')
            return _render_form(voucher, set())
        if voucher.max_usage_per_member is not None and voucher.max_usage_per_member < 0:
            flash('Kuota per member tidak boleh negatif.', 'danger')
            return _render_form(voucher, set())
        start_raw = (request.form.get('start_at') or '').strip()
        end_raw = (request.form.get('end_at') or '').strip()
        try:
            start_naive = datetime.strptime(start_raw, '%Y-%m-%dT%H:%M')
            end_naive = datetime.strptime(end_raw, '%Y-%m-%dT%H:%M')
        except ValueError:
            flash('Format tanggal mulai/berakhir tidak valid.', 'danger')
            return _render_form(voucher, set())
        if end_naive <= start_naive:
            flash('Tanggal berakhir harus lebih besar dari tanggal mulai.', 'danger')
            return _render_form(voucher, set())
        voucher.start_at = start_naive.replace(tzinfo=zi).astimezone(dt_timezone.utc).replace(tzinfo=None)
        voucher.end_at = end_naive.replace(tzinfo=zi).astimezone(dt_timezone.utc).replace(tzinfo=None)
        voucher.active = 'active' in request.form
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            flash('Kode voucher sudah digunakan. Gunakan kode lain.', 'danger')
            return redirect(url_for('members.voucher_form', id=id) if id else url_for('members.voucher_form'))

        VoucherCategoryScope.query.filter_by(voucher_id=voucher.id).delete()
        selected = request.form.getlist('category_ids')
        for cid in selected:
            try:
                cid_int = int(cid)
            except (TypeError, ValueError):
                continue
            if ProductCategory.query.filter_by(id=cid_int, tenant_id=tenant_id).first():
                db.session.add(VoucherCategoryScope(voucher_id=voucher.id, category_id=cid_int))
        db.session.commit()
        flash('Voucher tersimpan.', 'success')
        return redirect(url_for('members.vouchers'))

    selected_scope = {x.category_id for x in voucher.category_scopes} if voucher else set()
    return _render_form(voucher, selected_scope)


@members_bp.route('/vouchers/<int:id>/delete', methods=['POST'])
@login_required
def voucher_delete(id):
    tenant_id = current_user.tenant_id
    if current_user.role not in ('superadmin', 'admin'):
        flash('Hanya admin yang dapat menghapus voucher.', 'danger')
        return redirect(url_for('members.vouchers'))
    voucher = Voucher.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    db.session.delete(voucher)
    db.session.commit()
    flash('Voucher dihapus.', 'success')
    return redirect(url_for('members.vouchers'))


@members_bp.route('/vouchers/redemptions')
@login_required
def voucher_redemptions():
    tenant_id = current_user.tenant_id
    rows = (
        VoucherRedemption.query.filter_by(tenant_id=tenant_id)
        .order_by(VoucherRedemption.created_at.desc())
        .limit(300)
        .all()
    )
    return render_template('promotions/redemptions.html', rows=rows)
