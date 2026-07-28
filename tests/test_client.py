"""StockX client hardening regressions (no network):
- audit 1.3: Retry-After must accept the RFC 9110 HTTP-date form
- audit 1.1: the rate limit is machine-global — two throttles sharing the
  lease file must pace against each other, not independently
"""
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from arb.stockx.client import (
    RETRY_AFTER_CAP_S,
    CrossProcessThrottle,
    _retry_after_seconds,
)


class TestRetryAfter:
    def test_delay_seconds(self):
        assert _retry_after_seconds("60") == 60.0
        assert _retry_after_seconds("12.5") == 12.5

    def test_http_date(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=30)
        got = _retry_after_seconds(format_datetime(when, usegmt=True))
        assert got is not None
        assert 25.0 <= got <= 31.0

    def test_absent_or_garbage(self):
        assert _retry_after_seconds(None) is None
        assert _retry_after_seconds("") is None
        assert _retry_after_seconds("soon-ish") is None

    def test_cap_exists_and_is_sane(self):
        # a corrupt/hostile header must not park the scanner for hours
        assert 0 < RETRY_AFTER_CAP_S <= 600


class TestCrossProcessThrottle:
    def test_second_caller_waits_out_the_interval(self, tmp_path):
        lease = str(tmp_path / "lease")
        a = CrossProcessThrottle(lease, 0.30)
        b = CrossProcessThrottle(lease, 0.30)  # separate instance, shared file
        a._wait_turn_blocking()
        t0 = time.monotonic()
        b._wait_turn_blocking()
        assert time.monotonic() - t0 >= 0.25

    def test_no_wait_after_interval_elapsed(self, tmp_path):
        lease = str(tmp_path / "lease")
        a = CrossProcessThrottle(lease, 0.10)
        a._wait_turn_blocking()
        time.sleep(0.15)
        t0 = time.monotonic()
        a._wait_turn_blocking()
        assert time.monotonic() - t0 < 0.10

    def test_corrupt_lease_file_recovers(self, tmp_path):
        lease = tmp_path / "lease"
        lease.write_bytes(b"not-a-float")
        CrossProcessThrottle(str(lease), 0.05)._wait_turn_blocking()
        float(lease.read_bytes())  # healed to a valid stamp
