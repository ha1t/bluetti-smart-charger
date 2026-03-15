#!/usr/bin/env python3
"""Unit tests for charge_controller.py pure functions."""

import unittest

from charge_controller import calculate_slots_needed, get_cheapest_slots, decide_charge


def make_price_info(window_prices, current_price=None, is_denki_biyori=False):
    """Helper to build a price_info dict for decide_charge tests."""
    if current_price is None:
        current_price = window_prices[0]
    avg = sum(window_prices) / len(window_prices)
    return {
        "current_price": current_price,
        "average_price": avg,
        "slot_index": 0,
        "window_slots": len(window_prices),
        "window_prices": window_prices,
        "tomorrow_available": True,
        "current_level": -0.5 if is_denki_biyori else 0,
        "is_denki_biyori": is_denki_biyori,
    }


def make_config(soc_min=20, soc_max=80, charge_rate=10.0, consumption_rate=3.0):
    return {
        "soc_min": soc_min,
        "soc_max": soc_max,
        "charge_rate_pct_per_slot": charge_rate,
        "default_consumption_rate": consumption_rate,
    }


# --- calculate_slots_needed ---

class TestCalculateSlotsNeeded(unittest.TestCase):

    def test_basic(self):
        # soc=70, soc_max=80 → gap=10
        # total_consumption = 3.0 * 24h = 72
        # total_charge_needed = 82
        # net_per_slot = 10 - 1.5 = 8.5
        # slots = ceil(82 / 8.5) = 10
        result = calculate_slots_needed(
            soc=70, soc_max=80,
            consumption_rate=3.0, charge_rate_per_slot=10.0,
            window_size=48,
        )
        self.assertEqual(result, 10)

    def test_soc_at_max_still_needs_slots_for_consumption(self):
        # soc=80, soc_max=80 → gap=0, still needs slots for consumption
        # total_consumption = 3.0 * 24 = 72
        # net = 8.5, slots = ceil(72/8.5) = 9
        result = calculate_slots_needed(
            soc=80, soc_max=80,
            consumption_rate=3.0, charge_rate_per_slot=10.0,
            window_size=48,
        )
        self.assertEqual(result, 9)

    def test_result_capped_at_window_size(self):
        result = calculate_slots_needed(
            soc=0, soc_max=80,
            consumption_rate=3.0, charge_rate_per_slot=10.0,
            window_size=5,
        )
        self.assertEqual(result, 5)

    def test_net_charge_zero_or_negative_returns_window_size(self):
        # consumption_rate=20/h, 0.5h = 10%/slot → net = 10 - 10 = 0
        result = calculate_slots_needed(
            soc=70, soc_max=80,
            consumption_rate=20.0, charge_rate_per_slot=10.0,
            window_size=48,
        )
        self.assertEqual(result, 48)

    def test_soc_above_max_no_negative_gap(self):
        # soc=85 > soc_max=80 → gap = max(0, 80-85) = 0
        # only consumption matters
        result = calculate_slots_needed(
            soc=85, soc_max=80,
            consumption_rate=3.0, charge_rate_per_slot=10.0,
            window_size=48,
        )
        self.assertGreater(result, 0)
        self.assertLessEqual(result, 48)

    def test_full_charge_needed_small_window(self):
        result = calculate_slots_needed(
            soc=20, soc_max=80,
            consumption_rate=3.0, charge_rate_per_slot=10.0,
            window_size=48,
        )
        self.assertGreater(result, 0)
        self.assertLessEqual(result, 48)


# --- get_cheapest_slots ---

class TestGetCheapestSlots(unittest.TestCase):

    def test_n_zero_returns_empty(self):
        result = get_cheapest_slots([10.0, 5.0, 8.0], 0)
        self.assertEqual(result, set())

    def test_n_gte_length_returns_all(self):
        result = get_cheapest_slots([10.0, 5.0, 8.0], 5)
        self.assertEqual(result, {0, 1, 2})

    def test_selects_cheapest_n(self):
        prices = [10.0, 3.0, 8.0, 2.0, 9.0]
        result = get_cheapest_slots(prices, 2)
        self.assertEqual(result, {1, 3})  # 3.0 and 2.0

    def test_tie_prefers_earlier_index(self):
        # 同値の場合、インデックスが小さい方を優先
        prices = [5.0, 5.0, 10.0]
        result = get_cheapest_slots(prices, 1)
        self.assertIn(0, result)
        self.assertNotIn(1, result)

    def test_n_equals_one_returns_cheapest(self):
        prices = [10.0, 2.0, 8.0]
        result = get_cheapest_slots(prices, 1)
        self.assertEqual(result, {1})

    def test_n_equals_length_minus_one(self):
        prices = [10.0, 2.0, 8.0, 5.0]
        result = get_cheapest_slots(prices, 3)
        self.assertEqual(result, {1, 2, 3})  # 最高値(10.0)のindex=0を除く


# --- decide_charge ---

class TestDecideCharge(unittest.TestCase):

    def test_force_charge_when_soc_at_min(self):
        config = make_config(soc_min=20, soc_max=80)
        price_info = make_price_info([10.0] * 48)
        result = decide_charge(20, price_info, config)
        self.assertTrue(result["charge"])
        self.assertIn("force charge", result["reason"])

    def test_force_charge_when_soc_below_min(self):
        config = make_config(soc_min=20)
        price_info = make_price_info([10.0] * 48)
        result = decide_charge(10, price_info, config)
        self.assertTrue(result["charge"])

    def test_stop_charge_when_soc_at_max_normal(self):
        config = make_config(soc_max=80)
        price_info = make_price_info([10.0] * 48, is_denki_biyori=False)
        result = decide_charge(80, price_info, config)
        self.assertFalse(result["charge"])

    def test_stop_charge_when_soc_above_max_normal(self):
        config = make_config(soc_max=80)
        price_info = make_price_info([10.0] * 48, is_denki_biyori=False)
        result = decide_charge(90, price_info, config)
        self.assertFalse(result["charge"])

    def test_continue_charge_when_soc_at_max_but_denki_biyori(self):
        # でんき日和のときはsoc_maxを超えても充電を継続する
        config = make_config(soc_max=80)
        price_info = make_price_info([10.0] * 48, is_denki_biyori=True)
        result = decide_charge(80, price_info, config)
        self.assertTrue(result["charge"])
        self.assertIn("でんき日和", result["reason"])

    def test_stop_charge_when_soc_100_even_on_denki_biyori(self):
        # SOC=100%の場合はでんき日和でも充電停止
        config = make_config(soc_max=80)
        price_info = make_price_info([10.0] * 48, is_denki_biyori=True)
        result = decide_charge(100, price_info, config)
        self.assertFalse(result["charge"])

    def test_charge_when_current_slot_is_cheap(self):
        config = make_config(soc_min=20, soc_max=80)
        # slot 0 (current) が最安値
        prices = [1.0] + [10.0] * 47
        price_info = make_price_info(prices)
        result = decide_charge(50, price_info, config, consumption_rate=3.0)
        self.assertTrue(result["charge"])

    def test_no_charge_when_current_slot_is_expensive(self):
        config = make_config(soc_min=20, soc_max=80)
        # slot 0 (current) が最高値
        prices = [20.0] + [1.0] * 47
        price_info = make_price_info(prices)
        result = decide_charge(50, price_info, config, consumption_rate=3.0)
        self.assertFalse(result["charge"])

    def test_uses_default_consumption_rate_when_none(self):
        config = make_config(soc_min=20, soc_max=80, consumption_rate=3.0)
        prices = [1.0] + [10.0] * 47
        price_info = make_price_info(prices)
        # consumption_rate=None でも動くこと
        result = decide_charge(50, price_info, config, consumption_rate=None)
        self.assertIsInstance(result["charge"], bool)

    def test_slots_needed_returned_in_result(self):
        config = make_config(soc_min=20, soc_max=80)
        prices = [1.0] + [10.0] * 47
        price_info = make_price_info(prices)
        result = decide_charge(50, price_info, config, consumption_rate=3.0)
        self.assertIn("slots_needed", result)
        self.assertIsNotNone(result["slots_needed"])

    def test_slots_needed_none_for_force_charge(self):
        config = make_config(soc_min=20)
        price_info = make_price_info([10.0] * 48)
        result = decide_charge(20, price_info, config)
        self.assertIsNone(result["slots_needed"])

    def test_slots_needed_none_for_stop_charge(self):
        config = make_config(soc_max=80)
        price_info = make_price_info([10.0] * 48, is_denki_biyori=False)
        result = decide_charge(80, price_info, config)
        self.assertIsNone(result["slots_needed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
