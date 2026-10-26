"""Remaining-runtime estimate from the observed discharge rate.

The rate is measured, not assumed: every time the reported percent drops, the
time since the previous drop gives "seconds per percent" for that stretch.
Only the most recent stretches count, because the drain depends on how the
mouse is used and configured -- polling rate (1K vs 8K), DPI, motion sync,
how long it sits idle -- and any of those can change at any moment.

Two rules keep the number honest after such a change:

* **Regime change.** When the latest stretches disagree with the older ones by
  more than :data:`_REGIME_FACTOR` (say, the polling rate went from 1K to 8K),
  the older ones are dropped and the estimate follows the new drain at once.
* **Open stretch.** If the current percent has already lasted longer than the
  estimate assumes, that elapsed time joins the median as one more sample, so
  an idle mouse nudges the number up without one quiet hour dominating it.

Gaps in observation -- PC asleep or off, mouse switched off, app not running --
never count as discharge time: a gap longer than :data:`_MAX_GAP_S` restarts
the current stretch. A mouse that dozes off is not a gap: it is still on
battery, so :meth:`DischargeEstimator.touch` keeps the stretch open.

The percent is a voltage reading and flickers -- 80, 79, 80, 79 at every
boundary. A reading above the lowest one seen is that jitter and is ignored;
only a jump of :data:`_RECHARGE_JUMP` or more (charged while the app was not
looking) restarts the stretch. Restarting on every flicker would leave each
drop "the first one after a restart", which is never timed, so the estimate
would never appear.
"""

from __future__ import annotations

#: Stretches kept for the estimate (most recent last).
_WINDOW = 6
#: Stretches needed before any estimate is shown.
_MIN_STRETCHES = 2
#: Latest stretches this many times faster/slower than the older median mean
#: the usage changed: the older ones are discarded.
_REGIME_FACTOR = 1.8
#: Longer than this without a reading is a gap, not discharge time.
_MAX_GAP_S = 10 * 60
#: A reading this far above the lowest one seen is a missed recharge, not jitter.
_RECHARGE_JUMP = 5


class DischargeEstimator:
    """Feed it readings; ask it for the remaining seconds."""

    def __init__(self, stretches: list[float] | None = None):
        #: Seconds per percent, oldest first.
        self.stretches: list[float] = list(stretches or [])[-_WINDOW:]
        self._last_percent: int | None = None
        self._stretch_start: float | None = None  # when _last_percent was first seen
        self._last_seen: float | None = None

    def reset(self) -> None:
        """Forget the open stretch (charging, mouse switch); keep the history."""
        self._last_percent = None
        self._stretch_start = None
        self._last_seen = None

    def clear(self) -> None:
        """Forget everything, history included (a fresh charge cycle)."""
        self.reset()
        self.stretches.clear()

    def touch(self, now: float) -> None:
        """A reading without a level (the mouse is asleep): not a gap."""
        if self._last_seen is not None and now - self._last_seen <= _MAX_GAP_S:
            self._last_seen = now

    def add(self, now: float, percent: int) -> bool:
        """Record a discharging reading; ``True`` when a new stretch was measured."""
        gap = self._last_seen is not None and now - self._last_seen > _MAX_GAP_S
        self._last_seen = now
        if gap or self._last_percent is None or percent >= self._last_percent + _RECHARGE_JUMP:
            # First reading, a gap, or a recharge we did not see: the current
            # percent may have begun long before, so only the next drop starts
            # a stretch timed from its beginning.
            self._last_percent = percent
            self._stretch_start = None
            return False
        if percent >= self._last_percent:
            return False  # same level, or jitter above it: the lowest one stands

        dropped = self._last_percent - percent
        started = self._stretch_start
        self._last_percent, self._stretch_start = percent, now
        if started is None:
            return False  # first drop after a (re)start: its beginning was not seen
        self._push((now - started) / dropped)
        return True

    def _push(self, seconds_per_percent: float) -> None:
        self.stretches.append(seconds_per_percent)
        if len(self.stretches) >= 4:
            older = median(self.stretches[:-2])
            recent = self.stretches[-2:]
            if all(_differs(value, older) for value in recent):
                del self.stretches[:-2]
        del self.stretches[:-_WINDOW]

    def seconds_per_percent(self, now: float) -> float | None:
        if len(self.stretches) < _MIN_STRETCHES:
            return None
        estimate = median(self.stretches)
        if self._stretch_start is not None:
            open_for = now - self._stretch_start
            if open_for > estimate:
                # One more sample, not a floor: an idle hour nudges the
                # estimate, while a lasting slowdown wins once its drops land.
                estimate = median([*self.stretches, open_for])
        return estimate

    def remaining(self, now: float, percent: int) -> float | None:
        """Seconds of runtime left at ``percent``, or ``None`` while still learning."""
        rate = self.seconds_per_percent(now)
        if rate is None:
            return None
        elapsed = 0.0
        if self._stretch_start is not None:
            elapsed = min(now - self._stretch_start, rate)
        return max(0.0, percent * rate - elapsed)


def median(values: list[float]) -> float:
    """Median without ``statistics`` -- it imports ``decimal``, which the exe build excludes."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _differs(value: float, reference: float) -> bool:
    return value > reference * _REGIME_FACTOR or value * _REGIME_FACTOR < reference
