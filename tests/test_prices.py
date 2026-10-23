"""DeepSeek peak/off-peak pricing, checked against api-docs.deepseek.com/quick_start/pricing:
peak is Monday to Friday 01:00-04:00 and 06:00-10:00 UTC, every other hour is half price.

Run from the repo root:  python3 -I -m unittest discover -s tests -v
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import swarm  # noqa: E402
from swarmlib import config  # noqa: E402

UTC = dt.timezone.utc
USAGE = {"input_tokens": 1_000_000, "output_tokens": 0}

# 2026-10-26 is a Monday.
CASES = [
    (dt.datetime(2026, 10, 26, 0, 59, tzinfo=UTC), 0.5, "Monday just before the first window"),
    (dt.datetime(2026, 10, 26, 1, 0, tzinfo=UTC), 1.0, "Monday 01:00 UTC"),
    (dt.datetime(2026, 10, 26, 3, 59, tzinfo=UTC), 1.0, "Monday 03:59 UTC"),
    (dt.datetime(2026, 10, 26, 4, 0, tzinfo=UTC), 0.5, "Monday 04:00 UTC, between windows"),
    (dt.datetime(2026, 10, 26, 6, 0, tzinfo=UTC), 1.0, "Monday 06:00 UTC"),
    (dt.datetime(2026, 10, 26, 9, 59, tzinfo=UTC), 1.0, "Monday 09:59 UTC"),
    (dt.datetime(2026, 10, 26, 10, 0, tzinfo=UTC), 0.5, "Monday 10:00 UTC"),
    (dt.datetime(2026, 10, 26, 12, 0, tzinfo=UTC), 0.5, "Monday noon UTC (was wrongly peak)"),
    (dt.datetime(2026, 10, 30, 7, 0, tzinfo=UTC), 1.0, "Friday 07:00 UTC"),
    (dt.datetime(2026, 10, 31, 2, 0, tzinfo=UTC), 0.5, "Saturday 02:00 UTC"),
    (dt.datetime(2026, 11, 1, 8, 0, tzinfo=UTC), 0.5, "Sunday 08:00 UTC"),
    # 09:30 in Beijing on a Monday is 01:30 UTC: peak, whatever zone `when` is in.
    (dt.datetime(2026, 10, 26, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=8))), 1.0,
     "Monday 09:30 Beijing time"),
]


class PeakHours(unittest.TestCase):
    def test_controller_estimate(self):
        for when, factor, label in CASES:
            est = config.estimate_cost(USAGE, "deepseek-flash", when)
            self.assertEqual(est["price_factor"], factor, label)
            self.assertAlmostEqual(est["usd"], 0.30 * factor, msg=label)

    def test_one_shot_runner_estimate(self):
        for when, factor, label in CASES:
            self.assertAlmostEqual(swarm.estimate_cost(USAGE, "deepseek-flash", when), 0.30 * factor,
                                   msg=label)

    def test_v4_pro_uses_the_same_hours(self):
        when = dt.datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        self.assertEqual(config.estimate_cost(USAGE, "deepseek-v4-pro", when)["price_factor"], 0.5)

    def test_old_single_window_form_still_reads(self):
        price = {"peak": {"utc_offset_hours": 8, "off_peak_start": "00:30", "off_peak_end": "08:30",
                          "off_peak_factor": 0.5}}
        self.assertEqual(config.peak_factor(price, dt.datetime(2026, 10, 26, 17, 0, tzinfo=UTC)), 0.5)
        self.assertEqual(config.peak_factor(price, dt.datetime(2026, 10, 26, 12, 0, tzinfo=UTC)), 1.0)

    def test_no_peak_rule_means_flat_price(self):
        self.assertEqual(config.peak_factor({}, dt.datetime(2026, 10, 26, 2, 0, tzinfo=UTC)), 1.0)


if __name__ == "__main__":
    unittest.main()
