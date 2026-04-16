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

    def test_task_id_blank_raises_value_error(self):
        """A whitespace-only task_id must raise ValueError."""
        card = _make_card()
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=100,
                primary_card=card,
                order_queue=(),
                task_id="   ",
            )


class TestWorkerTaskOrderQueueImmutability(unittest.TestCase):
    """WorkerTask.order_queue must be a tuple to preserve immutability."""

    def test_order_queue_list_raises_type_error(self):
        """A list passed as order_queue must raise TypeError."""
        card = _make_card()
        with self.assertRaises(TypeError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=100,
                primary_card=card,
                order_queue=[card],
            )

    def test_order_queue_non_card_info_raises_type_error(self):
        """Non-CardInfo elements inside the tuple must raise TypeError."""
        card = _make_card()
        with self.assertRaises(TypeError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=100,
                primary_card=card,
                order_queue=("not_a_card",),
            )

    def test_order_queue_empty_tuple_accepted(self):
        """An empty tuple is a valid order_queue value."""
        card = _make_card()
        task = WorkerTask(
            recipient_email="test@example.com",
            amount=100,
            primary_card=card,
            order_queue=(),
        )
        self.assertEqual(task.order_queue, ())

    def test_order_queue_tuple_of_cards_accepted(self):
        """A tuple of CardInfo instances is a valid order_queue value."""
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
        """An empty string for recipient_email must raise ValueError."""
        card = _make_card()
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="",
                amount=100,
                primary_card=card,
                order_queue=(),
            )

    def test_blank_recipient_email_raises_value_error(self):
        """A whitespace-only recipient_email must raise ValueError."""
        card = _make_card()
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="   ",
                amount=100,
                primary_card=card,
                order_queue=(),
            )

    def test_non_string_recipient_email_raises_value_error(self):
        """A non-string recipient_email must raise ValueError."""
        card = _make_card()
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email=None,
                amount=100,
                primary_card=card,
                order_queue=(),
            )

    def test_zero_amount_raises_value_error(self):
        """Zero amount must raise ValueError."""
        card = _make_card()
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=0,
                primary_card=card,
                order_queue=(),
            )

    def test_negative_amount_raises_value_error(self):
        """A negative amount must raise ValueError."""
        card = _make_card()
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=-1,
                primary_card=card,
                order_queue=(),
            )

    def test_bool_amount_raises_value_error(self):
        """A bool amount must raise ValueError even though bool is an int subclass."""
        card = _make_card()
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=True,
                primary_card=card,
                order_queue=(),
            )

    def test_non_card_info_primary_card_raises_value_error(self):
        """A non-CardInfo primary_card must raise ValueError."""
        with self.assertRaises(ValueError):
            WorkerTask(
                recipient_email="test@example.com",
                amount=100,
                primary_card="not_a_card",
                order_queue=(),
            )
