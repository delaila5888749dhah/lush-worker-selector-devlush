"""M14 — force_rollback idempotency within a scale-up window."""
import threading
import unittest

from modules.rollout import main as rollout


class TestRollbackIdempotent(unittest.TestCase):
    def setUp(self):
        rollout.reset()
        # Configure with a healthy-check to allow try_scale_up() to progress.
        rollout.configure(
            check_rollback_fn=lambda: [],
            save_baseline_fn=lambda: None,
        )
        # Move to step 2 so force_rollback has room to decrement.
        rollout.try_scale_up()
        rollout.try_scale_up()

    def tearDown(self):
        rollout.reset()

    def test_two_concurrent_rollbacks_decrement_once(self):
        """Two concurrent force_rollback calls decrement the step at most once."""
        start_step = rollout.get_current_step_index()
        results = []

        def _call():
            results.append(rollout.force_rollback(reason="concurrent"))

        threads = [threading.Thread(target=_call) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2)
        end_step = rollout.get_current_step_index()
        self.assertEqual(start_step - end_step, 1,
                         "force_rollback must decrement step index exactly once")

    def test_scale_up_resets_flag(self):
        """A successful try_scale_up re-arms the rollback window."""
        rollout.force_rollback(reason="first window")
        # Second call in the same window is a no-op.
        step_after_first = rollout.get_current_step_index()
        rollout.force_rollback(reason="second same window")
        self.assertEqual(rollout.get_current_step_index(), step_after_first)
        # try_scale_up re-arms.
        rollout.try_scale_up()
        rollout.force_rollback(reason="new window")
        self.assertEqual(
            rollout.get_current_step_index(),
            step_after_first,  # advanced then rolled back
        )


if __name__ == "__main__":
    unittest.main()
