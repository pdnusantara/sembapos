import unittest

from app.loyalty_service import member_tier_discount_pct
from app.promo_service import _items_subtotal_for_scope, promo_payload_json


class _Tier:
    def __init__(self, pct=0, aktif=True):
        self.benefit_discount_pct = pct
        self.aktif = aktif


class _Member:
    def __init__(self, tier=None):
        self.tier = tier


class LoyaltyPromoUnitTests(unittest.TestCase):
    def test_member_tier_discount_pct_uses_active_tier(self):
        m = _Member(_Tier(pct=7.5, aktif=True))
        self.assertEqual(member_tier_discount_pct(m), 7.5)

    def test_member_tier_discount_pct_zero_when_no_tier(self):
        self.assertEqual(member_tier_discount_pct(_Member(None)), 0.0)

    def test_items_subtotal_for_scope_filters_categories(self):
        items = [
            {'category_id': 1, 'line_sub': 10000},
            {'category_id': 2, 'line_sub': 15000},
            {'category_id': 1, 'line_sub': 5000},
        ]
        self.assertEqual(_items_subtotal_for_scope(items, {1}), 15000)
        self.assertEqual(_items_subtotal_for_scope(items, set()), 30000)

    def test_promo_payload_json_returns_valid_json_string(self):
        payload = {'voucher_code': 'HEMAT', 'discount': 5000}
        out = promo_payload_json(payload)
        self.assertIn('"voucher_code"', out)
        self.assertIn('"discount"', out)


if __name__ == '__main__':
    unittest.main()
