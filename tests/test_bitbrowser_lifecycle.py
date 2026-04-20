"""C13 — BitBrowserSession.release_profile idempotent lifecycle."""
import unittest

from modules.cdp.fingerprint import BitBrowserClient, BitBrowserSession


def _make_client():
    client = unittest.mock.Mock(spec=BitBrowserClient)
    client.create_profile.return_value = "profile-xyz"
    client.launch_profile.return_value = {"webdriver": "http://127.0.0.1:9999"}
    return client


class TestBitBrowserLifecycle(unittest.TestCase):
    def test_session_create_register_release_sequence(self):
        """__enter__ creates/launches; release_profile closes + deletes once."""
        client = _make_client()
        session = BitBrowserSession(client)
        with session as (profile_id, wsurl):
            self.assertEqual(profile_id, "profile-xyz")
            self.assertEqual(wsurl, "http://127.0.0.1:9999")
            self.assertEqual(session.profile_id, "profile-xyz")
        client.close_profile.assert_called_once_with("profile-xyz")
        client.delete_profile.assert_called_once_with("profile-xyz")

    def test_release_profile_idempotent(self):
        """Calling release_profile twice is a no-op on the second call."""
        client = _make_client()
        session = BitBrowserSession(client)
        with session:
            pass
        # __exit__ already released — a second explicit call must not re-hit the API.
        session.release_profile()
        client.close_profile.assert_called_once()
        client.delete_profile.assert_called_once()

    def test_release_called_on_exception(self):
        """Exceptions raised mid-cycle still release the profile in finally."""
        client = _make_client()
        session = BitBrowserSession(client)
        with self.assertRaises(RuntimeError):
            with session:
                raise RuntimeError("boom")
        client.close_profile.assert_called_once_with("profile-xyz")
        client.delete_profile.assert_called_once_with("profile-xyz")

    def test_release_called_on_success(self):
        """Successful cycles still release the profile."""
        client = _make_client()
        with BitBrowserSession(client):
            pass
        client.close_profile.assert_called_once()
        client.delete_profile.assert_called_once()

    def test_release_before_enter_marks_released(self):
        """Calling release without __enter__ is safe and idempotent."""
        client = _make_client()
        session = BitBrowserSession(client)
        session.release_profile()
        session.release_profile()
        client.close_profile.assert_not_called()
        client.delete_profile.assert_not_called()

    def test_register_driver_called_after_create_browser(self):
        """Integration: make_task_fn wires register_driver after BitBrowserSession.

        We verify the ordering via the worker_task source — register_driver must
        appear textually AFTER entering the BitBrowserSession context manager.
        """
        import inspect
        from integration import worker_task
        src = inspect.getsource(worker_task.make_task_fn)
        idx_session = src.find("BitBrowserSession(")
        idx_register = src.find("cdp.register_driver(")
        self.assertGreater(idx_session, -1)
        self.assertGreater(idx_register, -1)
        self.assertLess(idx_session, idx_register)


if __name__ == "__main__":
    unittest.main()
