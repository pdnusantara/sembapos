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
    provinsi = db.Column(db.String(100), nullable=True)
    kab_kota = db.Column(db.String(100), nullable=True)
    kecamatan = db.Column(db.String(100), nullable=True)
    desa = db.Column(db.String(100), nullable=True)
    telepon = db.Column(db.String(20))
    email = db.Column(db.String(100))
    paket_id = db.Column(db.Integer, db.ForeignKey('tenant_packages.id'), nullable=True)
    paket = db.Column(db.String(20), default='basic')  # sinkron dengan TenantPackage.kode (legacy / export)
    aktif = db.Column(db.Boolean, default=True)
    tanggal_daftar = db.Column(db.DateTime, default=datetime.utcnow)
    tanggal_expired = db.Column(db.DateTime, nullable=True)
    max_cabang = db.Column(db.Integer, default=3)
    max_user = db.Column(db.Integer, default=5)
    # IANA timezone, e.g. Asia/Jakarta (WIB); dipakai dashboard & laporan per hari kalender
    timezone = db.Column(db.String(30), nullable=False, default='Asia/Jakarta')
    # Path relatif dari static/, e.g. uploads/tenants/<id>/logo.png
    logo = db.Column(db.String(255), nullable=True)

    subscription = db.relationship('TenantPackage', back_populates='tenants')
    branches = db.relationship('Branch', backref='tenant', lazy=True, cascade='all, delete-orphan')
    users = db.relationship('User', backref='tenant', lazy=True, cascade='all, delete-orphan')
    products = db.relationship('Product', backref='tenant', lazy=True, cascade='all, delete-orphan')
    categories = db.relationship('ProductCategory', backref='tenant', lazy=True, cascade='all, delete-orphan')
    etalases = db.relationship('Etalase', backref='tenant', lazy=True, cascade='all, delete-orphan')

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
    # Zona waktu pribadi superadmin (user tenant memakai tenant.timezone)
    timezone = db.Column(db.String(30), nullable=True)

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


class Etalase(db.Model):
    """Rak / pajangan fisik di toko (lokasi produk untuk kasir)."""
    __tablename__ = 'etalases'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    nama = db.Column(db.String(100), nullable=False)
    keterangan = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<Etalase {self.nama}>'


class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('product_categories.id'), nullable=True)
    etalase_id = db.Column(db.Integer, db.ForeignKey('etalases.id'), nullable=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=True)
    nama = db.Column(db.String(150), nullable=False)
    barcode = db.Column(db.String(50), nullable=True)
    satuan = db.Column(db.String(20), default='pcs')  # pcs, kg, liter, dus, karung
    harga_beli = db.Column(db.Float, default=0)
    harga_jual = db.Column(db.Float, nullable=False)  # ecer
    harga_coret = db.Column(db.Float, nullable=True)  # harga normal untuk label (dicoret)
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

    etalase = db.relationship('Etalase', backref='products', lazy=True)
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
    promo_code = db.Column(db.String(50), nullable=True)
    promo_type = db.Column(db.String(20), nullable=True)  # voucher_fixed, voucher_percent, tier_percent
    promo_name = db.Column(db.String(150), nullable=True)
    promo_discount = db.Column(db.Float, default=0, nullable=False)
    promo_payload = db.Column(db.Text, nullable=True)  # JSON string snapshot audit promo

    tenant = db.relationship('Tenant', backref='transactions', lazy=True)
    shift = db.relationship('CashierShift', backref='transactions', lazy=True)
    items = db.relationship('TransactionItem', backref='transaction', lazy=True, cascade='all, delete-orphan')
    payments = db.relationship('TransactionPayment', backref='transaction', lazy=True, cascade='all, delete-orphan')

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
    diskon = db.Column(db.Float, nullable=False, default=0)  # potongan rupiah per baris (POS)
    subtotal = db.Column(db.Float, nullable=False)
    hpp_snapshot = db.Column(db.Float, nullable=True)   # unit cost saat transaksi
    modal_snapshot = db.Column(db.Float, nullable=True)  # total modal baris saat transaksi

    def __repr__(self):
        return f'<TransactionItem {self.nama_produk} x{self.qty}>'


class TransactionPayment(db.Model):
    __tablename__ = 'transaction_payments'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    transaction_id = db.Column(db.Integer, db.ForeignKey('transactions.id'), nullable=False)
    method = db.Column(db.String(20), nullable=False)  # tunai, transfer, qris, kredit
    amount = db.Column(db.Float, nullable=False, default=0)
    note = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    tenant = db.relationship('Tenant', backref='transaction_payments', lazy=True)

    def __repr__(self):
        return f'<TransactionPayment {self.transaction_id}:{self.method}:{self.amount}>'


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


class StockOpnameSession(db.Model):
    __tablename__ = 'stock_opname_sessions'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    kode = db.Column(db.String(40), nullable=False, unique=True)
    judul = db.Column(db.String(160), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='draft')  # draft, review, approved, rejected
    catatan = db.Column(db.Text, nullable=True)

    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    submitted_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    rejected_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    submitted_at = db.Column(db.DateTime, nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    rejected_at = db.Column(db.DateTime, nullable=True)
    finalized_at = db.Column(db.DateTime, nullable=True)

    tenant = db.relationship('Tenant', backref='stock_opname_sessions', lazy=True)
    branch = db.relationship('Branch', backref='stock_opname_sessions', lazy=True)
    creator = db.relationship('User', foreign_keys=[created_by], backref='stock_opname_created', lazy=True)
    submitter = db.relationship('User', foreign_keys=[submitted_by], backref='stock_opname_submitted', lazy=True)
    reviewer = db.relationship('User', foreign_keys=[reviewed_by], backref='stock_opname_reviewed', lazy=True)
    approver = db.relationship('User', foreign_keys=[approved_by], backref='stock_opname_approved', lazy=True)
    rejector = db.relationship('User', foreign_keys=[rejected_by], backref='stock_opname_rejected', lazy=True)

    def __repr__(self):
        return f'<StockOpnameSession {self.kode} {self.status}>'


class StockOpnameItem(db.Model):
    __tablename__ = 'stock_opname_items'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('stock_opname_sessions.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    system_stock = db.Column(db.Float, nullable=False)
    physical_stock = db.Column(db.Float, nullable=True)
    selisih = db.Column(db.Float, nullable=False, default=0)
    alasan = db.Column(db.String(255), nullable=True)
    catatan = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    session = db.relationship('StockOpnameSession', backref=db.backref('items', lazy=True, cascade='all, delete-orphan'))
    product = db.relationship('Product', backref='stock_opname_items', lazy=True)
    creator = db.relationship('User', foreign_keys=[created_by], backref='stock_opname_items_created', lazy=True)
    updater = db.relationship('User', foreign_keys=[updated_by], backref='stock_opname_items_updated', lazy=True)

    __table_args__ = (
        db.UniqueConstraint('session_id', 'product_id', name='uq_stock_opname_items_session_product'),
    )

    def __repr__(self):
        return f'<StockOpnameItem s#{self.session_id} p#{self.product_id}>'


class StockOpnameApprovalLog(db.Model):
    __tablename__ = 'stock_opname_approval_logs'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('stock_opname_sessions.id'), nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    action = db.Column(db.String(40), nullable=False)  # create, add_item, submit_review, approve, reject
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    session = db.relationship('StockOpnameSession', backref=db.backref('approval_logs', lazy=True, cascade='all, delete-orphan'))
    actor = db.relationship('User', foreign_keys=[actor_user_id], backref='stock_opname_approval_logs', lazy=True)

    def __repr__(self):
        return f'<StockOpnameApprovalLog {self.action} s#{self.session_id}>'


class ProductAuditLog(db.Model):
    __tablename__ = 'product_audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    old_harga_jual = db.Column(db.Float, nullable=True)
    new_harga_jual = db.Column(db.Float, nullable=True)
    old_stok_minimum = db.Column(db.Float, nullable=True)
    new_stok_minimum = db.Column(db.Float, nullable=True)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    tenant = db.relationship('Tenant', backref='product_audit_logs', lazy=True)
    actor = db.relationship('User', foreign_keys=[actor_user_id], backref='product_audit_actions', lazy=True)
    product = db.relationship('Product', backref='audit_logs', lazy=True)

    def __repr__(self):
        return f'<ProductAuditLog {self.action} p#{self.product_id}>'


class InventoryCostLayer(db.Model):
    __tablename__ = 'inventory_cost_layers'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    source_type = db.Column(db.String(30), nullable=False, default='po_receive')  # po_receive, manual, opening
    source_id = db.Column(db.Integer, nullable=True)
    received_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    qty_in = db.Column(db.Float, nullable=False)
    qty_remaining = db.Column(db.Float, nullable=False)
    unit_cost = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    tenant = db.relationship('Tenant', backref='inventory_cost_layers', lazy=True)
    product = db.relationship('Product', backref='cost_layers', lazy=True)

    def __repr__(self):
        return f'<InventoryCostLayer p#{self.product_id} rem={self.qty_remaining}>'


class InventoryCostLayerUsage(db.Model):
    __tablename__ = 'inventory_cost_layer_usages'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    layer_id = db.Column(db.Integer, db.ForeignKey('inventory_cost_layers.id'), nullable=False)
    transaction_item_id = db.Column(db.Integer, db.ForeignKey('transaction_items.id'), nullable=False)
    qty_used = db.Column(db.Float, nullable=False)
    unit_cost = db.Column(db.Float, nullable=False)
    subtotal_cost = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    tenant = db.relationship('Tenant', backref='inventory_cost_layer_usages', lazy=True)
    layer = db.relationship('InventoryCostLayer', backref='usages', lazy=True)
    transaction_item = db.relationship('TransactionItem', backref='cost_layer_usages', lazy=True)

    def __repr__(self):
        return f'<InventoryCostLayerUsage ti#{self.transaction_item_id} qty={self.qty_used}>'


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
    __table_args__ = (
        UniqueConstraint('tenant_id', 'telepon', name='uq_member_tenant_phone'),
    )
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
    tier_id = db.Column(db.Integer, db.ForeignKey('member_tiers.id'), nullable=True)
    tier_evaluated_at = db.Column(db.DateTime, nullable=True)
    rolling_spend = db.Column(db.Float, default=0, nullable=False)
    rolling_tx_count = db.Column(db.Integer, default=0, nullable=False)
    rolling_last_days = db.Column(db.Integer, default=365, nullable=False)
    last_transaction_at = db.Column(db.DateTime, nullable=True)
    aktif = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    transactions = db.relationship('Transaction', backref='member', lazy=True)
    debts = db.relationship('Debt', backref='member', lazy=True)
    tier = db.relationship('MemberTier', backref='members', lazy=True)

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


class MemberTier(db.Model):
    __tablename__ = 'member_tiers'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'kode', name='uq_member_tier_tenant_kode'),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    kode = db.Column(db.String(20), nullable=False)  # silver/gold/platinum/custom
    nama = db.Column(db.String(80), nullable=False)
    min_spend = db.Column(db.Float, default=0, nullable=False)
    benefit_discount_pct = db.Column(db.Float, default=0, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    aktif = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    tenant = db.relationship('Tenant', backref='member_tiers', lazy=True)

    def __repr__(self):
        return f'<MemberTier {self.tenant_id}:{self.kode}>'


class PosLinePromoRule(db.Model):
    """Promo otomatis per baris di kasir: diskon nominal/persen untuk produk atau kategori, terjadwal."""
    __tablename__ = 'pos_line_promo_rules'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    nama = db.Column(db.String(120), nullable=False)
    scope = db.Column(db.String(20), nullable=False)  # product | category
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    category_id = db.Column(db.Integer, db.ForeignKey('product_categories.id'), nullable=True)
    discount_type = db.Column(db.String(20), nullable=False, default='percent')  # percent | fixed
    discount_value = db.Column(db.Float, nullable=False, default=0)
    max_discount = db.Column(db.Float, nullable=True)
    min_qty = db.Column(db.Float, nullable=False, default=1)
    priority = db.Column(db.Integer, nullable=False, default=0)
    start_at = db.Column(db.DateTime, nullable=False)
    end_at = db.Column(db.DateTime, nullable=False)
    aktif = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    tenant = db.relationship('Tenant', backref='pos_line_promo_rules', lazy=True)
    product = db.relationship('Product', foreign_keys=[product_id], lazy=True)
    category = db.relationship('ProductCategory', foreign_keys=[category_id], lazy=True)

    def __repr__(self):
        return f'<PosLinePromoRule {self.tenant_id}:{self.nama}>'


class Voucher(db.Model):
    __tablename__ = 'vouchers'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'kode', name='uq_voucher_tenant_kode'),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    kode = db.Column(db.String(50), nullable=False)
    nama = db.Column(db.String(120), nullable=False)
    deskripsi = db.Column(db.Text, nullable=True)
    discount_type = db.Column(db.String(20), nullable=False, default='fixed')  # fixed, percent
    discount_value = db.Column(db.Float, nullable=False, default=0)
    max_discount = db.Column(db.Float, nullable=True)
    min_spend = db.Column(db.Float, nullable=False, default=0)
    start_at = db.Column(db.DateTime, nullable=False)
    end_at = db.Column(db.DateTime, nullable=False)
    max_usage_global = db.Column(db.Integer, nullable=True)
    max_usage_per_member = db.Column(db.Integer, nullable=True, default=1)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    tenant = db.relationship('Tenant', backref='vouchers', lazy=True)
    creator = db.relationship('User', backref='vouchers_created', lazy=True)

    def __repr__(self):
        return f'<Voucher {self.tenant_id}:{self.kode}>'


class VoucherCategoryScope(db.Model):
    __tablename__ = 'voucher_category_scopes'
    __table_args__ = (
        UniqueConstraint('voucher_id', 'category_id', name='uq_voucher_category_scope'),
    )

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey('vouchers.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('product_categories.id'), nullable=False)

    voucher = db.relationship('Voucher', backref='category_scopes', lazy=True)
    category = db.relationship('ProductCategory', backref='voucher_scopes', lazy=True)


class VoucherRedemption(db.Model):
    __tablename__ = 'voucher_redemptions'
    __table_args__ = (
        UniqueConstraint('voucher_id', 'transaction_id', name='uq_voucher_redemption_tx'),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    voucher_id = db.Column(db.Integer, db.ForeignKey('vouchers.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey('transactions.id'), nullable=False)
    discount_amount = db.Column(db.Float, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    tenant = db.relationship('Tenant', backref='voucher_redemptions', lazy=True)
    voucher = db.relationship('Voucher', backref='redemptions', lazy=True)
    member = db.relationship('Member', backref='voucher_redemptions', lazy=True)
    transaction = db.relationship('Transaction', backref='voucher_redemption', lazy=True)


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


# ==========================================
# MARKETPLACE
# ==========================================

# ==========================================
# SUPERADMIN: LEAD CAPTURE & APP SETTINGS
# ==========================================

class LeadCapture(db.Model):
    __tablename__ = 'lead_captures'

    id = db.Column(db.Integer, primary_key=True)
    nama = db.Column(db.String(120), nullable=False)
    no_wa = db.Column(db.String(30), nullable=False, default='')
    jenis_usaha = db.Column(db.String(120), nullable=False, default='')
    catatan = db.Column(db.Text)
    source = db.Column(db.String(40), nullable=False, default='landing')
    status = db.Column(db.String(30), nullable=False, default='new')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    trial_tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=True)
    trial_username = db.Column(db.String(80), nullable=True)
    trial_password = db.Column(db.String(120), nullable=True)
    trial_expired_at = db.Column(db.DateTime, nullable=True)
    trial_created_at = db.Column(db.DateTime, nullable=True)
    provinsi = db.Column(db.String(100), nullable=True)
    kabupaten = db.Column(db.String(100), nullable=True)
    kecamatan = db.Column(db.String(100), nullable=True)
    desa = db.Column(db.String(100), nullable=True)
    catatan_admin = db.Column(db.Text, nullable=True)

    trial_tenant = db.relationship('Tenant', backref='lead_source', lazy=True)

    def __repr__(self):
        return f'<LeadCapture {self.nama}>'


class AppSetting(db.Model):
    __tablename__ = 'app_settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @staticmethod
    def get(key, default=None):
        row = AppSetting.query.filter_by(key=key).first()
        return row.value if row else default

    @staticmethod
    def set(key, value):
        row = AppSetting.query.filter_by(key=key).first()
        if row:
            row.value = str(value) if value is not None else None
        else:
            row = AppSetting(key=key, value=str(value) if value is not None else None)
            db.session.add(row)
        return row

    def __repr__(self):
        return f'<AppSetting {self.key}>'


# ==========================================
# SUPERADMIN: TUTORIAL CONTENT (DYNAMIC)
# ==========================================
class TutorialPageConfig(db.Model):
    """
    Simpan konten halaman tutorial dalam bentuk structured JSON (Text).

    data_json berisi blok HTML yang akan dirender kembali di `tutorial_dynamic.html`.
    Super Admin dapat mengedit blok-blok ini melalui halaman editor.
    """
    __tablename__ = 'tutorial_page_configs'

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), nullable=False, unique=True)  # mis. "tutorial_main"
    schema_version = db.Column(db.Integer, nullable=False, default=1)

    # JSON string (Text) berisi block HTML & structured metadata
    data_json = db.Column(db.Text, nullable=False, default='{}')

    aktif = db.Column(db.Boolean, default=True, nullable=False)

    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    updater = db.relationship('User', foreign_keys=[updated_by], lazy=True)

    def __repr__(self):
        return f'<TutorialPageConfig {self.slug} v{self.schema_version}>'


# ==========================================
# SUPERADMIN: BILLING / INVOICE
# ==========================================

class TenantInvoice(db.Model):
    __tablename__ = 'tenant_invoices'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    nomor = db.Column(db.String(50), unique=True, nullable=False)
    periode_mulai = db.Column(db.DateTime, nullable=True)
    periode_akhir = db.Column(db.DateTime, nullable=True)
    nominal = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(20), default='unpaid')  # unpaid, paid, overdue, cancelled
    tanggal_bayar = db.Column(db.DateTime, nullable=True)
    metode_bayar = db.Column(db.String(30), nullable=True)
    catatan = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant = db.relationship('Tenant', backref='invoices', lazy=True)
    creator = db.relationship('User', foreign_keys=[created_by], backref='invoices_created', lazy=True)

    def __repr__(self):
        return f'<TenantInvoice {self.nomor}>'


# ==========================================
# SUPERADMIN: ANNOUNCEMENTS
# ==========================================

class Announcement(db.Model):
    __tablename__ = 'announcements'

    id = db.Column(db.Integer, primary_key=True)
    judul = db.Column(db.String(200), nullable=False)
    isi = db.Column(db.Text, nullable=False)
    tipe = db.Column(db.String(20), default='info')  # info, warning, success, danger
    target = db.Column(db.String(30), default='all')  # all, or paket kode
    tanggal_mulai = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    tanggal_selesai = db.Column(db.DateTime, nullable=True)
    aktif = db.Column(db.Boolean, default=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by], backref='announcements_created', lazy=True)

    def __repr__(self):
        return f'<Announcement {self.judul}>'


class MarketplaceSeller(db.Model):
    """Seller yang dikelola Super Admin untuk marketplace B2B."""
    __tablename__ = 'marketplace_sellers'

    id = db.Column(db.Integer, primary_key=True)
    nama = db.Column(db.String(200), nullable=False)
    deskripsi = db.Column(db.Text)
    logo = db.Column(db.String(300))
    alamat = db.Column(db.Text)
    telepon = db.Column(db.String(30))
    email = db.Column(db.String(120))
    aktif = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    products = db.relationship('MarketplaceProduct', backref='seller', lazy=True,
                               cascade='all, delete-orphan')
    orders = db.relationship('MarketplaceOrder', backref='seller', lazy=True)

    def __repr__(self):
        return f'<MarketplaceSeller {self.nama}>'


class MarketplaceCategory(db.Model):
    """Kategori produk marketplace (dikelola Super Admin)."""
    __tablename__ = 'marketplace_categories'

    id = db.Column(db.Integer, primary_key=True)
    nama = db.Column(db.String(120), nullable=False)
    icon = db.Column(db.String(10), default='📦')
    slug = db.Column(db.String(120), unique=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    aktif = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    products = db.relationship('MarketplaceProduct', backref='category', lazy=True)

    def __repr__(self):
        return f'<MarketplaceCategory {self.nama}>'


class MarketplaceProduct(db.Model):
    """Produk yang dijual seller di marketplace."""
    __tablename__ = 'marketplace_products'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('marketplace_sellers.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('marketplace_categories.id'), nullable=True)
    nama = db.Column(db.String(300), nullable=False)
    deskripsi = db.Column(db.Text)
    harga = db.Column(db.Float, nullable=False, default=0)
    harga_grosir = db.Column(db.Float, nullable=True)
    min_qty_grosir = db.Column(db.Integer, nullable=True)
    stok = db.Column(db.Integer, nullable=False, default=0)
    satuan = db.Column(db.String(30), default='pcs')
    berat_gram = db.Column(db.Integer, default=0)
    gambar_utama = db.Column(db.String(300))
    sku = db.Column(db.String(100))
    aktif = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    images = db.relationship('MarketplaceProductImage', backref='product', lazy=True,
                             cascade='all, delete-orphan',
                             order_by='MarketplaceProductImage.sort_order')
    order_items = db.relationship('MarketplaceOrderItem', backref='product', lazy=True)

    def __repr__(self):
        return f'<MarketplaceProduct {self.nama}>'


class MarketplaceProductImage(db.Model):
    """Galeri foto produk marketplace."""
    __tablename__ = 'marketplace_product_images'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('marketplace_products.id'), nullable=False)
    url = db.Column(db.String(300), nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<MarketplaceProductImage {self.url}>'


MARKETPLACE_ORDER_STATUSES = [
    ('pending', 'Menunggu Konfirmasi'),
    ('confirmed', 'Dikonfirmasi'),
    ('processing', 'Diproses'),
    ('shipped', 'Dikirim'),
    ('delivered', 'Selesai'),
    ('cancelled', 'Dibatalkan'),
]

MARKETPLACE_ORDER_STATUS_LABELS = dict(MARKETPLACE_ORDER_STATUSES)


class MarketplaceOrder(db.Model):
    """Pesanan dari tenant ke marketplace."""
    __tablename__ = 'marketplace_orders'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    seller_id = db.Column(db.Integer, db.ForeignKey('marketplace_sellers.id'), nullable=False)
    nomor = db.Column(db.String(50), unique=True, nullable=False)
    status = db.Column(db.String(30), nullable=False, default='pending')
    total = db.Column(db.Float, nullable=False, default=0)
    nama_penerima = db.Column(db.String(200))
    telepon_penerima = db.Column(db.String(30))
    alamat_kirim = db.Column(db.Text)
    catatan = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant = db.relationship('Tenant', backref='marketplace_orders', lazy=True)
    items = db.relationship('MarketplaceOrderItem', backref='order', lazy=True,
                            cascade='all, delete-orphan')
    status_history = db.relationship('MarketplaceOrderStatusHistory', backref='order', lazy=True,
                                     cascade='all, delete-orphan',
                                     order_by='MarketplaceOrderStatusHistory.created_at')

    @property
    def status_label(self):
        return MARKETPLACE_ORDER_STATUS_LABELS.get(self.status, self.status)

    def __repr__(self):
        return f'<MarketplaceOrder {self.nomor}>'


class MarketplaceOrderItem(db.Model):
    """Item dalam pesanan marketplace."""
    __tablename__ = 'marketplace_order_items'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('marketplace_orders.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('marketplace_products.id'), nullable=True)
    nama_produk = db.Column(db.String(300), nullable=False)
    harga = db.Column(db.Float, nullable=False)
    qty = db.Column(db.Integer, nullable=False, default=1)
    subtotal = db.Column(db.Float, nullable=False)
    satuan = db.Column(db.String(30), default='pcs')

    def __repr__(self):
        return f'<MarketplaceOrderItem {self.nama_produk} x{self.qty}>'


class MarketplaceOrderStatusHistory(db.Model):
    """Log riwayat perubahan status pesanan marketplace."""
    __tablename__ = 'marketplace_order_status_history'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('marketplace_orders.id'), nullable=False)
    from_status = db.Column(db.String(30))
    to_status = db.Column(db.String(30), nullable=False)
    catatan = db.Column(db.Text)
    changed_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    changed_by = db.relationship('User', foreign_keys=[changed_by_user_id], lazy=True)

    @property
    def to_status_label(self):
        return MARKETPLACE_ORDER_STATUS_LABELS.get(self.to_status, self.to_status)

    def __repr__(self):
        return f'<MarketplaceOrderStatusHistory {self.from_status}->{self.to_status}>'

