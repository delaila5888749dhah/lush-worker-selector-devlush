import unittest

from modules.common.types import CardInfo, WorkerTask


def _make_card():
    return CardInfo(
        card_number="4111111111111111",
        exp_month="07",
        exp_year="27",
        cvv="123",
    )


class TestWorkerTaskIdNoneRejected(unittest.TestCase):
    """WorkerTask must reject task_id=None to preserve idempotency."""

    def test_task_id_none_raises_value_error(self):
        card = _make_card()
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=100,
                primary_card=card,
                order_queue=(),
                task_id=None,
            )
