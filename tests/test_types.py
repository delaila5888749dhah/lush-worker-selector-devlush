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


class TestWorkerTaskOrderQueueImmutability(unittest.TestCase):
    """WorkerTask.order_queue must be a tuple to preserve immutability."""

    def test_order_queue_list_raises_type_error(self):
        card = _make_card()
        with self.assertRaises(TypeError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=100,
                primary_card=card,
                order_queue=[card],
            )

    def test_order_queue_non_card_info_raises_type_error(self):
        card = _make_card()
        with self.assertRaises(TypeError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=100,
                primary_card=card,
                order_queue=("not_a_card",),
            )

    def test_order_queue_empty_tuple_accepted(self):
        card = _make_card()
        task = WorkerTask(
            recipient_email="test@example.com",
            amount=100,
            primary_card=card,
            order_queue=(),
        )
        self.assertEqual(task.order_queue, ())

    def test_order_queue_tuple_of_cards_accepted(self):
        card = _make_card()
        task = WorkerTask(
            recipient_email="test@example.com",
            amount=100,
            primary_card=card,
            order_queue=(card,),
        )
        self.assertEqual(len(task.order_queue), 1)


class TestWorkerTaskFieldValidation(unittest.TestCase):
    """WorkerTask must validate required fields at construction time."""

    def test_empty_recipient_email_raises_value_error(self):
        card = _make_card()
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="",
                amount=100,
                primary_card=card,
                order_queue=(),
            )

    def test_blank_recipient_email_raises_value_error(self):
        card = _make_card()
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="   ",
                amount=100,
                primary_card=card,
                order_queue=(),
            )

    def test_zero_amount_raises_value_error(self):
        card = _make_card()
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=0,
                primary_card=card,
                order_queue=(),
            )

    def test_negative_amount_raises_value_error(self):
        card = _make_card()
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=-1,
                primary_card=card,
                order_queue=(),
            )

    def test_non_card_info_primary_card_raises_value_error(self):
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=100,
                primary_card="not_a_card",
                order_queue=(),
            )
