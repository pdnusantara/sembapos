"""
Script untuk mengisi database dengan data demo.
Jalankan: python seed.py
"""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from app import create_app, db
from app.models import (
    Tenant,
    TenantPackage,
    Branch,
    User,
    ProductCategory,
    Product,
    Transaction,
    TransactionItem,
    StockMovement,
)
from datetime import datetime, timedelta
import random

app = create_app()

with app.app_context():
    print("🔄 Membersihkan database lama...")
    db.drop_all()
    db.create_all()

    print("👑 Membuat Super Admin...")
    superadmin = User(
        tenant_id=None,
        branch_id=None,
        nama='Super Administrator',
        username='superadmin',
        role='superadmin',
        aktif=True
    )
    superadmin.set_password('admin123')
    db.session.add(superadmin)

    print("📦 Paket langganan default...")
    pkg_basic = TenantPackage(
        kode='basic', nama='Basic', deskripsi='Paket pemula',
        max_cabang=3, max_user=5, modules_json=None,
        harga_bulanan=99000, harga_tahunan=990000, aktif=True, sort_order=10,
    )
    pkg_pro = TenantPackage(
        kode='pro', nama='Pro', deskripsi='Bisnis berkembang',
        max_cabang=10, max_user=20, modules_json=None,
        harga_bulanan=299000, harga_tahunan=2990000, aktif=True, sort_order=20,
    )
    pkg_ent = TenantPackage(
        kode='enterprise', nama='Enterprise', deskripsi='Skala besar',
        max_cabang=9999, max_user=9999, modules_json=None,
        harga_bulanan=0, harga_tahunan=0, aktif=True, sort_order=30,
    )
    db.session.add_all([pkg_basic, pkg_pro, pkg_ent])
    db.session.flush()

    # ========== TENANT 1: TOKO MAKMUR ==========
    print("🏪 Membuat Tenant 1: Toko Makmur...")
    tenant1 = Tenant(
        nama='Toko Makmur',
        kode='TOKO_MAKMUR',
        alamat='Jl. Raya Kuningan No. 10',
        telepon='08111234567',
        email='makmur@toko.com',
        paket_id=pkg_pro.id,
        paket='pro',
        aktif=True,
        max_cabang=5,
        max_user=10
    )
    db.session.add(tenant1)
    db.session.flush()

    # Cabang Toko Makmur
    branch1_main = Branch(tenant_id=tenant1.id, nama='Cabang Pusat', kode='MAIN', alamat='Jl. Raya Kuningan No. 10', telepon='08111234567', aktif=True)
    branch1_b = Branch(tenant_id=tenant1.id, nama='Cabang Timur', kode='TMR', alamat='Jl. Timur Raya No. 5', telepon='08219876543', aktif=True)
    db.session.add_all([branch1_main, branch1_b])
    db.session.flush()

    # User Toko Makmur
    admin1 = User(tenant_id=tenant1.id, branch_id=branch1_main.id, nama='Admin Makmur', username='admin_toko_makmur', role='admin', aktif=True)
    admin1.set_password('admin123')
    kasir1 = User(tenant_id=tenant1.id, branch_id=branch1_main.id, nama='Budi Santoso', username='kasir_makmur', role='kasir', aktif=True)
    kasir1.set_password('kasir123')
    kasir2 = User(tenant_id=tenant1.id, branch_id=branch1_b.id, nama='Sari Dewi', username='kasir_timur', role='kasir', aktif=True)
    kasir2.set_password('kasir123')
    db.session.add_all([admin1, kasir1, kasir2])

    # Kategori Toko Makmur
    cat_beras = ProductCategory(tenant_id=tenant1.id, nama='Beras & Gula')
    cat_minyak = ProductCategory(tenant_id=tenant1.id, nama='Minyak & Lemak')
    cat_bumbu = ProductCategory(tenant_id=tenant1.id, nama='Bumbu Dapur')
    cat_minuman = ProductCategory(tenant_id=tenant1.id, nama='Minuman')
    cat_snack = ProductCategory(tenant_id=tenant1.id, nama='Snack & Cemilan')
    cat_sabun = ProductCategory(tenant_id=tenant1.id, nama='Kebersihan')
    db.session.add_all([cat_beras, cat_minyak, cat_bumbu, cat_minuman, cat_snack, cat_sabun])
    db.session.flush()

    # Produk Toko Makmur
    produk_makmur = [
        # Beras & Gula
        Product(tenant_id=tenant1.id, category_id=cat_beras.id, nama='Beras Premium 5kg', barcode='8991001001', satuan='karung', harga_beli=62000, harga_jual=72000, stok=50, stok_minimum=10),
        Product(tenant_id=tenant1.id, category_id=cat_beras.id, nama='Beras Medium 5kg', satuan='karung', harga_beli=52000, harga_jual=60000, stok=40, stok_minimum=10),
        Product(tenant_id=tenant1.id, category_id=cat_beras.id, nama='Gula Pasir 1kg', barcode='8991002001', satuan='kg', harga_beli=13000, harga_jual=15000, stok=100, stok_minimum=20),
        Product(tenant_id=tenant1.id, category_id=cat_beras.id, nama='Gula Merah 1kg', satuan='kg', harga_beli=17000, harga_jual=20000, stok=30, stok_minimum=5),
        # Minyak
        Product(tenant_id=tenant1.id, category_id=cat_minyak.id, nama='Minyak Goreng Bimoli 2L', barcode='8991003001', satuan='botol', harga_beli=28000, harga_jual=33000, stok=60, stok_minimum=10),
        Product(tenant_id=tenant1.id, category_id=cat_minyak.id, nama='Minyak Goreng 1L', satuan='botol', harga_beli=14500, harga_jual=17000, stok=80, stok_minimum=15),
        # Bumbu
        Product(tenant_id=tenant1.id, category_id=cat_bumbu.id, nama='Garam Halus 250gr', satuan='pcs', harga_beli=2500, harga_jual=3500, stok=200, stok_minimum=30),
        Product(tenant_id=tenant1.id, category_id=cat_bumbu.id, nama='Kecap Manis Bango 135ml', barcode='8991004001', satuan='botol', harga_beli=6000, harga_jual=8000, stok=100, stok_minimum=20),
        Product(tenant_id=tenant1.id, category_id=cat_bumbu.id, nama='Saus Tomat ABC 135ml', satuan='botol', harga_beli=6500, harga_jual=9000, stok=80, stok_minimum=20),
        Product(tenant_id=tenant1.id, category_id=cat_bumbu.id, nama='Tepung Terigu Segitiga 1kg', satuan='kg', harga_beli=9000, harga_jual=12000, stok=50, stok_minimum=10),
        # Minuman
        Product(tenant_id=tenant1.id, category_id=cat_minuman.id, nama='Air Mineral Aqua 1.5L', barcode='8991005001', satuan='botol', harga_beli=3500, harga_jual=5000, stok=150, stok_minimum=30),
        Product(tenant_id=tenant1.id, category_id=cat_minuman.id, nama='Air Mineral Aqua 600ml', satuan='botol', harga_beli=2000, harga_jual=3000, stok=200, stok_minimum=40),
        Product(tenant_id=tenant1.id, category_id=cat_minuman.id, nama='Teh Kotak 200ml', satuan='dus', harga_beli=3500, harga_jual=5000, stok=100, stok_minimum=20),
        Product(tenant_id=tenant1.id, category_id=cat_minuman.id, nama='Kopi Kapal Api Sachet', satuan='pak', harga_beli=4500, harga_jual=6500, stok=80, stok_minimum=15),
        # Snack
        Product(tenant_id=tenant1.id, category_id=cat_snack.id, nama='Indomie Goreng', barcode='8991006001', satuan='pcs', harga_beli=2800, harga_jual=3500, stok=300, stok_minimum=50),
        Product(tenant_id=tenant1.id, category_id=cat_snack.id, nama='Indomie Kuah', satuan='pcs', harga_beli=2800, harga_jual=3500, stok=200, stok_minimum=50),
        Product(tenant_id=tenant1.id, category_id=cat_snack.id, nama='Chitato 140gr', satuan='pcs', harga_beli=12000, harga_jual=16000, stok=50, stok_minimum=10),
        # Kebersihan
        Product(tenant_id=tenant1.id, category_id=cat_sabun.id, nama='Sabun Lifebuoy 85gr', barcode='8991007001', satuan='pcs', harga_beli=3000, harga_jual=4500, stok=100, stok_minimum=20),
        Product(tenant_id=tenant1.id, category_id=cat_sabun.id, nama='Detergen Rinso 1kg', satuan='pcs', harga_beli=19000, harga_jual=24000, stok=40, stok_minimum=8),
        Product(tenant_id=tenant1.id, category_id=cat_sabun.id, nama='Sunlight Jeruk 400ml', satuan='botol', harga_beli=9500, harga_jual=13000, stok=60, stok_minimum=12),
    ]
    db.session.add_all(produk_makmur)

    # ========== TENANT 2: TOKO SEJAHTERA ==========
    print("🏪 Membuat Tenant 2: Toko Sejahtera...")
    tenant2 = Tenant(
        nama='Toko Sejahtera',
        kode='SEJAHTERA',
        alamat='Jl. Pasar Baru No. 25, Cirebon',
        telepon='08222345678',
        email='sejahtera@toko.com',
        paket_id=pkg_basic.id,
        paket='basic',
        aktif=True,
        max_cabang=3,
        max_user=5
    )
    db.session.add(tenant2)
    db.session.flush()

    branch2_main = Branch(tenant_id=tenant2.id, nama='Toko Utama', kode='MAIN', alamat='Jl. Pasar Baru No. 25', aktif=True)
    db.session.add(branch2_main)
    db.session.flush()

    admin2 = User(tenant_id=tenant2.id, branch_id=branch2_main.id, nama='Admin Sejahtera', username='admin_sejahtera', role='admin', aktif=True)
    admin2.set_password('admin123')
    kasir3 = User(tenant_id=tenant2.id, branch_id=branch2_main.id, nama='Rina Oktaviani', username='kasir_sejahtera', role='kasir', aktif=True)
    kasir3.set_password('kasir123')
    db.session.add_all([admin2, kasir3])

    cat2_bahan = ProductCategory(tenant_id=tenant2.id, nama='Bahan Pokok')
    cat2_minuman = ProductCategory(tenant_id=tenant2.id, nama='Minuman')
    db.session.add_all([cat2_bahan, cat2_minuman])
    db.session.flush()

    produk2 = [
        Product(tenant_id=tenant2.id, category_id=cat2_bahan.id, nama='Beras 5kg', satuan='karung', harga_beli=58000, harga_jual=68000, stok=30, stok_minimum=5),
        Product(tenant_id=tenant2.id, category_id=cat2_bahan.id, nama='Gula 1kg', satuan='kg', harga_beli=13000, harga_jual=16000, stok=50, stok_minimum=10),
        Product(tenant_id=tenant2.id, category_id=cat2_bahan.id, nama='Minyak 2L', satuan='botol', harga_beli=29000, harga_jual=34000, stok=40, stok_minimum=8),
        Product(tenant_id=tenant2.id, category_id=cat2_minuman.id, nama='Aqua 1.5L', satuan='botol', harga_beli=3500, harga_jual=5000, stok=100, stok_minimum=20),
        Product(tenant_id=tenant2.id, category_id=cat2_minuman.id, nama='Indomie Rasa Soto', satuan='pcs', harga_beli=2800, harga_jual=3500, stok=150, stok_minimum=30),
    ]
    db.session.add_all(produk2)
    db.session.flush()

    print("🧾 Membuat data transaksi demo...")
    # Generate beberapa transaksi untuk Toko Makmur
    db.session.flush()
    all_products_t1 = produk_makmur[:10]  # subset produk

    def make_trx(tenant_id, branch_id, user_id, products_pool, days_ago=0, trx_idx=1):
        date = datetime.utcnow() - timedelta(days=days_ago, hours=random.randint(8, 20), minutes=random.randint(0, 59))
        num_items = random.randint(1, 4)
        items_chosen = random.sample(products_pool, min(num_items, len(products_pool)))
        subtotal = 0
        trx_items = []
        for p in items_chosen:
            qty = random.randint(1, 3)
            if p.stok < qty:
                qty = max(1, int(p.stok))
            if qty == 0:
                continue
            sub = p.harga_jual * qty
            subtotal += sub
            trx_items.append((p, qty, sub))

        if not trx_items:
            return

        total = subtotal
        bayar = total + random.choice([0, 5000, 10000, 20000, 50000])
        nomor = f"TRX-{date.strftime('%Y%m%d')}-{branch_id:04d}-{trx_idx:04d}"
        trx = Transaction(
            tenant_id=tenant_id, branch_id=branch_id, user_id=user_id,
            nomor=nomor, subtotal=subtotal, diskon=0, total=total,
            bayar=bayar, kembalian=bayar-total,
            metode_bayar=random.choice(['tunai', 'tunai', 'transfer', 'qris']),
            status='selesai', created_at=date
        )
        db.session.add(trx)
        db.session.flush()
        for p, qty, sub in trx_items:
            db.session.add(TransactionItem(
                transaction_id=trx.id, product_id=p.id,
                nama_produk=p.nama, harga=p.harga_jual, qty=qty, subtotal=sub
            ))
            if p.stok >= qty:
                stok_sblm = p.stok
                p.stok -= qty
                db.session.add(StockMovement(
                    product_id=p.id, user_id=user_id, tipe='keluar',
                    qty=qty, stok_sebelum=stok_sblm, stok_sesudah=p.stok,
                    keterangan=f'Penjualan #{nomor}', created_at=date
                ))

    # Generate 60 transaksi selama 7 hari
    trx_idx = 1
    for day in range(7):
        n_trx = random.randint(8, 15)
        for j in range(n_trx):
            make_trx(tenant1.id, branch1_main.id, kasir1.id, all_products_t1, days_ago=day, trx_idx=trx_idx)
            trx_idx += 1
        if day < 3:
            for j in range(random.randint(3, 7)):
                make_trx(tenant1.id, branch1_b.id, kasir2.id, all_products_t1, days_ago=day, trx_idx=trx_idx)
                trx_idx += 1

    # Beberapa transaksi tenant 2
    for day in range(4):
        for j in range(random.randint(3, 6)):
            make_trx(tenant2.id, branch2_main.id, kasir3.id, produk2, days_ago=day, trx_idx=trx_idx)
            trx_idx += 1

    db.session.commit()
    print("\n✅ Seed data berhasil dibuat!")
    print("\n📋 AKUN LOGIN DEMO:")
    print("=" * 50)
    print("🔑 Super Admin  : superadmin / admin123")
    print("🛡️ Admin Makmur : admin_toko_makmur / admin123")
    print("💼 Kasir Makmur : kasir_makmur / kasir123")
    print("💼 Kasir Timur  : kasir_timur / kasir123")
    print("🛡️ Admin Sejahtera: admin_sejahtera / admin123")
    print("💼 Kasir Sejahtera: kasir_sejahtera / kasir123")
    print("=" * 50)
    print("\n🚀 Jalankan app: python run.py")
    print("🌐 Lalu buka: http://127.0.0.1:5000")
