"""Kontak publik (WhatsApp landing/tutorial) dari AppSetting / Super Admin."""
import re
from urllib.parse import quote

from .models import AppSetting

DEFAULT_LANDING_WA_PHONE = '6281234567890'
DEFAULT_LANDING_WA_MESSAGE = 'Halo, saya ingin tanya tentang SembaPOS'


def _digits_only(s):
    return re.sub(r'\D', '', (s or '').strip())


def public_whatsapp_link():
    """
    URL https://wa.me/... dari kunci AppSetting:
    landing_wa_phone, landing_wa_message (atur di Super Admin → Pengaturan Platform).
    """
    raw_phone = (AppSetting.get('landing_wa_phone', DEFAULT_LANDING_WA_PHONE) or DEFAULT_LANDING_WA_PHONE).strip()
    phone = _digits_only(raw_phone)
    if not phone:
        phone = _digits_only(DEFAULT_LANDING_WA_PHONE)
    elif phone.startswith('0') and len(phone) >= 10:
        phone = '62' + phone[1:]
    elif not phone.startswith('62'):
        phone = '62' + phone.lstrip('0')
    msg = (AppSetting.get('landing_wa_message', DEFAULT_LANDING_WA_MESSAGE) or DEFAULT_LANDING_WA_MESSAGE).strip()
    if not msg:
        msg = DEFAULT_LANDING_WA_MESSAGE
    return f'https://wa.me/{phone}?text={quote(msg)}'
