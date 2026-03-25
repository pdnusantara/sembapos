from datetime import datetime
from flask_login import UserMixin
from sqlalchemy import UniqueConstraint
from . import db, login_manager, bcrypt


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


class TenantPackage(db.Model):
    """Definisi paket langganan: kuota, modul, harga (dikelola Super Admin)."""
    __tablename__ = 'tenant_packages'
    id = db.Column(db.Integer, primary_key=True)
    kode = db.Column(db.String(30), unique=True, nullable=False)
    nama = db.Column(db.String(120), nullable=False)
    deskripsi = db.Column(db.Text)
    max_cabang = db.Column(db.Integer, nullable=False, default=3)
    max_user = db.Column(db.Integer, nullable=False, default=5)
    # JSON array kode modul (permissions.py); NULL/kosong = semua modul diizinkan
    modules_json = db.Column(db.Text, nullable=True)
    harga_bulanan = db.Column(db.Float, nullable=False, default=0)
    harga_tahunan = db.Column(db.Float, nullable=False, default=0)
    aktif = db.Column(db.Boolean, default=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    tenants = db.relationship('Tenant', back_populates='subscription', lazy=True)

    def __repr__(self):
        return f'<TenantPackage {self.kode}>'


class Tenant(db.Model):
    __tablename__ = 'tenants'
    id = db.Column(db.Integer, primary_key=True)
    nama = db.Column(db.String(100), nullable=False)
    kode = db.Column(db.String(20), unique=True, nullable=False)
    alamat = db.Column(db.Text)
    telepon = db.Column(db.String(20))
    email = db.Column(db.String(100))
    paket_id = db.Column(db.Integer, db.ForeignKey('tenant_packages.id'), nullable=True)
    paket = db.Column(db.String(20), default='basic')  # sinkron dengan TenantPackage.kode (legacy / export)
    aktif = db.Column(db.Boolean, default=True)
    tanggal_daftar = db.Column(db.DateTime, default=datetime.utcnow)
    tanggal_expired = db.Column(db.DateTime, nullable=True)
    max_cabang = db.Column(db.Integer, default=3)
    max_user = db.Column(db.Integer, default=5)

    subscription = db.relationship('TenantPackage', back_populates='tenants')
    branches = db.relationship('Branch', backref='tenant', lazy=True, cascade='all, delete-orphan')
    users = db.relationship('User', backref='tenant', lazy=True, cascade='all, delete-orphan')
    products = db.relationship('Product', backref='tenant', lazy=True, cascade='all, delete-orphan')
    categories = db.relationship('ProductCategory', backref='tenant', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Tenant {self.nama}>'


class Branch(db.Model):
    __tablename__ = 'branches'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'kode', name='uq_branch_tenant_kode'),
    )
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    nama = db.Column(db.String(100), nullable=False)
    kode = db.Column(db.String(20), nullable=False)
    alamat = db.Column(db.Text)
    telepon = db.Column(db.String(20))
    aktif = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    users = db.relationship('User', backref='branch', lazy=True)
    transactions = db.relationship('Transaction', backref='branch', lazy=True)

    def __repr__(self):
        return f'<Branch {self.nama}>'


class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=True)  # null for superadmin
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    nama = db.Column(db.String(100), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='kasir')  # superadmin, admin, kasir
    aktif = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    session_version = db.Column(db.Integer, default=0, nullable=False)
    # JSON list modul: lihat app/permissions.py — NULL = default penuh sesuai role
    permissions_json = db.Column(db.Text, nullable=True)

    transactions = db.relationship('Transaction', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)

    @property
    def is_superadmin(self):
        return self.role == 'superadmin'

    @property
    def is_admin(self):
        return self.role in ['superadmin', 'admin']

    def __repr__(self):
        return f'<User {self.username}>'


class UserAuditLog(db.Model):
    __tablename__ = 'user_audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    target_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    actor = db.relationship('User', foreign_keys=[actor_user_id], backref='audit_actions_done', lazy=True)
    target = db.relationship('User', foreign_keys=[target_user_id], backref='audit_actions_target', lazy=True)


class SuperadminAuditLog(db.Model):
    __tablename__ = 'superadmin_audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    action = db.Column(db.String(80), nullable=False)
    target_tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=True)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    actor = db.relationship('User', foreign_keys=[actor_user_id], backref='superadmin_audit_actions', lazy=True)
    target_tenant = db.relationship('Tenant', foreign_keys=[target_tenant_id], backref='superadmin_audits', lazy=True)


class TenantPlanHistory(db.Model):
    """Riwayat perubahan paket / kuota tenant (selaras audit Super Admin)."""
    __tablename__ = 'tenant_plan_history'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    event = db.Column(db.String(40), nullable=False, default='plan_change')
    old_paket_id = db.Column(db.Integer, nullable=True)
    new_paket_id = db.Column(db.Integer, nullable=True)
    old_paket_kode = db.Column(db.String(40), nullable=True)
    new_paket_kode = db.Column(db.String(40), nullable=True)
    old_max_cabang = db.Column(db.Integer, nullable=True)
    new_max_cabang = db.Column(db.Integer, nullable=True)
    old_max_user = db.Column(db.Integer, nullable=True)
    new_max_user = db.Column(db.Integer, nullable=True)

    tenant = db.relationship('Tenant', backref='plan_histories', lazy=True)
    actor = db.relationship('User', foreign_keys=[actor_user_id], backref='tenant_plan_changes', lazy=True)


class ProductCategory(db.Model):
    __tablename__ = 'product_categories'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    nama = db.Column(db.String(100), nullable=False)
    icon = db.Column(db.String(50), default='box')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    products = db.relationship('Product', backref='category', lazy=True)

    def __repr__(self):
        return f'<Category {self.nama}>'


class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('product_categories.id'), nullable=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=True)
    nama = db.Column(db.String(150), nullable=False)
    barcode = db.Column(db.String(50), nullable=True)
    satuan = db.Column(db.String(20), default='pcs')  # pcs, kg, liter, dus, karung
    harga_beli = db.Column(db.Float, default=0)
    harga_jual = db.Column(db.Float, nullable=False)  # ecer
    min_qty_grosir_1 = db.Column(db.Float, nullable=True)
    harga_jual_grosir_1 = db.Column(db.Float, nullable=True)
    min_qty_grosir_2 = db.Column(db.Float, nullable=True)
    harga_jual_grosir_2 = db.Column(db.Float, nullable=True)
    stok = db.Column(db.Float, default=0)
    stok_minimum = db.Column(db.Float, default=5)
    gambar = db.Column(db.String(255), nullable=True)
    aktif = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    transaction_items = db.relationship('TransactionItem', backref='product', lazy=True)
    stock_movements = db.relationship('StockMovement', backref='product', lazy=True)

    @property
    def stok_menipis(self):
        return self.stok <= self.stok_minimum

    def price_tiers(self):
        """Daftar tier harga aktif terurut dari qty kecil ke besar."""
        tiers = [{'min_qty': 1.0, 'harga': float(self.harga_jual or 0), 'label': 'ecer'}]
        if (self.min_qty_grosir_1 or 0) > 1 and (self.harga_jual_grosir_1 or 0) > 0:
            tiers.append({
                'min_qty': float(self.min_qty_grosir_1),
                'harga': float(self.harga_jual_grosir_1),
                'label': 'grosir1',
            })
        if (self.min_qty_grosir_2 or 0) > 1 and (self.harga_jual_grosir_2 or 0) > 0:
            tiers.append({
                'min_qty': float(self.min_qty_grosir_2),
                'harga': float(self.harga_jual_grosir_2),
                'label': 'grosir2',
            })
        return sorted(tiers, key=lambda x: x['min_qty'])

    def price_for_qty(self, qty):
        qty = float(qty or 0)
        picked = {'harga': float(self.harga_jual or 0), 'label': 'ecer', 'min_qty': 1.0}
        for t in self.price_tiers():
            if qty >= float(t['min_qty']):
                picked = t
        return picked

    def __repr__(self):
        return f'<Product {self.nama}>'


class CashierShift(db.Model):
    """Shift kasir: buka/tutup laci, rekonsiliasi tunai."""
    __tablename__ = 'cashier_shifts'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    opened_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=True)
    opening_float = db.Column(db.Float, nullable=False, default=0)
    closing_counted = db.Column(db.Float, nullable=True)
    expected_cash = db.Column(db.Float, nullable=True)
    variance = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='open')  # open, closed
    note_open = db.Column(db.Text, nullable=True)
    note_close = db.Column(db.Text, nullable=True)

    tenant = db.relationship('Tenant', backref='cashier_shifts', lazy=True)
    branch = db.relationship('Branch', backref='cashier_shifts', lazy=True)
    user = db.relationship('User', backref='cashier_shifts_opened', lazy=True, foreign_keys=[user_id])

    def __repr__(self):
        return f'<CashierShift {self.id} {self.status}>'


class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    nomor = db.Column(db.String(50), unique=True, nullable=False)
    subtotal = db.Column(db.Float, default=0)
    diskon = db.Column(db.Float, default=0)
    total = db.Column(db.Float, default=0)
    bayar = db.Column(db.Float, default=0)
    kembalian = db.Column(db.Float, default=0)
    metode_bayar = db.Column(db.String(20), default='tunai')  # tunai, transfer, qris
    catatan = db.Column(db.Text)
    status = db.Column(db.String(20), default='selesai')  # selesai, batal
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # ForeignKey ke Member untuk tracking histori belanja member / hutang
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=True)
    shift_id = db.Column(db.Integer, db.ForeignKey('cashier_shifts.id'), nullable=True)

    tenant = db.relationship('Tenant', backref='transactions', lazy=True)
    shift = db.relationship('CashierShift', backref='transactions', lazy=True)
    items = db.relationship('TransactionItem', backref='transaction', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Transaction {self.nomor}>'


class TransactionItem(db.Model):
    __tablename__ = 'transaction_items'
    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey('transactions.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    nama_produk = db.Column(db.String(150), nullable=False)  # snapshot nama produk
    harga = db.Column(db.Float, nullable=False)
    qty = db.Column(db.Float, nullable=False)
    subtotal = db.Column(db.Float, nullable=False)

    def __repr__(self):
        return f'<TransactionItem {self.nama_produk} x{self.qty}>'


class SalesReturn(db.Model):
    """Nota retur penjualan, terhubung transaksi sumber; stok dikembalikan lewat baris item."""
    __tablename__ = 'sales_returns'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'nomor', name='uq_sales_return_tenant_nomor'),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    shift_id = db.Column(db.Integer, db.ForeignKey('cashier_shifts.id'), nullable=True)
    source_transaction_id = db.Column(db.Integer, db.ForeignKey('transactions.id'), nullable=False)
    replacement_transaction_id = db.Column(db.Integer, db.ForeignKey('transactions.id'), nullable=True)
    nomor = db.Column(db.String(50), nullable=False)
    total_retur = db.Column(db.Float, nullable=False, default=0)
    alasan = db.Column(db.Text, nullable=True)
    catatan = db.Column(db.Text, nullable=True)
    jenis = db.Column(db.String(20), nullable=False, default='retur')  # retur, tukar
    metode_pengembalian = db.Column(db.String(30), nullable=False, default='tunai')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    tenant = db.relationship('Tenant', backref='sales_returns', lazy=True)
    branch = db.relationship('Branch', backref='sales_returns', lazy=True)
    user = db.relationship('User', backref='sales_returns_recorded', lazy=True, foreign_keys=[user_id])
    shift = db.relationship('CashierShift', backref='sales_returns', lazy=True)
    source_transaction = db.relationship(
        'Transaction',
        foreign_keys=[source_transaction_id],
        backref=db.backref('sales_returns', lazy=True),
    )
    replacement_transaction = db.relationship(
        'Transaction',
        foreign_keys=[replacement_transaction_id],
        backref=db.backref('originated_from_sales_returns', lazy=True),
    )
    items = db.relationship('SalesReturnItem', backref='sales_return', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<SalesReturn {self.nomor}>'


class SalesReturnItem(db.Model):
    __tablename__ = 'sales_return_items'
    id = db.Column(db.Integer, primary_key=True)
    return_id = db.Column(db.Integer, db.ForeignKey('sales_returns.id'), nullable=False)
    source_transaction_item_id = db.Column(db.Integer, db.ForeignKey('transaction_items.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty_retur = db.Column(db.Float, nullable=False)
    harga = db.Column(db.Float, nullable=False)
    subtotal = db.Column(db.Float, nullable=False)

    source_line = db.relationship('TransactionItem', backref='return_lines', lazy=True)
    product = db.relationship('Product', backref='sales_return_items', lazy=True)

    def __repr__(self):
        return f'<SalesReturnItem retur x{self.qty_retur}>'


class StockMovement(db.Model):
    __tablename__ = 'stock_movements'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    user = db.relationship('User', backref='stock_movements', lazy=True)
    tipe = db.Column(db.String(10), nullable=False)  # masuk, keluar
    qty = db.Column(db.Float, nullable=False)
    stok_sebelum = db.Column(db.Float, nullable=False)
    stok_sesudah = db.Column(db.Float, nullable=False)
    keterangan = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<StockMovement {self.tipe} {self.qty}>'


# ==========================================
# FITUR BISNIS: SUPPLIER & PURCHASE ORDER
# ==========================================

class Supplier(db.Model):
    __tablename__ = 'suppliers'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    nama = db.Column(db.String(100), nullable=False)
    kontak = db.Column(db.String(100))
    telepon = db.Column(db.String(20))
    alamat = db.Column(db.Text)
    email = db.Column(db.String(100))
    aktif = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    products = db.relationship('Product', backref='supplier', lazy=True)
    purchase_orders = db.relationship('PurchaseOrder', backref='supplier', lazy=True)

    def __repr__(self):
        return f'<Supplier {self.nama}>'


class PurchaseOrder(db.Model):
    __tablename__ = 'purchase_orders'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    nomor = db.Column(db.String(50), unique=True, nullable=False)
    status = db.Column(db.String(20), default='draft')  # draft, dipesan, diterima, batal
    total = db.Column(db.Float, default=0)
    tanggal_pesan = db.Column(db.DateTime, default=datetime.utcnow)
    tanggal_terima = db.Column(db.DateTime, nullable=True)
    catatan = db.Column(db.Text)

    items = db.relationship('PurchaseOrderItem', backref='purchase_order', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<PO {self.nomor}>'


class PurchaseOrderItem(db.Model):
    __tablename__ = 'purchase_order_items'
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey('purchase_orders.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    nama_produk = db.Column(db.String(150), nullable=False)
    harga_beli = db.Column(db.Float, nullable=False)
    qty_pesan = db.Column(db.Float, nullable=False)
    qty_terima = db.Column(db.Float, default=0)
    subtotal = db.Column(db.Float, nullable=False)

    def __repr__(self):
        return f'<POItem {self.nama_produk}>'


# ==========================================
# FITUR BISNIS: PELANGGAN, MEMBER & HUTANG
# ==========================================

class Member(db.Model):
    __tablename__ = 'members'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    nama = db.Column(db.String(100), nullable=False)
    telepon = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(100))
    alamat = db.Column(db.Text)
    poin = db.Column(db.Integer, default=0)
    total_belanja = db.Column(db.Float, default=0)
    total_hutang = db.Column(db.Float, default=0)
    diskon_persen = db.Column(db.Float, default=0)  # Misalnya member VIP dapet diskon 5%
    aktif = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    transactions = db.relationship('Transaction', backref='member', lazy=True)
    debts = db.relationship('Debt', backref='member', lazy=True)

    def __repr__(self):
        return f'<Member {self.nama}>'


class Debt(db.Model):
    __tablename__ = 'debts'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)
    transaction_id = db.Column(db.Integer, db.ForeignKey('transactions.id'), nullable=False)
    jumlah = db.Column(db.Float, nullable=False)
    sisa = db.Column(db.Float, nullable=False)
    keterangan = db.Column(db.String(255))
    jatuh_tempo = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='belum_lunas')  # belum_lunas, lunas
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    payments = db.relationship('DebtPayment', backref='debt', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Debt {self.jumlah}>'


class DebtPayment(db.Model):
    __tablename__ = 'debt_payments'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    debt_id = db.Column(db.Integer, db.ForeignKey('debts.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    jumlah = db.Column(db.Float, nullable=False)
    metode_bayar = db.Column(db.String(20), default='tunai')
    catatan = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    recorder = db.relationship('User', foreign_keys=[user_id], backref='debt_payments_made', lazy=True)

    def __repr__(self):
        return f'<DebtPayment {self.jumlah}>'


# ==========================================
# BIAYA OPERASIONAL
# ==========================================


class OperationalExpenseCategory(db.Model):
    __tablename__ = 'operational_expense_categories'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'nama', name='uq_opex_category_tenant_nama'),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    nama = db.Column(db.String(120), nullable=False)
    deskripsi = db.Column(db.Text)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    aktif = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    expenses = db.relationship('OperationalExpense', backref='category', lazy=True)

    def __repr__(self):
        return f'<OperationalExpenseCategory {self.nama}>'


class OperationalExpense(db.Model):
    __tablename__ = 'operational_expenses'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    category_id = db.Column(
        db.Integer, db.ForeignKey('operational_expense_categories.id'), nullable=False
    )
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    jumlah = db.Column(db.Float, nullable=False)
    tanggal = db.Column(db.DateTime, nullable=False)
    keterangan = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    recorder = db.relationship(
        'User', foreign_keys=[user_id], backref='operational_expenses_recorded', lazy=True
    )
    branch = db.relationship('Branch', backref='operational_expenses', lazy=True)

    def __repr__(self):
        return f'<OperationalExpense {self.jumlah}>'

