"""
Jalankan server: python run.py

Basis data:
  - Lokal tanpa .env: SQLite (kasir.db).
  - Production: set DATABASE_URL=postgresql://... di .env atau environment.

Migrasi (setelah pip install -r requirements.txt):
  export FLASK_APP=wsgi:app
  flask db upgrade
  # atau FLASK_APP=run:app — sama, keduanya memuat create_app()
"""
from app import create_app

app = create_app()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
