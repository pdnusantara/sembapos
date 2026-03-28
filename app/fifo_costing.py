from datetime import datetime

from . import db
from .models import InventoryCostLayer, InventoryCostLayerUsage, ProductAuditLog


EPSILON = 1e-9


def create_cost_layer(*, tenant_id, product_id, qty_in, unit_cost, source_type='po_receive', source_id=None, received_at=None):
    qty_in = float(qty_in or 0)
    unit_cost = float(unit_cost or 0)
    if qty_in <= 0:
        return None
    layer = InventoryCostLayer(
        tenant_id=tenant_id,
        product_id=product_id,
        source_type=(source_type or 'po_receive'),
        source_id=source_id,
        received_at=received_at or datetime.utcnow(),
        qty_in=qty_in,
        qty_remaining=qty_in,
        unit_cost=unit_cost,
    )
    db.session.add(layer)
    return layer


def consume_fifo_cost(*, tenant_id, product, transaction_item_id, qty_needed, actor_user_id=None):
    qty_needed = float(qty_needed or 0)
    if qty_needed <= 0:
        return {'total_cost': 0.0, 'fallback_qty': 0.0}

    remaining = qty_needed
    total_cost = 0.0
    fallback_qty = 0.0

    layers = (
        InventoryCostLayer.query.filter(
            InventoryCostLayer.tenant_id == tenant_id,
            InventoryCostLayer.product_id == product.id,
            InventoryCostLayer.qty_remaining > EPSILON,
        )
        .order_by(InventoryCostLayer.received_at.asc(), InventoryCostLayer.id.asc())
        .with_for_update()
        .all()
    )

    for layer in layers:
        if remaining <= EPSILON:
            break
        can_use = min(float(layer.qty_remaining or 0), remaining)
        if can_use <= EPSILON:
            continue
        layer.qty_remaining = max(0.0, float(layer.qty_remaining or 0) - can_use)
        subtotal_cost = can_use * float(layer.unit_cost or 0)
        total_cost += subtotal_cost
        remaining -= can_use
        db.session.add(InventoryCostLayerUsage(
            tenant_id=tenant_id,
            layer_id=layer.id,
            transaction_item_id=transaction_item_id,
            qty_used=can_use,
            unit_cost=float(layer.unit_cost or 0),
            subtotal_cost=subtotal_cost,
        ))

    if remaining > EPSILON:
        fallback_qty = remaining
        fallback_unit_cost = float(product.harga_beli or 0)
        fallback_cost = fallback_qty * fallback_unit_cost
        total_cost += fallback_cost
        if actor_user_id:
            db.session.add(ProductAuditLog(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                product_id=product.id,
                action='fifo_fallback_cost',
                old_harga_jual=None,
                new_harga_jual=None,
                old_stok_minimum=None,
                new_stok_minimum=None,
                detail=(
                    f'FIFO layer kurang saat checkout. qty_needed={qty_needed:.4f}, '
                    f'fallback_qty={fallback_qty:.4f}, fallback_unit_cost={fallback_unit_cost:.2f}'
                ),
            ))

    return {'total_cost': total_cost, 'fallback_qty': fallback_qty}


def restore_fifo_from_transaction_item(*, tenant_id, source_transaction_item, qty_return, actor_user_id=None):
    """
    Kembalikan qty retur ke layer asal transaksi (proporsional FIFO usage).
    Fallback: jika tidak ada usage lama, buat layer retur baru berdasar snapshot/unit cost.
    """
    qty_return = float(qty_return or 0)
    sold_qty = float(source_transaction_item.qty or 0)
    if qty_return <= 0 or sold_qty <= 0:
        return {'restored_qty': 0.0, 'fallback_layer_qty': 0.0}

    usages = (
        InventoryCostLayerUsage.query.filter_by(
            tenant_id=tenant_id,
            transaction_item_id=source_transaction_item.id,
        )
        .order_by(InventoryCostLayerUsage.id.asc())
        .all()
    )

    restored = 0.0
    if usages:
        remaining = qty_return
        for u in usages:
            if remaining <= EPSILON:
                break
            sold_from_usage = float(u.qty_used or 0)
            if sold_from_usage <= EPSILON:
                continue
            max_restore_here = sold_from_usage * (qty_return / sold_qty)
            restore_qty = min(max_restore_here, remaining)
            if restore_qty <= EPSILON:
                continue
            layer = InventoryCostLayer.query.filter_by(id=u.layer_id, tenant_id=tenant_id).with_for_update().first()
            if not layer:
                continue
            layer.qty_remaining = float(layer.qty_remaining or 0) + restore_qty
            remaining -= restore_qty
            restored += restore_qty

    fallback_layer_qty = max(0.0, qty_return - restored)
    if fallback_layer_qty > EPSILON:
        if getattr(source_transaction_item, 'hpp_snapshot', None) is not None:
            unit_cost = float(source_transaction_item.hpp_snapshot or 0)
        elif getattr(source_transaction_item, 'harga_beli_snapshot', None) is not None:
            unit_cost = float(source_transaction_item.harga_beli_snapshot or 0)
        elif sold_qty > 0 and getattr(source_transaction_item, 'modal_snapshot', None) is not None:
            unit_cost = float(source_transaction_item.modal_snapshot or 0) / sold_qty
        else:
            unit_cost = 0.0
        create_cost_layer(
            tenant_id=tenant_id,
            product_id=source_transaction_item.product_id,
            qty_in=fallback_layer_qty,
            unit_cost=unit_cost,
            source_type='sales_return',
            source_id=source_transaction_item.id,
            received_at=datetime.utcnow(),
        )
        if actor_user_id:
            db.session.add(ProductAuditLog(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                product_id=source_transaction_item.product_id,
                action='fifo_return_fallback_layer',
                detail=(
                    f'Retur tanpa usage layer lengkap. '
                    f'ti_id={source_transaction_item.id}, qty_return={qty_return:.4f}, '
                    f'fallback_layer_qty={fallback_layer_qty:.4f}, unit_cost={unit_cost:.2f}'
                ),
            ))
    return {'restored_qty': restored, 'fallback_layer_qty': fallback_layer_qty}


def consume_fifo_stock_out(*, tenant_id, product, qty_needed, actor_user_id=None, reason='manual_stock_out'):
    """Kurangi layer FIFO untuk pengeluaran stok non-penjualan (koreksi/penyesuaian)."""
    qty_needed = float(qty_needed or 0)
    if qty_needed <= 0:
        return {'depleted_qty': 0.0, 'fallback_qty': 0.0}

    remaining = qty_needed
    depleted = 0.0
    layers = (
        InventoryCostLayer.query.filter(
            InventoryCostLayer.tenant_id == tenant_id,
            InventoryCostLayer.product_id == product.id,
            InventoryCostLayer.qty_remaining > EPSILON,
        )
        .order_by(InventoryCostLayer.received_at.asc(), InventoryCostLayer.id.asc())
        .with_for_update()
        .all()
    )
    for layer in layers:
        if remaining <= EPSILON:
            break
        can_use = min(float(layer.qty_remaining or 0), remaining)
        if can_use <= EPSILON:
            continue
        layer.qty_remaining = max(0.0, float(layer.qty_remaining or 0) - can_use)
        remaining -= can_use
        depleted += can_use

    fallback_qty = max(0.0, remaining)
    if fallback_qty > EPSILON and actor_user_id:
        db.session.add(ProductAuditLog(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            product_id=product.id,
            action='fifo_stock_out_fallback',
            detail=(
                f'Pengeluaran stok non-penjualan tanpa layer cukup. '
                f'reason={reason}, qty_needed={qty_needed:.4f}, fallback_qty={fallback_qty:.4f}'
            ),
        ))
    return {'depleted_qty': depleted, 'fallback_qty': fallback_qty}
