"""
Simulated messaging provider.

Stands in for WhatsApp Business / SMS. It is deliberately unreliable on demand:
``SimulationControl.messaging_failures_remaining`` makes the next N sends fail, so
the worker's retry and resume behaviour can be demonstrated and reproduced without
touching any real customer.
"""
import time
import uuid

from .models import SimulationControl


class MessagingUnavailable(Exception):
    """The provider could not accept the message. Retryable."""


class MessagingPermanentError(Exception):
    """The provider rejected the message outright. Retrying will not help."""


def send_message(customer, content, channel=''):
    """Send one message. Raises on failure; never silently drops."""
    if not content or not content.strip():
        raise MessagingPermanentError('Refusing to send an empty message.')

    control = SimulationControl.current()

    if control.messaging_failures_remaining > 0:
        control.messaging_failures_remaining -= 1
        control.save(update_fields=['messaging_failures_remaining', 'updated_at'])
        raise MessagingUnavailable(
            f'Simulated provider unavailable (outage injected, '
            f'{control.messaging_failures_remaining} failure(s) left).'
        )

    if control.messaging_latency_ms:
        time.sleep(min(control.messaging_latency_ms, 5000) / 1000)

    return {
        'provider': 'simulated',
        'message_id': f'sim-{uuid.uuid4().hex[:12]}',
        'to': customer.phone,
        'channel': channel or customer.preferred_channel,
        'status': 'DELIVERED',
    }
