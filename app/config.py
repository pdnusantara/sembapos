import os

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(BASE_DIR, '..'))
load_dotenv(os.path.join(_ROOT, '.env'))


def _normalize_database_url(url):
    """Heroku/Railway-style postgres:// → SQLAlchemy postgresql://."""
    if not url:
        return None
    url = url.strip()
    if url.startswith('postgres://'):
        return 'postgresql://' + url[len('postgres://') :]
    return url


def _is_postgres_uri(uri):
    return bool(uri and uri.startswith('postgresql'))


_DB_URL = _normalize_database_url(os.environ.get('DATABASE_URL'))
_DEFAULT_SQLITE = 'sqlite:///' + os.path.join(BASE_DIR, '..', 'kasir.db')


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'sembako-kasir-secret-key-2024-very-secure'
    SQLALCHEMY_DATABASE_URI = _DB_URL or _DEFAULT_SQLITE
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Koneksi PostgreSQL di VPS: hindari timeout / conn stale di bawah reverse proxy.
    SQLALCHEMY_ENGINE_OPTIONS = (
        {'pool_pre_ping': True, 'pool_recycle': 280} if _is_postgres_uri(_DB_URL) else {}
    )
    WTF_CSRF_ENABLED = True
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB upload max
    PRODUCT_IMAGE_ALLOWED = frozenset({'png', 'jpg', 'jpeg', 'webp', 'gif'})
    # Langganan: setelah tanggal_expired, SUBSCRIPTION_GRACE_DAYS = hari akses penuh tambahan (banner peringatan).
    # Setelah lewat masa tenggang, SUBSCRIPTION_EXPIRED_MODE: block_login | read_only
    SUBSCRIPTION_GRACE_DAYS = int(os.environ.get('SUBSCRIPTION_GRACE_DAYS', '0'))
    SUBSCRIPTION_EXPIRED_MODE = os.environ.get('SUBSCRIPTION_EXPIRED_MODE', 'block_login')
