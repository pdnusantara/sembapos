from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import func

from .. import db
from ..models import (
    OperationalExpense,
    OperationalExpenseCategory,
    Branch,
)
from ..timezones import local_today_date, resolve_effective_timezone_id

operating_expenses_bp = Blueprint(
    'operating_expenses', __name__, url_prefix='/operating-expenses'
)


def _require_tenant():
    if current_user.tenant_id is None:
        flash('Modul ini hanya untuk akun tenant.', 'warning')
        return redirect(url_for('dashboard.index'))
    return None


def require_admin():
    if current_user.role not in ('superadmin', 'admin'):
        flash('Hanya admin yang dapat mengakses fitur ini.', 'danger')
        return False
    return True


def _parse_date(s, end_of_day=False):
    if not s or not str(s).strip():
        return None
    try:
        d = datetime.strptime(str(s).strip()[:10], '%Y-%m-%d')
        if end_of_day:
            return d.replace(hour=23, minute=59, second=59, microsecond=999999)
        return d.replace(hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        return None


def _default_range():
    end = datetime.utcnow().replace(hour=23, minute=59, second=59, microsecond=999999)
    start = (end - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


def _category_name_exists(tenant_id, nama, exclude_id=None):
    q = OperationalExpenseCategory.query.filter(
        OperationalExpenseCategory.tenant_id == tenant_id,
        func.lower(OperationalExpenseCategory.nama) == func.lower(nama.strip()),
    )
    if exclude_id:
        q = q.filter(OperationalExpenseCategory.id != exclude_id)
    return q.first() is not None


def _can_edit_expense(exp):
    if current_user.role in ('superadmin', 'admin'):
        return True
    if exp.user_id != current_user.id:
        return False
    if current_user.role == 'kasir':
        if not current_user.branch_id:
            return True
        if exp.branch_id is None:
            return False
        return exp.branch_id == current_user.branch_id
    return True


@operating_expenses_bp.route('/')
@login_required
def index():
    redir = _require_tenant()
    if redir:
        return redir

    tenant_id = current_user.tenant_id
    date_from = _parse_date(request.args.get('date_from'))
    date_to = _parse_date(request.args.get('date_to'), end_of_day=True)
    if not date_from or not date_to:
        date_from, date_to = _default_range()

    cat_param = request.args.get('category_id', '').strip()
    branch_param = request.args.get('branch_id', '').strip()

    q = (
        OperationalExpense.query.filter_by(tenant_id=tenant_id)
        .filter(OperationalExpense.tanggal >= date_from)
        .filter(OperationalExpense.tanggal <= date_to)
    )

    if current_user.role == 'kasir' and current_user.branch_id:
        q = q.filter(OperationalExpense.branch_id == current_user.branch_id)

    if cat_param:
        try:
            cid = int(cat_param)
            if OperationalExpenseCategory.query.filter_by(
                id=cid, tenant_id=tenant_id
            ).first():
                q = q.filter(OperationalExpense.category_id == cid)
        except (ValueError, TypeError):
            pass

    if branch_param and current_user.role in ('superadmin', 'admin'):
        try:
            bid = int(branch_param)
            if Branch.query.filter_by(id=bid, tenant_id=tenant_id).first():
                q = q.filter(OperationalExpense.branch_id == bid)
        except (ValueError, TypeError):
            pass

    expenses = q.order_by(OperationalExpense.tanggal.desc(), OperationalExpense.id.desc()).all()
    total_periode = sum(float(e.jumlah or 0) for e in expenses)

    categories = (
        OperationalExpenseCategory.query.filter_by(tenant_id=tenant_id)
        .order_by(OperationalExpenseCategory.sort_order, OperationalExpenseCategory.nama)
        .all()
    )
    branches = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(
        Branch.nama
    ).all()

    return render_template(
        'operating_expenses/index.html',
        expenses=expenses,
        total_periode=total_periode,
        categories=categories,
        branches=branches,
        date_from=date_from.strftime('%Y-%m-%d'),
        date_to=date_to.strftime('%Y-%m-%d'),
        category_id=cat_param,
        branch_id=branch_param,
    )


@operating_expenses_bp.route('/add', methods=['GET', 'POST'])
@login_required
def add():
    redir = _require_tenant()
    if redir:
        return redir

    tenant_id = current_user.tenant_id
    cats = (
        OperationalExpenseCategory.query.filter_by(tenant_id=tenant_id, aktif=True)
        .order_by(OperationalExpenseCategory.sort_order, OperationalExpenseCategory.nama)
        .all()
    )
    branches = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(
        Branch.nama
    ).all()

    if request.method == 'POST':
        if not cats:
            flash('Buat kategori pengeluaran terlebih dahulu.', 'warning')
            return redirect(url_for('operating_expenses.categories'))

        try:
            category_id = int(request.form.get('category_id', 0))
        except (ValueError, TypeError):
            category_id = 0
        cat = OperationalExpenseCategory.query.filter_by(
            id=category_id, tenant_id=tenant_id, aktif=True
        ).first()
        if not cat:
            flash('Kategori tidak valid.', 'danger')
            return redirect(url_for('operating_expenses.add'))

        try:
            jumlah = round(float(request.form.get('jumlah', 0) or 0))
        except (TypeError, ValueError):
            jumlah = 0
        if jumlah <= 0:
            flash('Jumlah harus lebih dari 0.', 'danger')
            return redirect(url_for('operating_expenses.add'))

        tanggal_s = (request.form.get('tanggal') or '').strip()
        try:
            tgl = datetime.strptime(tanggal_s[:10], '%Y-%m-%d')
            tanggal = tgl.replace(hour=12, minute=0, second=0, microsecond=0)
        except ValueError:
            flash('Tanggal tidak valid.', 'danger')
            return redirect(url_for('operating_expenses.add'))

        keterangan = (request.form.get('keterangan') or '').strip() or None

        branch_id = None
        if current_user.role in ('superadmin', 'admin'):
            bid = (request.form.get('branch_id') or '').strip()
            if bid:
                try:
                    b = int(bid)
                    if Branch.query.filter_by(id=b, tenant_id=tenant_id).first():
                        branch_id = b
                except (ValueError, TypeError):
                    pass
        else:
            branch_id = current_user.branch_id

        exp = OperationalExpense(
            tenant_id=tenant_id,
            category_id=cat.id,
            user_id=current_user.id,
            branch_id=branch_id,
            jumlah=float(jumlah),
            tanggal=tanggal,
            keterangan=keterangan,
        )
        db.session.add(exp)
        db.session.commit()
        flash('Biaya operasional tercatat.', 'success')
        return redirect(url_for('operating_expenses.index'))

    return render_template(
        'operating_expenses/expense_form.html',
        expense=None,
        categories=cats,
        branches=branches,
        action='Tambah',
        today_str=local_today_date(resolve_effective_timezone_id(current_user)).isoformat(),
    )


@operating_expenses_bp.route('/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit(id):
    redir = _require_tenant()
    if redir:
        return redir

    tenant_id = current_user.tenant_id
    exp = OperationalExpense.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    if not _can_edit_expense(exp):
        flash('Anda tidak dapat mengubah entri ini.', 'danger')
        return redirect(url_for('operating_expenses.index'))

    cats = (
        OperationalExpenseCategory.query.filter_by(tenant_id=tenant_id, aktif=True)
        .order_by(OperationalExpenseCategory.sort_order, OperationalExpenseCategory.nama)
        .all()
    )
    branches = Branch.query.filter_by(tenant_id=tenant_id, aktif=True).order_by(
        Branch.nama
    ).all()

    if request.method == 'POST':
        try:
            category_id = int(request.form.get('category_id', 0))
        except (ValueError, TypeError):
            category_id = 0
        cat = OperationalExpenseCategory.query.filter_by(
            id=category_id, tenant_id=tenant_id, aktif=True
        ).first()
        if not cat:
            flash('Kategori tidak valid.', 'danger')
            return redirect(url_for('operating_expenses.edit', id=id))

        try:
            jumlah = round(float(request.form.get('jumlah', 0) or 0))
        except (TypeError, ValueError):
            jumlah = 0
        if jumlah <= 0:
            flash('Jumlah harus lebih dari 0.', 'danger')
            return redirect(url_for('operating_expenses.edit', id=id))

        tanggal_s = (request.form.get('tanggal') or '').strip()
        try:
            tgl = datetime.strptime(tanggal_s[:10], '%Y-%m-%d')
            tanggal = tgl.replace(hour=12, minute=0, second=0, microsecond=0)
        except ValueError:
            flash('Tanggal tidak valid.', 'danger')
            return redirect(url_for('operating_expenses.edit', id=id))

        keterangan = (request.form.get('keterangan') or '').strip() or None

        exp.category_id = cat.id
        exp.jumlah = float(jumlah)
        exp.tanggal = tanggal
        exp.keterangan = keterangan

        if current_user.role in ('superadmin', 'admin'):
            bid = (request.form.get('branch_id') or '').strip()
            if bid:
                try:
                    b = int(bid)
                    if Branch.query.filter_by(id=b, tenant_id=tenant_id).first():
                        exp.branch_id = b
                except (ValueError, TypeError):
                    pass
            else:
                exp.branch_id = None

        db.session.commit()
        flash('Data diperbarui.', 'success')
        return redirect(url_for('operating_expenses.index'))

    return render_template(
        'operating_expenses/expense_form.html',
        expense=exp,
        categories=cats,
        branches=branches,
        action='Edit',
        today_str=local_today_date(resolve_effective_timezone_id(current_user)).isoformat(),
    )


@operating_expenses_bp.route('/delete/<int:id>', methods=['POST'])
@login_required
def delete(id):
    redir = _require_tenant()
    if redir:
        return redir

    if not require_admin():
        return redirect(url_for('operating_expenses.index'))

    tenant_id = current_user.tenant_id
    exp = OperationalExpense.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    db.session.delete(exp)
    db.session.commit()
    flash('Pengeluaran dihapus.', 'success')
    return redirect(url_for('operating_expenses.index'))


@operating_expenses_bp.route('/categories')
@login_required
def categories():
    redir = _require_tenant()
    if redir:
        return redir

    if not require_admin():
        return redirect(url_for('operating_expenses.index'))

    tenant_id = current_user.tenant_id
    cats = (
        OperationalExpenseCategory.query.filter_by(tenant_id=tenant_id)
        .order_by(OperationalExpenseCategory.sort_order, OperationalExpenseCategory.nama)
        .all()
    )
    counts = dict(
        db.session.query(
            OperationalExpense.category_id,
            func.count(OperationalExpense.id),
        )
        .filter(OperationalExpense.tenant_id == tenant_id)
        .group_by(OperationalExpense.category_id)
        .all()
    )
    return render_template(
        'operating_expenses/categories.html',
        categories=cats,
        expense_counts=counts,
    )


@operating_expenses_bp.route('/categories/add', methods=['POST'])
@login_required
def add_category():
    redir = _require_tenant()
    if redir:
        return redir

    if not require_admin():
        return redirect(url_for('operating_expenses.categories'))

    tenant_id = current_user.tenant_id
    nama = (request.form.get('nama') or '').strip()
    if not nama:
        flash('Nama kategori wajib diisi.', 'danger')
        return redirect(url_for('operating_expenses.categories'))

    if _category_name_exists(tenant_id, nama):
        flash('Kategori dengan nama serupa sudah ada.', 'danger')
        return redirect(url_for('operating_expenses.categories'))

    deskripsi = (request.form.get('deskripsi') or '').strip() or None
    try:
        sort_order = int(request.form.get('sort_order', 0) or 0)
    except (ValueError, TypeError):
        sort_order = 0

    cat = OperationalExpenseCategory(
        tenant_id=tenant_id,
        nama=nama[:120],
        deskripsi=deskripsi,
        sort_order=sort_order,
        aktif=True,
    )
    db.session.add(cat)
    db.session.commit()
    flash(f'Kategori "{cat.nama}" disimpan.', 'success')
    return redirect(url_for('operating_expenses.categories'))


@operating_expenses_bp.route('/categories/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_category(id):
    redir = _require_tenant()
    if redir:
        return redir

    if not require_admin():
        return redirect(url_for('operating_expenses.categories'))

    tenant_id = current_user.tenant_id
    cat = OperationalExpenseCategory.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()

    if request.method == 'POST':
        nama = (request.form.get('nama') or '').strip()
        if not nama:
            flash('Nama kategori wajib diisi.', 'danger')
            return redirect(url_for('operating_expenses.edit_category', id=id))

        if _category_name_exists(tenant_id, nama, exclude_id=cat.id):
            flash('Kategori dengan nama serupa sudah ada.', 'danger')
            return redirect(url_for('operating_expenses.edit_category', id=id))

        cat.nama = nama[:120]
        cat.deskripsi = (request.form.get('deskripsi') or '').strip() or None
        try:
            cat.sort_order = int(request.form.get('sort_order', 0) or 0)
        except (ValueError, TypeError):
            cat.sort_order = 0
        db.session.commit()
        flash('Kategori diperbarui.', 'success')
        return redirect(url_for('operating_expenses.categories'))

    return render_template('operating_expenses/category_form.html', category=cat)


@operating_expenses_bp.route('/categories/toggle/<int:id>', methods=['POST'])
@login_required
def toggle_category(id):
    redir = _require_tenant()
    if redir:
        return redir

    if not require_admin():
        return redirect(url_for('operating_expenses.categories'))

    tenant_id = current_user.tenant_id
    cat = OperationalExpenseCategory.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    cat.aktif = not cat.aktif
    db.session.commit()
    flash(
        f'Kategori "{cat.nama}" {"diaktifkan" if cat.aktif else "dinonaktifkan"}.',
        'success',
    )
    return redirect(url_for('operating_expenses.categories'))


@operating_expenses_bp.route('/categories/delete/<int:id>', methods=['POST'])
@login_required
def delete_category(id):
    redir = _require_tenant()
    if redir:
        return redir

    if not require_admin():
        return redirect(url_for('operating_expenses.categories'))

    tenant_id = current_user.tenant_id
    cat = OperationalExpenseCategory.query.filter_by(id=id, tenant_id=tenant_id).first_or_404()
    n = OperationalExpense.query.filter_by(category_id=cat.id).count()
    if n > 0:
        flash(
            'Tidak dapat menghapus: masih ada pengeluaran yang memakai kategori ini. Nonaktifkan saja.',
            'danger',
        )
        return redirect(url_for('operating_expenses.categories'))

    nama = cat.nama
    db.session.delete(cat)
    db.session.commit()
    flash(f'Kategori "{nama}" dihapus.', 'success')
    return redirect(url_for('operating_expenses.categories'))
