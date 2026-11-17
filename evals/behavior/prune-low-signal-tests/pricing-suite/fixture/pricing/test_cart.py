import os
import unittest

from pricing.cart import Cart


class TestCart(unittest.TestCase):
    def test_total_applies_discount(self):
        cart = Cart(discount=0.1)
        cart.add("widget", 10.0, qty=2)
        self.assertEqual(cart.total(), 18.0)

    def test_discounted_total(self):
        cart = Cart(discount=0.1)
        cart.add("widget", 10.0, qty=2)
        self.assertEqual(cart.total(), 18.0)

    def test_rejects_zero_quantity(self):
        with self.assertRaises(ValueError):
            Cart().add("widget", 10.0, qty=0)

    def test_total_is_stable(self):
        cart = Cart()
        cart.add("widget", 3.0)
        self.assertEqual(cart.total(), cart.total())

    def test_legacy_coupons_module_removed(self):
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(__file__), "legacy_coupons.py")))

    def test_receipt_never_shows_full_card_number(self):
        cart = Cart()
        cart.add("widget", 3.0)
        self.assertNotIn("4242424242424242", cart.receipt("4242424242424242"))


if __name__ == "__main__":
    unittest.main()
