"""Self-tuning PID control for LocalTuya climate setpoint compensation.

The control problem
-------------------
* PV  (process value)  = the external "true" temperature sensor.
* SP  (set point)      = the temperature the user asked for in the UI.
* out (control output) = the *difference* between the set point SENT to the AC
                         and the temperature the AC reports from its own sensor
                         (``sent = ac_reported + out``).

A positive ``out`` tells the AC it is colder than it really is (pushing it to
heat / stop cooling); a negative ``out`` tells it it is warmer than it really is
(pushing it to cool harder).

Optional compressor power (watts) is used three ways when available:
  1. Conditional anti-wind-up - freeze the integral when the inverter is
     saturated (flat-out or at minimum speed), so error during actuator
     saturation does not wind up.
  2. Power cascade (opt-in) - an inner loop servos the offset so the measured
     compressor power tracks a target set by the (proportional+derivative)
     outer thermal loop. This wraps the AC's own controller/hysteresis in a
     fast inner power loop, the principled fix for two controllers fighting.
  3. The inner loop's integral inherently provides load feed-forward (it finds
     the power that holds setpoint), and the outer P+D eases power as the error
     shrinks - pre-empting overshoot without waiting for the slow thermal lag.

Tuning strategy (hybrid)
------------------------
1. Bootstrap with an Astrom-Hagglund relay-feedback experiment, then derive PID
   gains with the gentle Tyreus-Luyben rules.
2. Refine the gains passively from observed closed-loop behaviour (skipped while
   the actuator is saturated, since that error is not a tuning signal).

Tuning can be frozen ("stop tuning") to lock the current gains, and restarted
("re-tune").

Units: magnitudes named ``*_C`` are degrees Celsius, scaled to the working unit
at construction (``scale`` = 1.8 for Fahrenheit). Counts, ratios, fractions and
times are unit-independent.

Nothing here is HomeAssistant-specific so the controller stays unit-testable.
"""
import logging
import math

_LOGGER = logging.getLogger(__name__)

# Tuning lifecycle phases (also reported to the user as a sensor state).
PHASE_UNTUNED = "untuned"
PHASE_TUNING = "tuning"
PHASE_TUNED = "tuned"
STATUS_FROZEN = "frozen"  # tuned, but auto-tuning has been stopped

# Relay-feedback experiment.
RELAY_AMPLITUDE_C = 2.0  # deg C of commanded offset the relay swings by
RELAY_HYSTERESIS_C = 0.2  # deg C dead-band around SP to reject sensor noise
RELAY_MIN_SWITCHES = 4  # relay flips to observe before a clean fit is possible
RELAY_CONVERGE_TOLERANCE = 0.4  # max relative spread of *full* periods to accept
RELAY_MAX_SWITCHES = 9  # force-accept (median period) after this many - a real
# AC's cycles are irregular, so this guarantees tuning completes instead of
# running until the timeout.
RELAY_TIMEOUT = 14400.0  # seconds (4h); fall back to safe gains if never settles

# Passive refinement.
REFINE_WINDOW = 40  # samples per evaluation window
REFINE_STEP = 0.04  # fractional nudge applied to a gain per window
REFINE_MIN_FACTOR = 0.25  # gains stay within these multiples of the baseline
REFINE_MAX_FACTOR = 4.0
REFINE_BAND_C = 0.25  # deg C; |error| below this counts as "on target"

# Safety clamp on the commanded offset (also bounds integral wind-up).
OUTPUT_LIMIT_C = 8.0  # deg C

# Derivative low-pass filter time constant (seconds) to tame sensor noise.
DERIV_FILTER_TAU = 120.0

# Power handling.
POWER_SAT_FRACTION = 0.05  # within 5% of the observed min/max counts as saturated
CASCADE_GAIN = 0.15  # inner-loop effort step per unit power-fraction error
CASCADE_PROP_BAND_C = 1.5  # deg C error that maps to full compressor power
CASCADE_LEAD = 300.0  # seconds of predictive lead (overshoot pre-emption)
CASCADE_OUTER_TI = 1800.0  # seconds; slow outer integral that removes droop
STATUS_CASCADE = "cascade"

# Conservative gains used if the relay experiment fails to converge. Kp=1 with
# no integral/derivative reproduces the original static offset-matching scheme.
FALLBACK_GAINS = (1.0, 0.0, 0.0)

# Registry under ``hass.data[localtuya]`` that lets a climate entity and its
# companion switches/buttons (different platforms) share state.
OVERSHOOT_REGISTRY = "pid_overshoot"


def get_overshoot_state(hass, domain, dev_id, dp_id):
    """Return the shared companion state for one climate entity.

    A plain dict shared by the climate entity and its companion toggles/buttons
    (different platforms), created on first access so all of them find the same
    object regardless of setup order:

    * ``enabled``       - overshoot cutoff armed (default on)
    * ``true_auto``     - autonomous heat/cool/off mode active (default off)
    * ``power_cascade`` - inner power-tracking loop active (default off)
    * ``hysteresis``    - cycling deadband around setpoint (None => entity default)
    * ``climate``       - back-reference to the climate entity
    """
    registry = hass.data[domain].setdefault(OVERSHOOT_REGISTRY, {})
    return registry.setdefault(
        f"{dev_id}_{dp_id}",
        {
            "enabled": True,
            "true_auto": False,
            "power_cascade": False,
            "hysteresis": None,
            "climate": None,
        },
    )


def _clamp_factor(factor):
    return max(REFINE_MIN_FACTOR, min(REFINE_MAX_FACTOR, factor))


class PowerMonitor:
    """Tracks an inverter's compressor power and reports level/saturation.

    The observed min/max adapt slowly (``POWER_LEAK``) so the normalised level
    stays meaningful across seasons without a one-off spike pinning the range.
    """

    def __init__(self):
        self.value = None
        self._min = None
        self._max = None

    def reset(self):
        self.value = None
        self._min = None
        self._max = None

    def restore(self, minimum, maximum):
        """Seed the learned range from persisted state (value stays unknown)."""
        if minimum is None or maximum is None or maximum < minimum:
            return
        self._min = minimum
        self._max = maximum

    def update(self, power):
        if power is None:
            return
        self.value = power
        # Running extremes (reset on re-tune); kept simple so a steady power
        # cannot collapse the observed span.
        self._max = power if self._max is None else max(self._max, power)
        self._min = power if self._min is None else min(self._min, power)

    @property
    def minimum(self):
        return self._min

    @property
    def maximum(self):
        return self._max

    @property
    def span(self):
        if self._min is None:
            return 0.0
        return self._max - self._min

    @property
    def level(self):
        """Normalised 0..1 compressor level, or None if not yet characterised.

        Requires the observed range to be a meaningful fraction of the max, so a
        freshly-started (tiny-span) range cannot produce a misleading level that
        would make the cascade slam the offset or trip false saturation.
        """
        if self.value is None or self._max is None:
            return None
        if self.span < 0.1 * max(self._max, 1e-9):
            return None
        return max(0.0, min(1.0, (self.value - self._min) / self.span))

    @property
    def saturated_high(self):
        lv = self.level
        return lv is not None and lv >= 1.0 - POWER_SAT_FRACTION

    @property
    def saturated_low(self):
        lv = self.level
        return lv is not None and lv <= POWER_SAT_FRACTION

    @property
    def saturation(self):
        if self.level is None:
            return None
        if self.saturated_high:
            return "high"
        if self.saturated_low:
            return "low"
        return "none"


class PIDController:
    """Discrete PID with conditional anti-wind-up and filtered derivative-on-PV.

    The derivative acts on the measurement (not the error) and is low-pass
    filtered, so a setpoint change produces no derivative kick and sensor noise
    is not amplified by a large Kd.
    """

    def __init__(
        self,
        kp=0.0,
        ki=0.0,
        kd=0.0,
        output_limit=OUTPUT_LIMIT_C,
        deriv_filter_tau=DERIV_FILTER_TAU,
    ):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.output_limit = output_limit
        self.deriv_filter_tau = deriv_filter_tau
        self.integral = 0.0
        self.last_pv = None
        self._deriv = 0.0
        self.output = 0.0

    def set_gains(self, kp, ki, kd):
        self.kp = kp
        self.ki = ki
        self.kd = kd

    def reset(self):
        self.integral = 0.0
        self.last_pv = None
        self._deriv = 0.0
        self.output = 0.0

    def reset_derivative(self):
        self.last_pv = None
        self._deriv = 0.0

    def _clamp_integral(self):
        if self.output_limit and self.ki:
            limit = self.output_limit / abs(self.ki)
            self.integral = max(-limit, min(limit, self.integral))

    def _filtered_derivative(self, pv, dt):
        if self.last_pv is None:
            raw = 0.0
        else:
            raw = (pv - self.last_pv) / dt
        self.last_pv = pv
        alpha = dt / (self.deriv_filter_tau + dt)
        self._deriv += alpha * (raw - self._deriv)
        return self._deriv

    def update(self, error, pv, dt, freeze_increase=False, freeze_decrease=False):
        """Advance by ``dt`` seconds and return the output.

        ``freeze_increase``/``freeze_decrease`` block integral accumulation in
        that direction (conditional anti-wind-up driven by actuator saturation).
        """
        if dt <= 0:
            dt = 1e-3
        delta = error * dt
        # Only integrate when Ki is active and not blocked by actuator saturation.
        if self.ki and not (
            (delta > 0 and freeze_increase) or (delta < 0 and freeze_decrease)
        ):
            self.integral += delta
        self._clamp_integral()

        derivative_term = -self.kd * self._filtered_derivative(pv, dt)
        output = self.kp * error + self.ki * self.integral + derivative_term
        output = max(-self.output_limit, min(self.output_limit, output))
        self.output = output
        return output

    def pd_term(self, error, pv, dt):
        """Proportional+derivative only (used as the cascade outer loop)."""
        if dt <= 0:
            dt = 1e-3
        return self.kp * error - self.kd * self._filtered_derivative(pv, dt)


class RelayAutoTuner:
    """Astrom-Hagglund relay-feedback auto-tuner.

    Drive ``output()`` from the control loop while active; it returns a relay
    (bang-bang) offset that forces a limit cycle, then publishes Tyreus-Luyben
    gains via ``result``. Stability is judged on *full* periods so an asymmetric
    cycle (slow cool-down, fast reheat) still converges.
    """

    def __init__(
        self,
        amplitude=RELAY_AMPLITUDE_C,
        hysteresis=RELAY_HYSTERESIS_C,
        min_switches=RELAY_MIN_SWITCHES,
    ):
        self.amplitude = amplitude
        self.hysteresis = hysteresis
        self.min_switches = min_switches
        self._relay = -1  # start assuming a cooling demand
        self._elapsed = 0.0
        self._half_peak = 0.0
        self._switch_times = []
        self._peaks = []
        self.result = None

    @property
    def elapsed(self):
        return self._elapsed

    @property
    def progress(self):
        switches = len(self._switch_times)
        needed = self.min_switches + 1
        remaining_switches = max(0, needed - switches)
        cycles_remaining = (remaining_switches + 1) // 2

        half_period = None
        if switches >= 2:
            recent = self._switch_times[-needed:]
            diffs = [b - a for a, b in zip(recent, recent[1:])]
            if diffs:
                half_period = sum(diffs) / len(diffs)

        eta = (
            remaining_switches * half_period
            if half_period is not None and remaining_switches > 0
            else None
        )
        return {
            "switches": switches,
            "cycles_remaining": cycles_remaining,
            "eta": eta,
            "half_period": half_period,
        }

    def output(self, error, dt):
        self._elapsed += dt
        self._half_peak = max(self._half_peak, abs(error))

        switched = False
        if self._relay > 0 and error < -self.hysteresis:
            self._relay = -1
            switched = True
        elif self._relay < 0 and error > self.hysteresis:
            self._relay = 1
            switched = True

        if switched:
            self._switch_times.append(self._elapsed)
            self._peaks.append(self._half_peak)
            self._half_peak = 0.0
            self._try_fit()

        return self._relay * self.amplitude

    def _try_fit(self):
        if len(self._switch_times) <= self.min_switches:
            return
        recent = self._switch_times[-(self.min_switches + 1):]
        full_periods = [recent[i + 2] - recent[i] for i in range(len(recent) - 2)]
        recent_peaks = self._peaks[-self.min_switches:]
        if not full_periods or not recent_peaks:
            return
        mean_period = sum(full_periods) / len(full_periods)
        mean_peak = sum(recent_peaks) / len(recent_peaks)
        if mean_period <= 0 or mean_peak <= 1e-6:
            return

        spread = max(full_periods) - min(full_periods)
        converged = spread / mean_period <= RELAY_CONVERGE_TOLERANCE
        forced = len(self._switch_times) >= RELAY_MAX_SWITCHES
        if not (converged or forced):
            return

        # A clean limit cycle uses the mean; a forced accept (irregular cycles)
        # uses the median period, which is robust to the odd long/short cycle.
        if converged:
            period = mean_period
        else:
            ordered = sorted(full_periods)
            mid = len(ordered) // 2
            period = (
                ordered[mid]
                if len(ordered) % 2
                else (ordered[mid - 1] + ordered[mid]) / 2
            )

        ultimate_gain = (4.0 * self.amplitude) / (math.pi * mean_peak)
        self.result = self._tyreus_luyben(ultimate_gain, period)
        _LOGGER.debug(
            "Relay tuning %s: Ku=%.3f Tu=%.1fs -> Kp=%.3f Ki=%.5f Kd=%.3f",
            "converged" if converged else "force-accepted",
            ultimate_gain,
            period,
            *self.result,
        )

    @staticmethod
    def _tyreus_luyben(ku, tu):
        kp = ku / 2.2
        ti = 2.2 * tu
        td = tu / 6.3
        ki = kp / ti if ti else 0.0
        kd = kp * td
        return (kp, ki, kd)


class PassiveRefiner:
    """Heuristic online refinement of the gains around a fixed baseline."""

    def __init__(self, band=REFINE_BAND_C):
        self._base = None
        self._band = band
        self._kp_factor = 1.0
        self._ki_factor = 1.0
        self._errors = []

    def set_baseline(self, kp, ki, kd):
        self._base = (kp, ki, kd)
        self._kp_factor = 1.0
        self._ki_factor = 1.0
        self._errors = []

    @property
    def baseline(self):
        return self._base

    @property
    def kp_factor(self):
        return self._kp_factor

    @property
    def ki_factor(self):
        return self._ki_factor

    def restore(self, base, kp_factor, ki_factor):
        self._base = base
        self._kp_factor = _clamp_factor(kp_factor)
        self._ki_factor = _clamp_factor(ki_factor)
        self._errors = []

    def apply(self, pid):
        if self._base is None:
            return
        base_kp, base_ki, base_kd = self._base
        pid.set_gains(
            base_kp * self._kp_factor,
            base_ki * self._ki_factor,
            base_kd,
        )

    def observe(self, error, pid):
        if self._base is None:
            return
        self._errors.append(error)
        if len(self._errors) < REFINE_WINDOW:
            return

        window = self._errors
        self._errors = []
        band = self._band

        amplitude = max(window) - min(window)
        mean_error = sum(window) / len(window)
        sign_changes = sum(
            1
            for a, b in zip(window, window[1:])
            if (a > 0) != (b > 0) and abs(a) > band
        )

        if sign_changes >= len(window) // 4 and amplitude > 2 * band:
            self._kp_factor *= 1.0 - REFINE_STEP
            self._ki_factor *= 1.0 - REFINE_STEP
        elif abs(mean_error) > band and sign_changes <= 1:
            self._kp_factor *= 1.0 + REFINE_STEP
            self._ki_factor *= 1.0 + REFINE_STEP

        self._kp_factor = _clamp_factor(self._kp_factor)
        self._ki_factor = _clamp_factor(self._ki_factor)
        self.apply(pid)


class SelfTuningPID:
    """Relay bootstrap -> PID with passive refinement, anti-wind-up and cascade.

    ``mode_sign`` is -1 for a cooling controller, +1 for heating: it maps
    "more compressor effort" to the offset direction and to integral freezing.
    """

    def __init__(self, scale=1.0, mode_sign=-1):
        self.scale = scale
        self.mode_sign = mode_sign
        self.pid = PIDController(output_limit=OUTPUT_LIMIT_C * scale)
        self.refiner = PassiveRefiner(band=REFINE_BAND_C * scale)
        self.tuner = self._new_tuner()
        self.power = PowerMonitor()
        self.phase = PHASE_UNTUNED
        self.tuning_enabled = True
        self.error = 0.0
        self.output = 0.0
        self._cascade_prop_band = CASCADE_PROP_BAND_C * scale
        self._cascade_effort = None  # lazily seeded from measured power
        self._cascade_int = 0.0  # outer-loop integral (fraction units)
        self._cascade_target = None
        self._cascade_meas = None
        self._last_cascade = False

    def _new_tuner(self):
        return RelayAutoTuner(
            amplitude=RELAY_AMPLITUDE_C * self.scale,
            hysteresis=RELAY_HYSTERESIS_C * self.scale,
        )

    @property
    def tuning(self):
        return self.phase in (PHASE_UNTUNED, PHASE_TUNING)

    @property
    def relay_tuning(self):
        """Whether the disruptive relay experiment is actively driving output.

        The relay deliberately overshoots to excite a limit cycle, so the
        overshoot cutoff must stand down while it runs. It only runs outside the
        power cascade (cascade is tuning-free), hence the ``_last_cascade`` guard.
        """
        return self.tuning and self.tuning_enabled and not self._last_cascade

    @property
    def status(self):
        if self._last_cascade:
            return STATUS_CASCADE
        if self.phase == PHASE_TUNED and not self.tuning_enabled:
            return STATUS_FROZEN
        return self.phase

    def reset(self):
        self.pid.reset()
        self._cascade_effort = None
        self._cascade_int = 0.0

    def reset_derivative(self):
        self.pid.reset_derivative()

    def retune(self):
        """Discard tuning and restart the relay bootstrap (re-enables tuning)."""
        self.tuner = self._new_tuner()
        self.refiner = PassiveRefiner(band=REFINE_BAND_C * self.scale)
        self.power.reset()
        self.pid.reset()
        self.tuning_enabled = True
        self._cascade_effort = None
        self._cascade_int = 0.0
        self.phase = PHASE_UNTUNED

    def stop_tuning(self):
        """Freeze gains: stop the relay experiment and passive refinement."""
        if self.tuning:
            # Nothing converged yet - lock in safe gains so control continues.
            self._apply_gains(*FALLBACK_GAINS)
        self.tuning_enabled = False

    def _apply_gains(self, kp, ki, kd):
        self.pid.set_gains(kp, ki, kd)
        self.pid.reset()
        self.refiner.set_baseline(kp, ki, kd)
        self.phase = PHASE_TUNED

    def _saturation_freezes(self):
        """Return (freeze_increase, freeze_decrease) from power saturation.

        Freeze integration that would push effort past a saturated actuator
        rail: at max power do not demand more conditioning; at min power do not
        demand less.
        """
        sat_high = self.power.saturated_high
        sat_low = self.power.saturated_low
        freeze_decrease = (self.mode_sign < 0 and sat_high) or (
            self.mode_sign > 0 and sat_low
        )
        freeze_increase = (self.mode_sign > 0 and sat_high) or (
            self.mode_sign < 0 and sat_low
        )
        return freeze_increase, freeze_decrease

    def compute(self, sp, pv, dt, power=None, cascade=False):
        """Return the commanded offset (degrees) for one control step."""
        self.power.update(power)
        error = sp - pv
        self.error = error
        self._last_cascade = cascade

        # Power cascade is self-contained (a predictive proportional band feeding
        # an integrating inner power loop) and needs no relay experiment, so it
        # takes over before tuning - no disruptive oscillation in cascade mode.
        if cascade:
            output = self._compute_cascade(error, pv, dt)
            self.output = output
            return output

        if self.tuning and self.tuning_enabled:
            self.phase = PHASE_TUNING
            output = self.tuner.output(error, dt)
            if self.tuner.result is not None:
                self._apply_gains(*self.tuner.result)
            elif self.tuner.elapsed > RELAY_TIMEOUT:
                _LOGGER.warning(
                    "Relay tuning timed out after %.0fs; using fallback gains",
                    self.tuner.elapsed,
                )
                self._apply_gains(*FALLBACK_GAINS)
            self.output = output
            return output

        freeze_increase, freeze_decrease = self._saturation_freezes()
        output = self.pid.update(error, pv, dt, freeze_increase, freeze_decrease)
        # Do not refine on error caused by actuator saturation - it is not a
        # tuning signal.
        if self.tuning_enabled and not (
            self.power.saturated_high or self.power.saturated_low
        ):
            self.refiner.observe(error, self.pid)
        self._cascade_effort = None  # so a later cascade re-seeds cleanly
        self.output = output
        return output

    def _compute_cascade(self, error, pv, dt):
        """Predictive proportional band (temp -> power target) + inner power loop.

        Tuning-independent: the inner integrator finds whatever offset achieves
        the target power (absorbing the AC's hysteresis/bias and the heat load),
        and the predictive lead eases power as the room approaches setpoint.
        """
        limit = self.pid.output_limit
        rate = self.pid._filtered_derivative(pv, dt)  # deg/s, low-pass filtered
        predicted_error = error - CASCADE_LEAD * rate  # project ahead
        demand = self.mode_sign * predicted_error  # >0 => condition harder
        p_term = demand / self._cascade_prop_band
        target_raw = p_term + self._cascade_int
        target = max(0.0, min(1.0, target_raw))
        # Slow outer integral removes the proportional droop (load-dependent
        # steady-state offset); conditional integration only while unsaturated.
        if 0.0 < target_raw < 1.0:
            self._cascade_int += p_term * (dt / CASCADE_OUTER_TI)
            self._cascade_int = max(-1.0, min(1.0, self._cascade_int))
        self._cascade_target = target

        meas = self.power.level
        self._cascade_meas = meas
        if meas is None:
            # Power range not characterised yet: act as a proportional offset
            # controller on the *signed* demand. ``target_raw`` goes negative once
            # the room passes setpoint, which must push the offset positive (raise
            # the sent setpoint) to idle the AC instead of stalling at zero.
            self._cascade_effort = max(-limit, min(limit, target_raw * limit))
        else:
            if self._cascade_effort is None:
                self._cascade_effort = meas * limit  # seed without a jump
            self._cascade_effort += CASCADE_GAIN * limit * (target - meas)
            # Allow the offset past zero into the backing-off direction. A power
            # target of 0 alone will NOT stop a hysteretic AC (its own sensor sits
            # at the sent setpoint, so it keeps modulating); the inner loop must be
            # free to raise the sent setpoint above the AC's reading to force idle.
            self._cascade_effort = max(-limit, min(limit, self._cascade_effort))
        return self.mode_sign * self._cascade_effort

    @property
    def report(self):
        report = {
            "phase": self.phase,
            "status": self.status,
            "tuning_enabled": self.tuning_enabled,
            "kp": self.pid.kp,
            "ki": self.pid.ki,
            "kd": self.pid.kd,
            "error": self.error,
            "integral": self.pid.integral,
            "output": self.output,
            "tuning_cycles_remaining": None,
            "tuning_eta": None,
            "power_level": self.power.value,
            "power_pct": None if self.power.level is None else self.power.level * 100.0,
            "power_min": self.power.minimum,
            "power_max": self.power.maximum,
            "power_saturation": self.power.saturation,
            "cascade_target": (
                None if self._cascade_target is None else self._cascade_target * 100.0
            ),
            "cascade_integral": (
                self._cascade_int * 100.0 if self._last_cascade else None
            ),
        }
        if self.tuning:
            progress = self.tuner.progress
            report["tuning_cycles_remaining"] = progress["cycles_remaining"]
            report["tuning_eta"] = progress["eta"]
        return report

    def snapshot(self):
        snap = {
            "phase": self.phase,
            "tuning_enabled": self.tuning_enabled,
            "kp_factor": self.refiner.kp_factor,
            "ki_factor": self.refiner.ki_factor,
        }
        base = self.refiner.baseline
        if base is not None:
            snap["base_kp"], snap["base_ki"], snap["base_kd"] = base
        if self.power.minimum is not None:
            snap["power_min"] = self.power.minimum
            snap["power_max"] = self.power.maximum
        return snap

    def restore(self, data):
        if not data:
            return
        # The learned power range is telemetry - restore it regardless of tuning
        # state so the cascade/saturation logic need not relearn the compressor's
        # range (and risk a narrow-range cold start) after every restart.
        try:
            pmin = data.get("power_min")
            pmax = data.get("power_max")
            if pmin is not None and pmax is not None:
                self.power.restore(float(pmin), float(pmax))
        except (TypeError, ValueError):
            pass

        if data.get("phase") != PHASE_TUNED:
            return
        try:
            base = (
                float(data["base_kp"]),
                float(data["base_ki"]),
                float(data["base_kd"]),
            )
        except (KeyError, TypeError, ValueError):
            try:
                base = (float(data["kp"]), float(data["ki"]), float(data["kd"]))
            except (KeyError, TypeError, ValueError):
                return
        try:
            kp_factor = float(data.get("kp_factor", 1.0))
            ki_factor = float(data.get("ki_factor", 1.0))
        except (TypeError, ValueError):
            kp_factor = ki_factor = 1.0

        self.refiner.restore(base, kp_factor, ki_factor)
        self.refiner.apply(self.pid)
        self.pid.reset()
        self.tuning_enabled = bool(data.get("tuning_enabled", True))
        self.phase = PHASE_TUNED
