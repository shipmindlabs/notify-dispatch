"""Tests for quiet hours read on the recipient's own clock."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest
from conftest import CONTEXT, ORDER_CONFIRMED, Recorder

from notify_dispatch import (
    Channel,
    QuietHours,
    QuietHoursError,
    Recipient,
    SmsAdapter,
    Urgency,
)

TOKYO = timezone(timedelta(hours=9))
LATE_IN_UTC = datetime(2026, 3, 1, 23, 0, tzinfo=timezone.utc)
NIGHT_IN_TOKYO = QuietHours(start=time(22, 0), end=time(7, 0), tz=TOKYO)
NIGHT_IN_UTC = QuietHours(start=time(22, 0), end=time(7, 0), tz=timezone.utc)


def test_an_aware_moment_is_converted_before_the_clock_is_read():
    # 23:00 UTC is 08:00 in Tokyo, so the night there is already over
    assert not NIGHT_IN_TOKYO.covers(LATE_IN_UTC)
    assert NIGHT_IN_TOKYO.covers(LATE_IN_UTC - timedelta(hours=2))


def test_the_same_moment_silences_a_window_on_another_clock():
    assert NIGHT_IN_UTC.covers(LATE_IN_UTC)
    assert NIGHT_IN_UTC.tz is timezone.utc


def test_a_send_outside_the_recipients_night_is_not_silenced(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))
    recipient = Recipient(phone="+81", quiet_hours=NIGHT_IN_TOKYO)

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, now=LATE_IN_UTC)

    assert receipt.channel is Channel.SMS
    assert len(sms.calls) == 1


def test_a_silenced_send_reports_until_in_the_windows_zone(make_dispatcher):
    dispatcher = make_dispatcher(SmsAdapter(Recorder()))
    recipient = Recipient(phone="+491", quiet_hours=NIGHT_IN_UTC)

    with pytest.raises(QuietHoursError) as excinfo:
        dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, now=LATE_IN_UTC)

    assert excinfo.value.until == time(7, 0, tzinfo=timezone.utc)
    assert "07:00+00:00" in str(excinfo.value)


def test_an_urgent_send_still_overrides_a_zoned_window(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))
    recipient = Recipient(phone="+491", quiet_hours=NIGHT_IN_UTC)

    receipt = dispatcher.send(
        recipient,
        ORDER_CONFIRMED,
        CONTEXT,
        now=LATE_IN_UTC,
        urgency=Urgency.URGENT,
    )

    assert receipt.channel is Channel.SMS


def test_a_zoned_window_refuses_a_moment_that_names_no_zone():
    with pytest.raises(ValueError):
        NIGHT_IN_TOKYO.covers(datetime(2026, 3, 1, 23, 0))
    with pytest.raises(ValueError):
        NIGHT_IN_TOKYO.covers(time(23, 0))


def test_a_zoned_window_refuses_a_naive_dispatcher_clock(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))
    recipient = Recipient(phone="+81", quiet_hours=NIGHT_IN_TOKYO)

    with pytest.raises(ValueError):
        dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT)

    assert not sms.calls


def test_a_channel_the_window_leaves_alone_is_never_converted():
    window = QuietHours(
        start=time(22, 0), end=time(7, 0), channels=(Channel.PUSH,), tz=TOKYO
    )

    assert not window.silences(Channel.SMS, datetime(2026, 3, 1, 23, 0))


def test_a_window_without_a_tz_reads_the_moment_as_it_stands():
    window = QuietHours(start=time(22, 0), end=time(7, 0))

    assert window.covers(datetime(2026, 3, 1, 23, 0))
    assert window.covers(LATE_IN_UTC)
    assert window.covers(time(23, 0))
    assert not window.covers(time(8, 0))
    assert window.until == time(7, 0)
    assert window.until.tzinfo is None
