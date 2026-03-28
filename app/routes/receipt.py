from flask import Blueprint, render_template, request, make_response, redirect, url_for, flash
from flask_login import login_required, current_user
import io
from sqlalchemy.orm import selectinload
from .. import db
from ..models import Transaction

receipt_bp = Blueprint('receipt', __name__, url_prefix='/receipt')


@receipt_bp.route('/<int:id>')
@login_required
def view_receipt(id):
    tenant_id = current_user.tenant_id
    trx = (
        Transaction.query.options(
            selectinload(Transaction.items),
            selectinload(Transaction.user),
            selectinload(Transaction.member),
            selectinload(Transaction.payments),
            selectinload(Transaction.branch),
            selectinload(Transaction.tenant),
        )
        .filter_by(id=id, tenant_id=tenant_id)
        .first_or_404()
    )
    return render_template('receipt/print.html', trx=trx, auto_print=False)


@receipt_bp.route('/<int:id>/pdf')
@login_required
def download_pdf(id):
    # Native web print behavior
    tenant_id = current_user.tenant_id
    trx = (
        Transaction.query.options(
            selectinload(Transaction.items),
            selectinload(Transaction.user),
            selectinload(Transaction.member),
            selectinload(Transaction.payments),
            selectinload(Transaction.branch),
            selectinload(Transaction.tenant),
        )
        .filter_by(id=id, tenant_id=tenant_id)
        .first_or_404()
    )
    return render_template('receipt/print.html', trx=trx, auto_print=True)

