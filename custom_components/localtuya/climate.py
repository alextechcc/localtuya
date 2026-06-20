"""Platform to locally control Tuya-based climate devices."""
import asyncio
import logging
from datetime import timedelta

from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

import voluptuous as vol
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.components.climate import (
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DOMAIN,
    ClimateEntity,
)
from homeassistant.components.climate.const import (
    HVACAction,
    HVACMode,
    PRESET_AWAY,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_NONE,
    PRESET_SLEEP,
    ClimateEntityFeature,
    FAN_AUTO,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_HIGH,
    FAN_TOP,
    SWING_ON,
    SWING_OFF,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_DEVICE_ID,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_ID,
    CONF_PLATFORM,
    CONF_TEMPERATURE_UNIT,
    PRECISION_HALVES,
    PRECISION_TENTHS,
    PRECISION_WHOLE,
    UnitOfTemperature,
)

from .common import LocalTuyaEntity, get_dps_for_platform
from .const import (
    CONF_CURRENT_TEMPERATURE_DP,
    CONF_TEMP_MAX,
    CONF_TEMP_MIN,
    CONF_ECO_DP,
    CONF_ECO_VALUE,
    CONF_HEURISTIC_ACTION,
    CONF_HVAC_ACTION_DP,
    CONF_HVAC_ACTION_SET,
    CONF_HVAC_MODE_DP,
    CONF_HVAC_MODE_SET,
    CONF_MAX_TEMP_DP,
    CONF_MIN_TEMP_DP,
    CONF_PRECISION,
    CONF_PRESET_DP,
    CONF_PRESET_SET,
    CONF_TARGET_PRECISION,
    CONF_TARGET_TEMPERATURE_DP,
    CONF_TEMPERATURE_STEP,
    CONF_HVAC_FAN_MODE_DP,
    CONF_HVAC_FAN_MODE_SET,
    CONF_HVAC_SWING_MODE_DP,
    CONF_HVAC_SWING_MODE_SET,
    CONF_MODEL,
    CONF_PROTOCOL_VERSION,
    CONF_SLEEP_DP,
    CONF_TRUE_TEMPERATURE_ENTITY,
    CONF_POWER_LEVEL_ENTITY,
    DOMAIN as LOCALTUYA_DOMAIN,
    TUYA_DEVICES,
)
from .pid import (
    SelfTuningPID,
    get_overshoot_state,
)

_LOGGER = logging.getLogger(__name__)

# The self-tuning PID re-evaluates the commanded offset on this cadence rather
# than only when a new temperature reading arrives. A slow plant (a sluggish
# window AC) does not need fast sampling, and slower sampling reduces derivative
# noise and actuator chatter.
PID_SAMPLE_TIME = 60  # seconds

# Minimum time the unit must stay off (or on) before the overshoot cutoff may
# toggle power again - protects the compressor from short-cycling.
MIN_CYCLE_TIME = 180  # seconds

# The cutoff only fires when the commanded offset opposes the mode by at least
# this margin (deg C, unit-scaled), so noise near the setpoint cannot toggle it.
CUTOFF_DEADBAND_C = 0.5

# "True Auto": software heat/cool/off control. Band around the setpoint within
# which neither heating nor cooling runs (deg C, unit-scaled).
AUTO_DEADBAND_C = 0.5

# Minimum time between heat<->cool reversals in True Auto, to protect the
# reversing valve / compressor from rapid mode flipping.
AUTO_MODE_SWITCH_MIN_TIME = 900  # seconds (15 min)

HVAC_MODE_SETS = {
    "manual/auto": {
        HVACMode.HEAT: "manual",
        HVACMode.AUTO: "auto",
    },
    "Manual/Auto": {
        HVACMode.HEAT: "Manual",
        HVACMode.AUTO: "Auto",
    },
    "MANUAL/AUTO": {
        HVACMode.HEAT: "MANUAL",
        HVACMode.AUTO: "AUTO",
    },
    "Manual/Program": {
        HVACMode.HEAT: "Manual",
        HVACMode.AUTO: "Program",
    },
    "m/p": {
        HVACMode.HEAT: "m",
        HVACMode.AUTO: "p",
    },
    "True/False": {
        HVACMode.HEAT: True,
    },
    "Auto/Cold/Dry/Wind/Hot": {
        HVACMode.HEAT: "hot",
        HVACMode.FAN_ONLY: "wind",
        HVACMode.DRY: "wet",
        HVACMode.COOL: "cold",
        HVACMode.AUTO: "auto",
    },
    "Auto/Cold/Dry/Wind/Hot/Eco": {
        HVACMode.HEAT: "hot",
        HVACMode.FAN_ONLY: "wind",
        HVACMode.DRY: "wet",
        HVACMode.COOL: "cold",
        HVACMode.AUTO: "auto",
        "eco": "eco",
    },
    "Hot/Cold/Dry/Wind/Eco": {
        HVACMode.HEAT: "hot",
        HVACMode.COOL: "cold",
        HVACMode.DRY: "wet",
        HVACMode.FAN_ONLY: "wind",
        "Eco": "eco",
    },
    "Hot/Cold/Dry/Wind": {
        HVACMode.HEAT: "hot",
        HVACMode.COOL: "cold",
        HVACMode.DRY: "wet",
        HVACMode.FAN_ONLY: "wind",
        HVACMode.AUTO: "auto",
    },
    "Cold/Dehumidify/Hot": {
        HVACMode.HEAT: "hot",
        HVACMode.DRY: "dehumidify",
        HVACMode.COOL: "cold",
    },
    "1/0": {
        HVACMode.HEAT: "1",
        HVACMode.AUTO: "0",
    },
}
HVAC_ACTION_SETS = {
    "True/False": {
        HVACAction.HEATING: True,
        HVACAction.IDLE: False,
    },
    "open/close": {
        HVACAction.HEATING: "open",
        HVACAction.IDLE: "close",
    },
    "heating/no_heating": {
        HVACAction.HEATING: "heating",
        HVACAction.IDLE: "no_heating",
    },
    "Heat/Warming": {
        HVACAction.HEATING: "Heat",
        HVACAction.IDLE: "Warming",
    },
    "heating/warming": {
        HVACAction.HEATING: "heating",
        HVACAction.IDLE: "warming",
    },
}
HVAC_FAN_MODE_SETS = {
    "Auto/Low/Middle/High/Strong": {
        FAN_AUTO: "auto",
        FAN_LOW: "low",
        FAN_MEDIUM: "middle",
        FAN_HIGH: "high",
        FAN_TOP: "strong",
    },
    "Low/High/Silent": {
        FAN_LOW: "mute",
        FAN_MEDIUM: "low",
        FAN_HIGH: "high",
    },
}
HVAC_SWING_MODE_SETS = {
    "True/False": {
        SWING_ON: True,
        SWING_OFF: False,
    }
}
PRESET_SETS = {
    "Manual/Holiday/Program": {
        PRESET_AWAY: "Holiday",
        PRESET_HOME: "Program",
        PRESET_NONE: "Manual",
    },
    "smart/holiday/hold": {
        PRESET_AWAY: "holiday",
        PRESET_HOME: "smart",
        PRESET_NONE: "hold",
    },
}

TEMPERATURE_CELSIUS = "celsius"
TEMPERATURE_FAHRENHEIT = "fahrenheit"
DEFAULT_TEMPERATURE_UNIT = TEMPERATURE_CELSIUS
DEFAULT_PRECISION = PRECISION_TENTHS
DEFAULT_TEMPERATURE_STEP = PRECISION_HALVES
# Empirically tested to work for AVATTO thermostat
MODE_WAIT = 0.1


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        vol.Optional(CONF_TARGET_TEMPERATURE_DP): vol.In(dps),
        vol.Optional(CONF_CURRENT_TEMPERATURE_DP): vol.In(dps),
        vol.Optional(CONF_TEMPERATURE_STEP, default=PRECISION_WHOLE): vol.In(
            [PRECISION_WHOLE, PRECISION_HALVES, PRECISION_TENTHS]
        ),
        vol.Optional(CONF_TEMP_MIN, default=DEFAULT_MIN_TEMP): vol.Coerce(float),
        vol.Optional(CONF_TEMP_MAX, default=DEFAULT_MAX_TEMP): vol.Coerce(float),
        vol.Optional(CONF_MAX_TEMP_DP): vol.In(dps),
        vol.Optional(CONF_MIN_TEMP_DP): vol.In(dps),
        vol.Optional(CONF_PRECISION, default=PRECISION_WHOLE): vol.In(
            [PRECISION_WHOLE, PRECISION_HALVES, PRECISION_TENTHS]
        ),
        vol.Optional(CONF_HVAC_MODE_DP): vol.In(dps),
        vol.Optional(CONF_HVAC_MODE_SET): vol.In(list(HVAC_MODE_SETS.keys())),
        vol.Optional(CONF_HVAC_FAN_MODE_DP): vol.In(dps),
        vol.Optional(CONF_HVAC_FAN_MODE_SET): vol.In(list(HVAC_FAN_MODE_SETS.keys())),
        vol.Optional(CONF_HVAC_ACTION_DP): vol.In(dps),
        vol.Optional(CONF_HVAC_ACTION_SET): vol.In(list(HVAC_ACTION_SETS.keys())),
        vol.Optional(CONF_ECO_DP): vol.In(dps),
        vol.Optional(CONF_ECO_VALUE): str,
        vol.Optional(CONF_SLEEP_DP): vol.In(dps),
        vol.Optional(CONF_PRESET_DP): vol.In(dps),
        vol.Optional(CONF_PRESET_SET): vol.In(list(PRESET_SETS.keys())),
        vol.Optional(CONF_TEMPERATURE_UNIT): vol.In(
            [TEMPERATURE_CELSIUS, TEMPERATURE_FAHRENHEIT]
        ),
        vol.Optional(CONF_TARGET_PRECISION, default=PRECISION_WHOLE): vol.In(
            [PRECISION_WHOLE, PRECISION_HALVES, PRECISION_TENTHS]
        ),
        vol.Optional(CONF_HEURISTIC_ACTION): bool,
        vol.Optional(CONF_TRUE_TEMPERATURE_ENTITY): str,
        vol.Optional(CONF_POWER_LEVEL_ENTITY): str,
    }


class _PIDExtraData(ExtraStoredData):
    """Restorable container for the self-tuned PID gains."""

    def __init__(self, data):
        self._data = data

    def as_dict(self):
        return self._data


class LocaltuyaClimate(LocalTuyaEntity, ClimateEntity):
    """Tuya climate device."""

    def __init__(
        self,
        device,
        config_entry,
        switchid,
        **kwargs,
    ):
        """Initialize a new LocaltuyaClimate."""
        super().__init__(device, config_entry, switchid, _LOGGER, **kwargs)
        self._state = None
        self._target_temperature = None
        self._current_temperature = None
        self._hvac_mode = None
        self._fan_mode = None
        self._swing_mode = None
        self._preset_mode = None
        self._hvac_action = None
        self._precision = self._config.get(CONF_PRECISION, DEFAULT_PRECISION)
        self._target_precision = self._config.get(
            CONF_TARGET_PRECISION, self._precision
        )
        self._conf_hvac_mode_dp = self._config.get(CONF_HVAC_MODE_DP)
        self._conf_hvac_mode_set = HVAC_MODE_SETS.get(
            self._config.get(CONF_HVAC_MODE_SET), {}
        )
        self._conf_hvac_fan_mode_dp = self._config.get(CONF_HVAC_FAN_MODE_DP)
        self._conf_hvac_fan_mode_set = HVAC_FAN_MODE_SETS.get(
            self._config.get(CONF_HVAC_FAN_MODE_SET), {}
        )
        self._conf_hvac_swing_mode_dp = self._config.get(CONF_HVAC_SWING_MODE_DP)
        self._conf_hvac_swing_mode_set = HVAC_SWING_MODE_SETS.get(
            self._config.get(CONF_HVAC_SWING_MODE_SET), {}
        )
        self._conf_preset_dp = self._config.get(CONF_PRESET_DP)
        self._conf_preset_set = PRESET_SETS.get(self._config.get(CONF_PRESET_SET), {})
        self._conf_hvac_action_dp = self._config.get(CONF_HVAC_ACTION_DP)
        self._conf_hvac_action_set = HVAC_ACTION_SETS.get(
            self._config.get(CONF_HVAC_ACTION_SET), {}
        )
        self._conf_eco_dp = self._config.get(CONF_ECO_DP)
        self._conf_eco_value = self._config.get(CONF_ECO_VALUE, "ECO")
        self._conf_sleep_dp = self._config.get(CONF_SLEEP_DP)
        self._true_temp_entity_id = self._config.get(CONF_TRUE_TEMPERATURE_ENTITY) or None
        self._true_temperature = None
        # Optional inverter compressor power reading (watts), read-only telemetry.
        self._power_level_entity_id = self._config.get(CONF_POWER_LEVEL_ENTITY) or None
        self._power_level = None
        # Self-tuning PID compensation is active whenever a true-temperature
        # entity is paired with a writable target-temperature DP.
        self._pid_enabled = bool(
            self._true_temp_entity_id and self.has_config(CONF_TARGET_TEMPERATURE_DP)
        )
        # Degree-based PID constants are defined in Celsius; scale to the unit.
        unit_scale = (
            1.8
            if self._config.get(CONF_TEMPERATURE_UNIT, DEFAULT_TEMPERATURE_UNIT)
            == TEMPERATURE_FAHRENHEIT
            else 1.0
        )
        # Heat and cool are tuned and persisted independently - their thermal
        # dynamics differ, so each mode gets its own parameter set.
        self._pid_cool = SelfTuningPID(scale=unit_scale, mode_sign=-1)
        self._pid_heat = SelfTuningPID(scale=unit_scale, mode_sign=1)
        self._cutoff_deadband = CUTOFF_DEADBAND_C * unit_scale
        self._auto_deadband = AUTO_DEADBAND_C * unit_scale
        self._pid_lock = asyncio.Lock()
        self._pid_forced_off = False
        self._last_power_change = None  # loop time of the last forced on/off
        self._user_hvac_mode = None  # mode shown in the UI even when forced off
        self._last_pid_time = None
        self._sent_setpoint = None  # last setpoint actually written to the AC
        # True Auto state.
        self._auto_dir = None  # HVACMode.COOL / HVACMode.HEAT / None (idle)
        self._last_mode_switch = None  # loop time of the last heat<->cool switch
        self._true_auto_off = False  # user master-off while True Auto is on
        self._shared_state = None
        self._pid_signal = None
        self._has_presets = (
            self.has_config(CONF_ECO_DP)
            or self.has_config(CONF_PRESET_DP)
            or self.has_config(CONF_SLEEP_DP)
        )
        _LOGGER.debug("Initialized climate [%s]", self.name)

    async def async_added_to_hass(self):
        """Wire up true-temperature tracking and the self-tuning PID loop."""
        await super().async_added_to_hass()
        if not self._pid_enabled:
            return

        # Restore previously tuned gains so a restart does not re-run the
        # disruptive relay experiment.
        last_extra = await self.async_get_last_extra_data()
        if last_extra is not None:
            data = last_extra.as_dict() or {}
            if "cool" in data or "heat" in data:
                self._pid_cool.restore(data.get("cool"))
                self._pid_heat.restore(data.get("heat"))
            else:
                # Migrate a pre-split single snapshot onto both controllers.
                self._pid_cool.restore(data)
                self._pid_heat.restore(data)

        # Share companion-toggle flags (overshoot cutoff, True Auto) with the
        # switch entities that live on a different platform.
        dev_id = self._dev_config_entry[CONF_DEVICE_ID]
        self._shared_state = get_overshoot_state(
            self.hass, LOCALTUYA_DOMAIN, dev_id, self._dp_id
        )
        self._shared_state["climate"] = self
        self._pid_signal = f"localtuya_pid_{dev_id}_{self._dp_id}"

        # Seed the current true-temperature reading.
        state = self.hass.states.get(self._true_temp_entity_id)
        if state and state.state not in ("unknown", "unavailable"):
            try:
                self._true_temperature = float(state.state)
            except ValueError:
                pass

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._true_temp_entity_id],
                self._async_true_temp_changed,
            )
        )

        # Optional inverter power reading (watts): read-only telemetry used for
        # diagnostics and future cascade/min-speed logic.
        if self._power_level_entity_id:
            state = self.hass.states.get(self._power_level_entity_id)
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    self._power_level = float(state.state)
                    self._pid_cool.power.update(self._power_level)
                    self._pid_heat.power.update(self._power_level)
                except ValueError:
                    pass
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._power_level_entity_id],
                    self._async_power_level_changed,
                )
            )

        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_pid_tick,
                timedelta(seconds=PID_SAMPLE_TIME),
            )
        )

    @property
    def extra_restore_state_data(self):
        """Persist both tuned gain sets across restarts."""
        if not self._pid_enabled:
            return None
        return _PIDExtraData(
            {
                "cool": self._pid_cool.snapshot(),
                "heat": self._pid_heat.snapshot(),
            }
        )

    async def _async_true_temp_changed(self, event):
        """Store the latest true-temperature reading (the PID loop consumes it)."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            self._true_temperature = float(new_state.state)
        except ValueError:
            return
        # Reflect the new reading as the displayed current temperature; the
        # periodic PID tick owns pushing setpoints to the device.
        self.async_write_ha_state()

    async def _async_power_level_changed(self, event):
        """Store the latest inverter power reading (watts)."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            self._power_level = float(new_state.state)
        except ValueError:
            return
        # Feed the monitors silently (cheap min/max update, no dispatch). The
        # external power sensor can update every few seconds; publishing the full
        # PID report on each one starved the device heartbeat and flapped the
        # connection, so diagnostics refresh on the 60s control tick instead.
        self._pid_cool.power.update(self._power_level)
        self._pid_heat.power.update(self._power_level)

    @property
    def power_level(self):
        """Latest inverter compressor power draw in watts (or None)."""
        return self._power_level

    # --- Self-tuning PID control -------------------------------------------

    @property
    def _overshoot_cutoff_enabled(self):
        """Whether the 'Turn Off If Overshoot' behaviour is currently armed."""
        if self._shared_state is not None:
            return self._shared_state.get("enabled", True)
        return True

    @property
    def _true_auto_enabled(self):
        """Whether autonomous heat/cool/off ('True Auto') is toggled on."""
        if self._shared_state is not None:
            return self._shared_state.get("true_auto", False)
        return False

    @property
    def _power_cascade_enabled(self):
        """Whether the inner power-tracking cascade is on (needs a power input)."""
        if self._power_level_entity_id is None or self._shared_state is None:
            return False
        return self._shared_state.get("power_cascade", False)

    @property
    def _true_auto_capable(self):
        """True Auto needs both a heat and a cool device mode to switch between."""
        return (
            HVACMode.COOL in self._conf_hvac_mode_set
            and HVACMode.HEAT in self._conf_hvac_mode_set
        )

    @property
    def _true_auto_active(self):
        """Whether True Auto is on, possible and actually in control."""
        return (
            self._pid_enabled
            and self._true_auto_enabled
            and self._true_auto_capable
        )

    def _mode_sign(self):
        """Direction the active mode drives the offset: -1 cool, +1 heat."""
        if self._user_hvac_mode == HVACMode.COOL:
            return -1
        if self._user_hvac_mode == HVACMode.HEAT:
            return 1
        return None

    def _active_pid(self):
        """Return the controller for the current mode (cool/heat tune separately).

        In True Auto the running direction selects the set; otherwise the device
        mode does. For AUTO / other modes (or idle True Auto) we pick by which
        side of the setpoint the room is on, so each set only learns from its own
        regime.
        """
        if self._true_auto_enabled and self._auto_dir is not None:
            return self._pid_heat if self._auto_dir == HVACMode.HEAT else self._pid_cool
        if not self._true_auto_enabled:
            if self._user_hvac_mode == HVACMode.HEAT:
                return self._pid_heat
            if self._user_hvac_mode == HVACMode.COOL:
                return self._pid_cool
        if (
            self._target_temperature is not None
            and self._true_temperature is not None
            and self._true_temperature < self._target_temperature
        ):
            return self._pid_heat
        return self._pid_cool

    async def _async_pid_tick(self, now):
        """Periodic timer callback."""
        await self._async_run_pid(now)

    def _dwell_elapsed(self):
        """Whether enough time has passed since the last forced power change."""
        if self._last_power_change is None:
            return True
        return (self.hass.loop.time() - self._last_power_change) >= MIN_CYCLE_TIME

    def _control_active(self):
        """Whether the loop should steer: directional mode, or just powered on."""
        if self.has_config(CONF_HVAC_MODE_DP):
            return self._user_hvac_mode not in (None, HVACMode.OFF)
        return bool(self._state)

    def _mode_switch_dwell_elapsed(self):
        """Whether the valve-protection time since the last reversal has passed."""
        if self._last_mode_switch is None:
            return True
        return (
            self.hass.loop.time() - self._last_mode_switch
        ) >= AUTO_MODE_SWITCH_MIN_TIME

    async def _async_run_pid(self, now=None):
        """Compute the PID offset and push the resulting setpoint to the AC."""
        if not self._pid_enabled:
            return
        async with self._pid_lock:
            if (
                self._target_temperature is None
                or self._true_temperature is None
                or self._current_temperature is None
            ):
                return

            # True Auto owns heat/cool/off selection itself.
            if self._true_auto_active:
                await self._run_true_auto(now)
                return

            if not self._control_active():
                return

            mode_sign = self._mode_sign()
            pid = self._active_pid()

            # Resume from a forced-off state only once the room again demands
            # this mode's direction (cool wants error < 0, heat wants error > 0)
            # and the minimum off-time has elapsed.
            if self._pid_forced_off:
                error = self._target_temperature - self._true_temperature
                pid.error = error  # keep diagnostics fresh while parked
                if (
                    mode_sign is not None and mode_sign * error <= 0
                ) or not self._dwell_elapsed():
                    self._publish_pid_report()
                    return
                await self._set_pid_forced_off(False)

            if now is not None and self._last_pid_time is not None:
                dt = (now - self._last_pid_time).total_seconds()
            else:
                dt = PID_SAMPLE_TIME
            if now is not None:
                self._last_pid_time = now

            output = pid.compute(
                self._target_temperature,
                self._true_temperature,
                dt,
                power=self._power_level,
                cascade=self._power_cascade_enabled,
            )

            # Hard cutoff: a tuned controller commanding an offset that opposes
            # the active mode (beyond a deadband) means the AC should be off.
            # Suppressed during the relay experiment, gated by the minimum
            # on-time. If we cannot cut power yet, fall through - the opposing
            # offset already raises the sent setpoint so the AC idles itself.
            if (
                not pid.tuning
                and self._overshoot_cutoff_enabled
                and mode_sign is not None
                and mode_sign * output < -self._cutoff_deadband
                and self._dwell_elapsed()
            ):
                await self._set_pid_forced_off(True)
                self._publish_pid_report()
                return

            desired = self._current_temperature + output
            raw = round(desired / self._target_precision)
            self._sent_setpoint = raw * self._target_precision
            await self._device.set_dp(raw, self._config[CONF_TARGET_TEMPERATURE_DP])
            self._publish_pid_report()

    async def _set_pid_forced_off(self, forced):
        """Latch the unit off (or back on) without changing the displayed mode."""
        if forced == self._pid_forced_off:
            return
        self._pid_forced_off = forced
        self._last_power_change = self.hass.loop.time()
        self._active_pid().reset()  # avoid wind-up across the on/off transition
        if forced:
            await self._device.set_dp(False, self._dp_id)
        else:
            await self._device.set_dp(True, self._dp_id)
            if (
                self._conf_hvac_mode_dp is not None
                and self._user_hvac_mode in self._conf_hvac_mode_set
            ):
                await asyncio.sleep(MODE_WAIT)
                await self._device.set_dp(
                    self._conf_hvac_mode_set[self._user_hvac_mode],
                    self._conf_hvac_mode_dp,
                )
        self.async_write_ha_state()

    def on_overshoot_cutoff_changed(self, enabled):
        """Called by the companion switch when the toggle is flipped."""
        if not enabled and self._pid_forced_off:
            # Stop parking the unit off the moment the user disarms the feature.
            self.hass.async_create_task(self._set_pid_forced_off(False))

    def force_retune(self):
        """Called by the companion button to re-tune the active mode's set."""
        if not self._pid_enabled:
            return
        self._active_pid().retune()
        self._last_pid_time = None
        self.hass.async_create_task(self._async_retune())

    def stop_tuning(self):
        """Called by the companion button: freeze both gain sets (lock tuning)."""
        if not self._pid_enabled:
            return
        self._pid_cool.stop_tuning()
        self._pid_heat.stop_tuning()
        self._publish_pid_report()
        self.async_write_ha_state()

    def on_power_cascade_changed(self, enabled):
        """Called by the companion switch when Power Cascade is toggled."""
        # Start the cascade (or revert) from a clean controller state.
        self._pid_cool.reset()
        self._pid_heat.reset()
        self._publish_pid_report()
        self.hass.async_create_task(self._async_run_pid())

    async def _async_retune(self):
        """Power the unit back on (if parked) and resume tuning immediately."""
        if self._pid_forced_off:
            await self._set_pid_forced_off(False)
        self._publish_pid_report()
        await self._async_run_pid()

    # --- True Auto (autonomous heat/cool/off) ------------------------------

    async def _run_true_auto(self, now=None):
        """Autonomous heat/cool/off control, presented to the UI as AUTO."""
        if self._true_auto_off:
            if self._auto_dir is not None:
                await self._apply_auto_direction(None)
            self._publish_pid_report()
            return

        error = self._target_temperature - self._true_temperature

        # Desired direction with a deadband around the setpoint.
        if error <= -self._auto_deadband:
            desired = HVACMode.COOL
        elif error >= self._auto_deadband:
            desired = HVACMode.HEAT
        else:
            desired = None  # within band -> idle

        # Protect the reversing valve: hold the current direction until the
        # minimum heat<->cool dwell has elapsed.
        if (
            desired in (HVACMode.COOL, HVACMode.HEAT)
            and self._auto_dir in (HVACMode.COOL, HVACMode.HEAT)
            and desired != self._auto_dir
            and not self._mode_switch_dwell_elapsed()
        ):
            desired = self._auto_dir

        # Protect the compressor: honour the minimum on/off time on transitions
        # to or from idle.
        if desired != self._auto_dir and not self._dwell_elapsed():
            desired = self._auto_dir

        if desired != self._auto_dir:
            await self._apply_auto_direction(desired)

        if desired is None:
            self._active_pid().error = error  # keep diagnostics fresh
            self._publish_pid_report()
            return

        if now is not None and self._last_pid_time is not None:
            dt = (now - self._last_pid_time).total_seconds()
        else:
            dt = PID_SAMPLE_TIME
        if now is not None:
            self._last_pid_time = now

        pid = self._pid_heat if desired == HVACMode.HEAT else self._pid_cool
        output = pid.compute(
            self._target_temperature,
            self._true_temperature,
            dt,
            power=self._power_level,
            cascade=self._power_cascade_enabled,
        )
        sent = self._current_temperature + output
        raw = round(sent / self._target_precision)
        self._sent_setpoint = raw * self._target_precision
        await self._device.set_dp(raw, self._config[CONF_TARGET_TEMPERATURE_DP])
        self._publish_pid_report()

    async def _apply_auto_direction(self, direction):
        """Drive the device to COOL/HEAT/off for True Auto, tracking dwell timers."""
        reversing = direction in (HVACMode.COOL, HVACMode.HEAT) and self._auto_dir in (
            HVACMode.COOL,
            HVACMode.HEAT,
        )
        self._auto_dir = direction
        now = self.hass.loop.time()
        if direction is None:
            await self._device.set_dp(False, self._dp_id)
            self._last_power_change = now
        else:
            if not self._state:
                await self._device.set_dp(True, self._dp_id)
                self._last_power_change = now
                await asyncio.sleep(MODE_WAIT)
            value = self._conf_hvac_mode_set.get(direction)
            if value is not None and self._conf_hvac_mode_dp is not None:
                await self._device.set_dp(value, self._conf_hvac_mode_dp)
            # Entering a direction (re)starts the valve-protection clock; a
            # reversal also restarts the compressor clock.
            self._last_mode_switch = now
            if reversing:
                self._last_power_change = now
            self._active_pid().reset()
        self.async_write_ha_state()

    def on_true_auto_changed(self, enabled):
        """Called by the companion switch when True Auto is toggled."""
        if enabled:
            self._pid_forced_off = False
            self._true_auto_off = False
        self._auto_dir = None
        self._last_mode_switch = None
        self.async_write_ha_state()
        self.hass.async_create_task(self._async_run_pid())

    def _publish_pid_report(self):
        """Notify the companion report sensors that PID state changed."""
        if self._pid_signal is not None:
            async_dispatcher_send(self.hass, self._pid_signal)

    @property
    def pid_report(self):
        """Gains/state for the companion report sensors.

        Per-mode gains and tuning status are reported for both sets; the live
        signals (error/integral/output/eta/cycles) reflect whichever set is
        currently active.
        """
        active = self._active_pid().report
        return {
            # The real device mode, which differs from the displayed hvac_mode
            # while the unit is parked off for overshoot protection.
            "actual_mode": self._hvac_mode,
            "sent": self._sent_setpoint,
            "error": active["error"],
            "integral": active["integral"],
            "output": active["output"],
            "tuning_cycles_remaining": active["tuning_cycles_remaining"],
            "tuning_eta": active["tuning_eta"],
            "cool_status": self._pid_cool.status,
            "cool_kp": self._pid_cool.pid.kp,
            "cool_ki": self._pid_cool.pid.ki,
            "cool_kd": self._pid_cool.pid.kd,
            "heat_status": self._pid_heat.status,
            "heat_kp": self._pid_heat.pid.kp,
            "heat_ki": self._pid_heat.pid.ki,
            "heat_kd": self._pid_heat.pid.kd,
            # Power-related live telemetry (active controller).
            "power_level": self._power_level,
            "power_pct": active["power_pct"],
            "power_min": active["power_min"],
            "power_max": active["power_max"],
            "power_saturation": active["power_saturation"],
            "cascade_target": active["cascade_target"],
            "cascade_integral": active["cascade_integral"],
        }

    @property
    def supported_features(self):
        """Flag supported features."""
        supported_features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self.has_config(CONF_TARGET_TEMPERATURE_DP):
            supported_features = supported_features | ClimateEntityFeature.TARGET_TEMPERATURE
        if self.has_config(CONF_MAX_TEMP_DP):
            supported_features = supported_features | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        if self.has_config(CONF_PRESET_DP) or self.has_config(CONF_ECO_DP):
            supported_features = supported_features | ClimateEntityFeature.PRESET_MODE
        if self.has_config(CONF_HVAC_FAN_MODE_DP) and self.has_config(CONF_HVAC_FAN_MODE_SET):
            supported_features = supported_features | ClimateEntityFeature.FAN_MODE
        if self.has_config(CONF_HVAC_SWING_MODE_DP):
            supported_features = supported_features | ClimateEntityFeature.SWING_MODE
        return supported_features

    @property
    def precision(self):
        """Return the precision of the system."""
        return self._precision

    @property
    def target_precision(self):
        """Return the precision of the target."""
        return self._target_precision

    @property
    def temperature_unit(self):
        """Return the unit of measurement used by the platform."""
        if (
            self._config.get(CONF_TEMPERATURE_UNIT, DEFAULT_TEMPERATURE_UNIT)
            == TEMPERATURE_FAHRENHEIT
        ):
            return UnitOfTemperature.FAHRENHEIT
        return UnitOfTemperature.CELSIUS

    @property
    def hvac_mode(self):
        """Return current operation ie. heat, cool, idle."""
        # True Auto presents a single AUTO mode and hides the real heat/cool/off
        # switching the loop performs underneath.
        if self._true_auto_active:
            return HVACMode.OFF if self._true_auto_off else HVACMode.AUTO
        # When the PID has parked the unit off to prevent overshoot, keep
        # showing the user's intended directional mode.
        if self._pid_forced_off and self._user_hvac_mode is not None:
            return self._user_hvac_mode
        return self._hvac_mode

    @property
    def hvac_modes(self):
        """Return the list of available operation modes."""
        if not self.has_config(CONF_HVAC_MODE_DP):
            return None
        modes = [HVACMode.OFF] + list(self._conf_hvac_mode_set)
        # True Auto presents AUTO regardless of whether the device exposes it.
        if self._true_auto_active and HVACMode.AUTO not in modes:
            modes.append(HVACMode.AUTO)
        return modes

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported.

        Need to be one of CURRENT_HVAC_*.
        """
        # True Auto: report what the loop is actually doing underneath AUTO.
        if self._true_auto_active:
            if self._true_auto_off:
                return HVACAction.OFF
            if self._auto_dir == HVACMode.COOL:
                return HVACAction.COOLING
            if self._auto_dir == HVACMode.HEAT:
                return HVACAction.HEATING
            return HVACAction.IDLE
        # While parked off for overshoot protection, keep displaying the
        # directional action so the UI still reads as cooling/heating.
        if self._pid_forced_off and self._user_hvac_mode is not None:
            if self._user_hvac_mode == HVACMode.COOL:
                return HVACAction.COOLING
            if self._user_hvac_mode == HVACMode.HEAT:
                return HVACAction.HEATING
        if self._config.get(CONF_HEURISTIC_ACTION, False):
            if self._hvac_mode == HVACMode.HEAT:
                if self._current_temperature < (
                    self._target_temperature - self._precision
                ):
                    self._hvac_action = HVACAction.HEATING
                if self._current_temperature == (
                    self._target_temperature - self._precision
                ):
                    if self._hvac_action == HVACAction.HEATING:
                        self._hvac_action = HVACAction.HEATING
                    if self._hvac_action == HVACAction.IDLE:
                        self._hvac_action = HVACAction.IDLE
                if (
                    self._current_temperature + self._precision
                ) > self._target_temperature:
                    self._hvac_action = HVACAction.IDLE
            return self._hvac_action
        return self._hvac_action

    @property
    def preset_mode(self):
        """Return current preset."""
        return self._preset_mode

    @property
    def preset_modes(self):
        """Return the list of available presets modes."""
        if not self._has_presets:
            return None
        presets = [PRESET_NONE] + list(self._conf_preset_set)
        if self._conf_eco_dp:
            presets.append(PRESET_ECO)
        if self._conf_sleep_dp is not None:
            presets.append(PRESET_SLEEP)
        return presets

    @property
    def current_temperature(self):
        """Return the current temperature."""
        if self._true_temp_entity_id and self._true_temperature is not None:
            return self._true_temperature
        return self._current_temperature

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        return self._config.get(CONF_TEMPERATURE_STEP, DEFAULT_TEMPERATURE_STEP)

    @property
    def fan_mode(self):
        """Return the fan setting."""
        return self._fan_mode

    @property
    def fan_modes(self):
        """Return the list of available fan modes."""
        if not self.has_config(CONF_HVAC_FAN_MODE_DP):
            return None
        return list(self._conf_hvac_fan_mode_set)

    @property
    def swing_mode(self):
        """Return the swing setting."""
        return self._swing_mode

    @property
    def swing_modes(self):
        """Return the list of available swing modes."""
        if not self.has_config(CONF_HVAC_SWING_MODE_DP):
            return None
        return list(self._conf_hvac_swing_mode_set)

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        if ATTR_TEMPERATURE in kwargs and self.has_config(CONF_TARGET_TEMPERATURE_DP):
            user_temp = kwargs[ATTR_TEMPERATURE]
            self._target_temperature = user_temp
            if self._pid_enabled:
                # The setpoint stays the user's value; the PID loop owns what is
                # actually sent. Re-baseline the derivative and recompute now so
                # the change is not delayed until the next tick.
                self._pid_cool.reset_derivative()
                self._pid_heat.reset_derivative()
                await self._async_run_pid()
                self.async_write_ha_state()
            else:
                temperature = round(user_temp / self._target_precision)
                await self._device.set_dp(
                    temperature, self._config[CONF_TARGET_TEMPERATURE_DP]
                )

    async def async_set_fan_mode(self, fan_mode):
        """Set new target fan mode."""
        if self._conf_hvac_fan_mode_dp is None:
            _LOGGER.error("Fan speed unsupported (no DP)")
            return
        if fan_mode not in self._conf_hvac_fan_mode_set:
            _LOGGER.error("Unsupported fan_mode: %s" % fan_mode)
            return
        await self._device.set_dp(
            self._conf_hvac_fan_mode_set[fan_mode], self._conf_hvac_fan_mode_dp
        )

    async def async_set_hvac_mode(self, hvac_mode):
        """Set new target operation mode."""
        if self._true_auto_active:
            # The loop owns heat/cool; the dropdown only toggles master on/off
            # (any non-off selection resumes autonomous control).
            self._true_auto_off = hvac_mode == HVACMode.OFF
            if self._true_auto_off:
                self._auto_dir = None
                await self._device.set_dp(False, self._dp_id)
            self.async_write_ha_state()
            await self._async_run_pid()
            return
        if self._pid_enabled:
            self._user_hvac_mode = hvac_mode
            # A real mode change clears any overshoot-driven off latch and starts
            # the now-active mode's controller from a clean slate.
            self._pid_forced_off = False
            self._active_pid().reset()
        if hvac_mode == HVACMode.OFF:
            await self._device.set_dp(False, self._dp_id)
            return
        if not self._state and self._conf_hvac_mode_dp != self._dp_id:
            await self._device.set_dp(True, self._dp_id)
            # Some thermostats need a small wait before sending another update
            await asyncio.sleep(MODE_WAIT)
        await self._device.set_dp(
            self._conf_hvac_mode_set[hvac_mode], self._conf_hvac_mode_dp
        )

    async def async_set_swing_mode(self, swing_mode):
        """Set new target swing operation."""
        if self._conf_hvac_swing_mode_dp is None:
            _LOGGER.error("Swing mode unsupported (no DP)")
            return
        if swing_mode not in self._conf_hvac_swing_mode_set:
            _LOGGER.error("Unsupported swing_mode: %s" % swing_mode)
            return
        await self._device.set_dp(
            self._conf_hvac_swing_mode_set[swing_mode], self._conf_hvac_swing_mode_dp
        )

    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        if self._true_auto_active:
            self._true_auto_off = False
            self.async_write_ha_state()
            await self._async_run_pid()
            return
        await self._device.set_dp(True, self._dp_id)

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        if self._true_auto_active:
            self._true_auto_off = True
            self._auto_dir = None
            await self._device.set_dp(False, self._dp_id)
            self.async_write_ha_state()
            return
        await self._device.set_dp(False, self._dp_id)

    async def async_set_preset_mode(self, preset_mode):
        """Set new target preset mode."""
        if preset_mode == PRESET_SLEEP:
            await self._device.set_dp(True, self._conf_sleep_dp)
            return
        if self._conf_sleep_dp is not None:
            await self._device.set_dp(False, self._conf_sleep_dp)
        if preset_mode == PRESET_ECO:
            await self._device.set_dp(self._conf_eco_value, self._conf_eco_dp)
            return
        if preset_mode == PRESET_NONE:
            return
        await self._device.set_dp(
            self._conf_preset_set[preset_mode], self._conf_preset_dp
        )

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        if self.has_config(CONF_MIN_TEMP_DP):
            return self.dps_conf(CONF_MIN_TEMP_DP)
        return self._config[CONF_TEMP_MIN]

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        if self.has_config(CONF_MAX_TEMP_DP):
            return self.dps_conf(CONF_MAX_TEMP_DP)
        return self._config[CONF_TEMP_MAX]

    def status_updated(self):
        """Device status was updated."""
        self._state = self.dps(self._dp_id)

        if self.has_config(CONF_CURRENT_TEMPERATURE_DP):
            self._current_temperature = (
                self.dps_conf(CONF_CURRENT_TEMPERATURE_DP) * self._precision
            )

        if self.has_config(CONF_TARGET_TEMPERATURE_DP):
            device_temp = (
                self.dps_conf(CONF_TARGET_TEMPERATURE_DP) * self._target_precision
            )
            if (
                self._true_temp_entity_id
                and self._true_temperature is not None
                and self._current_temperature is not None
            ):
                # Only initialize from the device on the first update; after that
                # _target_temperature is owned by the user and must not be recomputed
                # from the device DP (which carries the adjusted value, not user intent).
                if self._target_temperature is None:
                    self._target_temperature = device_temp + (
                        self._true_temperature - self._current_temperature
                    )
            elif self._true_temp_entity_id:
                # True-temp compensation is configured but the reading is not yet
                # available: the device DP holds the *adjusted* value, so seeding
                # the user setpoint from it would be wrong. Wait for the sensor.
                pass
            else:
                self._target_temperature = device_temp

        if self._has_presets:
            if self._conf_sleep_dp is not None and self.dps(self._conf_sleep_dp):
                self._preset_mode = PRESET_SLEEP
            elif (
                self.has_config(CONF_ECO_DP)
                and self.dps_conf(CONF_ECO_DP) == self._conf_eco_value
            ):
                self._preset_mode = PRESET_ECO
            else:
                for preset, value in self._conf_preset_set.items():  # todo remove
                    if self.dps_conf(CONF_PRESET_DP) == value:
                        self._preset_mode = preset
                        break
                else:
                    self._preset_mode = PRESET_NONE

        # Update the HVAC status
        if self.has_config(CONF_HVAC_MODE_DP):
            if not self._state:
                self._hvac_mode = HVACMode.OFF
            else:
                for mode, value in self._conf_hvac_mode_set.items():
                    if self.dps_conf(CONF_HVAC_MODE_DP) == value:
                        self._hvac_mode = mode
                        break
                else:
                    # in case hvac mode and preset share the same dp
                    self._hvac_mode = HVACMode.AUTO

            # Track the user's intended mode for PID control / display. While
            # the PID has forced the unit off, the device reports OFF but the
            # user's intent is unchanged, so we must not overwrite it.
            if self._pid_enabled:
                if self._state and self._hvac_mode not in (None, HVACMode.OFF):
                    self._user_hvac_mode = self._hvac_mode
                elif not self._state and not self._pid_forced_off:
                    self._user_hvac_mode = HVACMode.OFF

        # Update the fan status
        if self.has_config(CONF_HVAC_FAN_MODE_DP):
            for mode, value in self._conf_hvac_fan_mode_set.items():
                if self.dps_conf(CONF_HVAC_FAN_MODE_DP) == value:
                    self._fan_mode = mode
                    break
            else:
                # in case fan mode and preset share the same dp
                _LOGGER.debug("Unknown fan mode %s" % self.dps_conf(CONF_HVAC_FAN_MODE_DP))
                self._fan_mode = FAN_AUTO

        # Update the swing status
        if self.has_config(CONF_HVAC_SWING_MODE_DP):
            for mode, value in self._conf_hvac_swing_mode_set.items():
                if self.dps_conf(CONF_HVAC_SWING_MODE_DP) == value:
                    self._swing_mode = mode
                    break
            else:
                _LOGGER.debug("Unknown swing mode %s" % self.dps_conf(CONF_HVAC_SWING_MODE_DP))
                self._swing_mode = SWING_OFF

        # Update the current action
        for action, value in self._conf_hvac_action_set.items():
            if self.dps_conf(CONF_HVAC_ACTION_DP) == value:
                self._hvac_action = action


def _cleanup_orphan_climate_companions(hass, config_entry, dev_id, dp_id):
    """Remove old climate-domain companion entities.

    The AC-temperature and PID diagnostic sensors used to be created on the
    climate platform (``climate.*`` ids, which the UI cannot graph). They now
    live on the sensor platform, so any leftover climate-domain companions for
    this device are orphans and get pruned here.
    """
    # Note: the AC-temperature companion keys off the current-temperature DP,
    # not the climate DP, so match at the device level.
    # Match by integration + entity domain + unique-id pattern rather than the
    # config-entry binding: HA clears config_entry_id once an entity is orphaned,
    # so config-entry lookups would miss exactly the entries we need to prune.
    prefix = f"local_{dev_id}_"
    registry = er.async_get(hass)
    for entry in list(registry.entities.values()):
        uid = entry.unique_id or ""
        if (
            entry.platform == LOCALTUYA_DOMAIN
            and entry.domain == DOMAIN  # old climate-platform companions
            and uid.startswith(prefix)
            and ("_pid_" in uid or uid.endswith("_ac_temp"))
        ):
            _LOGGER.debug("Removing orphaned climate companion %s", entry.entity_id)
            registry.async_remove(entry.entity_id)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up LocalTuya climate entities.

    Companion sensors (AC measured temperature and PID diagnostics) are created
    by the sensor platform; the overshoot/True-Auto switches by the switch
    platform; the re-tune button by the button platform.
    """
    entities = []

    for dev_id, dev_entry in config_entry.data.get("devices", {}).items():
        climate_configs = [
            e for e in dev_entry.get(CONF_ENTITIES, [])
            if e.get(CONF_PLATFORM) == DOMAIN  # DOMAIN == "climate" here
        ]
        if not climate_configs:
            continue

        tuyainterface = hass.data[LOCALTUYA_DOMAIN][TUYA_DEVICES][dev_id]
        dps_config_fields = list(get_dps_for_platform(flow_schema))
        dev_entities = []

        for entity_config in climate_configs:
            for dp_conf in dps_config_fields:
                if dp_conf in entity_config:
                    tuyainterface.dps_to_request[entity_config[dp_conf]] = None

            dev_entities.append(
                LocaltuyaClimate(tuyainterface, dev_entry, entity_config[CONF_ID])
            )

            if entity_config.get(CONF_TRUE_TEMPERATURE_ENTITY):
                _cleanup_orphan_climate_companions(
                    hass, config_entry, dev_id, entity_config[CONF_ID]
                )

        tuyainterface.add_entities(dev_entities)
        entities.extend(dev_entities)

    if entities:
        async_add_entities(entities)
