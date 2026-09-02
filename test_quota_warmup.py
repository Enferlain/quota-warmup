import unittest
from datetime import datetime

from quota_warmup import (
    AntigravityProvider,
    Quota,
    activity_cutoff_ms,
    parse_antigravity_usage,
    parse_codex_rate_limits,
    parse_glm_quotas,
    due_quotas,
    state_bucket,
)


class ParsingTests(unittest.TestCase):
    def test_antigravity_tracks_both_windows_for_both_groups(self):
        payload = {
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.75},
                                {"id": "gemini-5h", "window": "5h", "remaining_fraction": 1.0},
                            ],
                        },
                        {
                            "name": "Claude and GPT models",
                            "buckets": [
                                {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.50},
                                {"id": "3p-5h", "window": "5h", "remaining_fraction": 0.90},
                            ],
                        },
                    ]
                }
            }
        }
        quotas = parse_antigravity_usage(payload)
        self.assertEqual(len(quotas), 4)
        self.assertEqual({quota.group for quota in quotas}, {"gemini", "third_party"})
        self.assertEqual({quota.window for quota in quotas}, {"weekly", "5h"})
        self.assertAlmostEqual(next(quota.used_fraction for quota in quotas if quota.quota_id.endswith("gemini-weekly")), 0.25)

    def test_codex_primary_and_secondary_buckets(self):
        quotas = parse_codex_rate_limits(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": 1800000000},
                        "secondary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": 1800000100},
                    }
                }
            }
        )
        self.assertEqual({quota.window for quota in quotas}, {"weekly", "5h"})
        self.assertAlmostEqual(next(quota.used_fraction for quota in quotas if quota.window == "weekly"), 0.20)

    def test_glm_monitor_limits_are_normalized_as_used_percent(self):
        quotas = parse_glm_quotas({"data": {"limits": [{"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "percentage": 35, "nextResetTime": 1800000000000}, {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "percentage": 10, "nextResetTime": 1800000000000}]}})
        self.assertEqual([quota.window for quota in quotas], ["5h", "weekly"])
        self.assertAlmostEqual(quotas[0].used_fraction, 0.35)
        self.assertIsNotNone(quotas[0].reset_time)

    def test_glm_legacy_monitor_limit_is_still_supported(self):
        quotas = parse_glm_quotas({"data": {"limits": [{"type": "TOKENS_LIMIT", "percentage": 35}]}})
        self.assertEqual(quotas[0].window, "5h")
        self.assertAlmostEqual(quotas[0].used_fraction, 0.35)

    def test_glm_monthly_mcp_limit_is_not_warmable(self):
        quotas = parse_glm_quotas({"data": {"limits": [{"type": "TIME_LIMIT", "unit": 5, "number": 1, "percentage": 20}, {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 10}]}})
        self.assertEqual([quota.window for quota in quotas], ["5h"])

    def test_antigravity_model_preferences_pick_flash_low(self):
        provider = AntigravityProvider({"model_preferences": {"gemini": ["gemini-3.5-flash-low"]}})
        model, effort = provider.choose_model("gemini", ["gemini-3.5-flash-medium", "gemini-3.5-flash-low", "gemini-3.1-pro-high"])
        self.assertEqual(model, "gemini-3.5-flash-low")
        self.assertEqual(effort, "low")


class StateTests(unittest.TestCase):
    def test_exact_activity_means_rounded_zero_window_is_already_started(self):
        quota = Quota("test", "test:5h", "test", "5h", 1.0, metadata={"activity_detected": True})
        state = {"buckets": {}}
        self.assertEqual(due_quotas(state, [quota], 0.0, 0.02), [])

    def test_zero_five_hour_window_is_due(self):
        quota = Quota("test", "test:5h", "test", "5h", 1.0)
        self.assertEqual(due_quotas({"buckets": {}}, [quota], 0.0, 0.02), [quota])

    def test_zero_weekly_window_is_not_a_warmup_target(self):
        quota = Quota("test", "test:weekly", "test", "weekly", 1.0)
        self.assertEqual(due_quotas({"buckets": {}}, [quota], 0.0, 0.02), [])

    def test_exact_activity_does_not_clear_kicked_state_at_zero_percent(self):
        quota = Quota("test", "test:5h", "test", "5h", 1.0, metadata={"activity_detected": True})
        state = {"buckets": {"test:5h": {"kicked": True, "last_used_fraction": 0.0}}}
        entry = state_bucket(state, quota, 0.02)
        self.assertTrue(entry["kicked"])

    def test_attempted_window_is_not_retried(self):
        quota = Quota("test", "test:5h", "test", "5h", 1.0)
        state = {"buckets": {"test:5h": {"attempted": True, "kicked": False, "last_used_fraction": 0.0, "hold_until": "2999-01-01T00:00:00Z"}}}
        self.assertEqual(due_quotas(state, [quota], 0.0, 0.02), [])

    def test_activity_cutoff_uses_provider_window_boundary(self):
        quota = Quota("test", "test:weekly", "test", "weekly", 1.0, "2026-09-07T00:00:00Z", metadata={"window_duration_mins": 10080})
        now_ms = int(datetime.fromisoformat("2026-09-02T00:00:00+00:00").timestamp() * 1000)
        expected = int(datetime.fromisoformat("2026-08-31T00:00:00+00:00").timestamp() * 1000)
        self.assertEqual(activity_cutoff_ms(quota, now_ms), expected)

    def test_zero_usage_clears_previous_kick(self):
        quota = Quota("test", "test:weekly", "test", "weekly", 0.5, "2026-01-01T00:00:00Z")
        state = {"buckets": {}}
        entry = state_bucket(state, quota, 0.02)
        entry["kicked"] = True
        reset_quota = Quota("test", "test:weekly", "test", "weekly", 1.0, "2026-01-01T00:00:00Z")
        reset_entry = state_bucket(state, reset_quota, 0.02)
        self.assertFalse(reset_entry["kicked"])


if __name__ == "__main__":
    unittest.main()
