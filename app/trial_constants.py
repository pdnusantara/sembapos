"""Konstanta bersama wizard pendaftaran trial / afiliasi."""

SIMPLE_ANIMAL_PASSWORD_WORDS = (
    'kucing', 'kelinci', 'beruang', 'harimau', 'gajah', 'zebra', 'panda',
    'koala', 'rusa', 'serigala', 'elang', 'lumba', 'paus', 'kuda', 'merpati',
    'kancil', 'komodo', 'badak', 'rubah', 'lebah',
)

TRIAL_JENIS_USAHA_CHOICES = (
    ('warung_sembako', 'Warung sembako / grocery'),
    ('minimarket', 'Minimarket'),
    ('toko_kelontong', 'Toko kelontong'),
    ('retail_umum', 'Retail / toko serba ada'),
    ('distributor_grosir', 'Distributor / grosir'),
    ('frozen_snack', 'Frozen food & snack'),
    ('lainnya', 'Lainnya'),
)
JENIS_USAHA_KEY_TO_LABEL = dict(TRIAL_JENIS_USAHA_CHOICES)
