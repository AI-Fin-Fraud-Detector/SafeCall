"""Run with: python3 -m unittest discover -s tests -v (from backend/fraud).

Session methods are loaded from their actual source AST to exercise scheduling
without importing audio/ML services, generated protobufs, or requiring API keys.
External I/O is mocked; these are not device/push integration tests.
"""

import ast
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import const
from src.ssci import compute_ssci, extract_trigger_results


def load_session_methods():
    tree = ast.parse((ROOT / "src/voice_session.py").read_text())
    session = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_Session")
    names = {"_evaluate_ssci", "_evaluate_ssci_at_start", "_run_fraud_detection"}
    session.body = [node for node in session.body if getattr(node, "name", None) in names]
    module = ast.Module(body=[session], type_ignores=[])
    namespace = {
        "asyncio": asyncio,
        "time": SimpleNamespace(monotonic=Mock(return_value=0.0)),
        "SSCI_MAX_DURATION_SECONDS": const.SSCI_MAX_DURATION_SECONDS,
        "INFERENCES_PER_TRIGGER": const.INFERENCES_PER_TRIGGER,
        "extract_trigger_results": extract_trigger_results,
        "compute_ssci": compute_ssci,
        "notify_user_in_background": Mock(),
    }
    exec(compile(module, str(ROOT / "src/voice_session.py"), "exec"), namespace)
    return namespace["_Session"], namespace


class ScoreTests(unittest.TestCase):
    def test_all_predictions_and_sentence_scale(self):
        raw = [False, False, False, True, True, True]
        extracted = extract_trigger_results(raw)
        self.assertEqual(extracted, raw)
        self.assertIsNot(extracted, raw)
        self.assertEqual(compute_ssci(extracted)["n_k"], 12)
        self.assertEqual(const.TAU, 2.0)

    def test_bonus_once_reset_and_restore(self):
        raw = [True, True, True, True, False, True, True]
        bonuses = [compute_ssci(raw[:k])["consecutive_true_bonus"] for k in range(1, 8)]
        self.assertEqual(bonuses, [0, .1, .1, .1, 0, 0, .1])
        self.assertEqual(compute_ssci(raw[:4])["consecutive_true_count"], 4)
        self.assertEqual(compute_ssci(raw[:5])["consecutive_true_count"], 0)

    def test_reported_case(self):
        raw = [False, False, False, True, True, True]
        fifth = compute_ssci(raw[:5], "non_contact")
        sixth = compute_ssci(raw, "non_contact")
        self.assertAlmostEqual(fifth["base_scam_probability"], .4000559)
        self.assertAlmostEqual(fifth["scam_probability"], .5000559)
        self.assertAlmostEqual(sixth["scam_probability"], .6058199)
        self.assertGreater(fifth["scam_probability"], fifth["scam_threshold"])

    def test_cap_and_unchanged_identity_thresholds(self):
        with patch("src.ssci.SSCI_CONSECUTIVE_TRUE_BONUS", 1.0):
            self.assertEqual(compute_ssci([True, True])["scam_probability"], 1.0)
        for caller, threshold in [("contact", .4), ("non_contact", .5), ("private", .55), (None, .5)]:
            self.assertEqual(compute_ssci([True], caller)["scam_threshold"], threshold)

    def test_empty_history(self):
        self.assertEqual(compute_ssci([]), {})

    def test_line_thresholds_remain_independent(self):
        for caller, threshold in [("private", .50), ("non_contact", .55), (None, .55)]:
            self.assertEqual(compute_ssci([True], caller)["line_alert_threshold"], threshold)


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        session_type, self.globals = load_session_methods()
        self.session = session_type()
        s = self.session
        s.call_active = asyncio.Event()
        s.call_active.set()
        s.call_started_at = 0.0
        s.latest_ssci = None
        s.ssci_action_started = False
        s.ssci_action_task = None
        s.scam_notified = False
        s.user_uuid = None
        s.caller_type = "non_contact"
        s.caller_phone = "0900000000"
        s.caller_name = "Test caller"
        s.conversation_id = "test"
        s.raw_results = []
        s.inference_index = 0
        s.last_prediction = None
        s.trigger_index = 0
        s.scam_probability = 0.0
        s.messages = [{"id": "message", "role": "user"}]
        s.ssci_snapshots = []
        s._handle_scam_detected = AsyncMock()
        s._format_conversation_for_detection = AsyncMock(return_value="conversation")
        s._call_fraud_detection_api = AsyncMock()
        s._update_message_metadata = AsyncMock()
        s._update_conversation_metadata = AsyncMock()

    async def asyncTearDown(self):
        if self.session.ssci_action_task:
            await self.session.ssci_action_task

    async def predict(self, prediction, age):
        self.globals["time"].monotonic.return_value = age
        self.session._call_fraud_detection_api.return_value = prediction
        await self.session._run_fraud_detection()

    async def test_case_alerts_on_fifth_without_duplicates(self):
        for prediction, age in zip([False, False, False, True], [0, 27, 64, 104]):
            await self.predict(prediction, age)
        self.assertFalse(self.session.ssci_action_started)
        await self.predict(True, 130)
        self.assertTrue(self.session.ssci_action_started)
        task = self.session.ssci_action_task
        await task
        await self.predict(True, 170)
        await self.predict(False, 180)
        self.assertIs(self.session.ssci_action_task, task)
        self.session._handle_scam_detected.assert_awaited_once()
        self.assertEqual(self.session.latest_ssci["consecutive_true_bonus"], 0)
        self.assertTrue(self.session.ssci_action_started)
        self.assertEqual(len(self.session.ssci_snapshots), 7)
        self.assertEqual(self.session._update_conversation_metadata.await_count, 7)

    async def test_low_score_does_not_lock_future_evaluations(self):
        for prediction, age in zip([False, False, False, True], [0, 27, 64, 130]):
            await self.predict(prediction, age)
        self.assertFalse(self.session.ssci_action_started)
        await self.predict(True, 160)
        self.assertTrue(self.session.ssci_action_started)

    async def test_failed_prediction_does_not_reset_streak_or_update(self):
        await self.predict(True, 10)
        await self.predict(None, 20)
        await self.predict(True, 30)
        self.assertEqual(self.session.raw_results, [True, True])
        self.assertEqual(self.session.latest_ssci["consecutive_true_bonus"], .1)
        self.assertEqual(self.session.inference_index, 2)
        self.assertEqual(self.session._update_conversation_metadata.await_count, 2)

    async def test_strict_threshold_and_inactive_call(self):
        self.globals["time"].monotonic.return_value = 120
        self.session.latest_ssci = {"scam_probability": .5, "scam_threshold": .5}
        await self.session._evaluate_ssci()
        self.assertFalse(self.session.ssci_action_started)
        self.session.latest_ssci["scam_probability"] = .6
        self.session.call_active.clear()
        await self.session._evaluate_ssci()
        self.assertFalse(self.session.ssci_action_started)

    async def test_timer_evaluates_cached_score_without_new_prediction(self):
        await self.predict(True, 90)
        await self.predict(True, 100)
        await self.predict(True, 110)
        self.assertFalse(self.session.ssci_action_started)
        real_sleep = asyncio.sleep

        async def advance_clock(delay):
            self.assertEqual(delay, 10)
            self.globals["time"].monotonic.return_value = 120
            await real_sleep(0)

        with patch.object(asyncio, "sleep", side_effect=advance_clock):
            await self.session._evaluate_ssci_at_start()
        self.assertTrue(self.session.ssci_action_started)

    async def test_timer_with_no_evidence_keeps_observing(self):
        self.globals["time"].monotonic.return_value = 120
        await self.session._evaluate_ssci_at_start()
        self.assertFalse(self.session.ssci_action_started)
        await self.predict(True, 130)
        await self.predict(True, 140)
        await self.predict(True, 150)
        self.assertTrue(self.session.ssci_action_started)

    async def test_line_alert_before_120_seconds_at_its_own_threshold_once(self):
        s = self.session
        s.user_uuid = "test-user"
        s.messages = []  # No message push; exercise only the LINE notification.
        self.globals["compute_ssci"] = Mock(return_value={
            "scam_probability": .54, "scam_threshold": .5, "line_alert_threshold": .55,
        })
        notify = self.globals["notify_user_in_background"]
        await self.predict(True, 10)
        notify.assert_not_called()
        self.globals["compute_ssci"].return_value["scam_probability"] = .55
        await self.predict(True, 20)
        await self.predict(True, 30)
        notify.assert_called_once_with("test-user", "scam_detected", {
            "caller_phone": s.caller_phone,
            "caller_name": s.caller_name,
            "caller_type": "non_contact",
            "scam_probability": .55,
        })
        self.assertFalse(s.ssci_action_started)
        self.assertTrue(s.scam_notified)
        # An earlier LINE alert must not block the later in-call fraud alert.
        await self.predict(True, 130)
        self.assertTrue(s.ssci_action_started)
        self.assertEqual(notify.call_count, 1)

    async def test_in_call_alert_does_not_bypass_line_threshold(self):
        s = self.session
        s.user_uuid = "test-user"
        s.messages = []
        self.globals["compute_ssci"] = Mock(return_value={
            "scam_probability": .5000559, "scam_threshold": .5, "line_alert_threshold": .55,
        })
        await self.predict(True, 130)
        self.assertTrue(s.ssci_action_started)
        self.globals["notify_user_in_background"].assert_not_called()
        self.assertFalse(s.scam_notified)
        self.globals["compute_ssci"].return_value["scam_probability"] = .6058199
        await self.predict(True, 170)
        self.globals["notify_user_in_background"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
