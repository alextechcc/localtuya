"""Platform to locally control Tuya-based climate devices."""
import asyncio
import logging

from homeassistant.helpers.event import async_track_state_change_event

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity
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
    TUYA_DEVICES,
)

_LOGGER = logging.getLogger(__name__)

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
    }


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
        self._has_presets = (
            self.has_config(CONF_ECO_DP)
            or self.has_config(CONF_PRESET_DP)
            or self.has_config(CONF_SLEEP_DP)
        )
        _LOGGER.debug("Initialized climate [%s]", self.name)

    async def async_added_to_hass(self):
        """Subscribe to state changes of the true temperature entity."""
        await super().async_added_to_hass()
        if not self._true_temp_entity_id:
            return
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

    async def _async_true_temp_changed(self, event):
        """Handle state change of the true temperature entity."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            self._true_temperature = float(new_state.state)
        except ValueError:
            return
        if (
            self._target_temperature is not None
            and self._current_temperature is not None
            and self.has_config(CONF_TARGET_TEMPERATURE_DP)
        ):
            adjusted = self._target_temperature + (
                self._current_temperature - self._true_temperature
            )
            raw = round(adjusted / self._target_precision)
            await self._device.set_dp(raw, self._config[CONF_TARGET_TEMPERATURE_DP])

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
        return self._hvac_mode

    @property
    def hvac_modes(self):
        """Return the list of available operation modes."""
        if not self.has_config(CONF_HVAC_MODE_DP):
            return None
        return [HVACMode.OFF] + list(self._conf_hvac_mode_set)

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported.

        Need to be one of CURRENT_HVAC_*.
        """
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
            if (
                self._true_temp_entity_id
                and self._true_temperature is not None
                and self._current_temperature is not None
            ):
                self._target_temperature = user_temp
                adjusted = user_temp + (self._current_temperature - self._true_temperature)
            else:
                adjusted = user_temp
            temperature = round(adjusted / self._target_precision)
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
        await self._device.set_dp(True, self._dp_id)

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
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


class LocaltuyaACTemperatureSensor(RestoreEntity, SensorEntity):
    """Sensor reporting the temperature as measured by the AC unit itself."""

    def __init__(self, device, dev_entry, climate_config):
        super().__init__()
        self._device = device
        self._dev_entry = dev_entry
        self._climate_config = climate_config
        self._temperature = None
        self._precision = climate_config.get(CONF_PRECISION, DEFAULT_PRECISION)
        self._dp_key = str(climate_config[CONF_CURRENT_TEMPERATURE_DP])

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        signal = f"localtuya_{self._dev_entry[CONF_DEVICE_ID]}"
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal, self._handle_status)
        )

    def _handle_status(self, status):
        if status is None or self._dp_key not in status:
            return
        self._temperature = status[self._dp_key] * self._precision
        self.schedule_update_ha_state()

    @property
    def unique_id(self):
        return f"local_{self._dev_entry[CONF_DEVICE_ID]}_{self._dp_key}_ac_temp"

    @property
    def name(self):
        return f"{self._climate_config[CONF_FRIENDLY_NAME]} AC Measured Temperature"

    @property
    def native_value(self):
        return self._temperature

    @property
    def native_unit_of_measurement(self):
        if self._climate_config.get(CONF_TEMPERATURE_UNIT) == TEMPERATURE_FAHRENHEIT:
            return UnitOfTemperature.FAHRENHEIT
        return UnitOfTemperature.CELSIUS

    @property
    def device_class(self):
        return SensorDeviceClass.TEMPERATURE

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT

    @property
    def should_poll(self):
        return False

    @property
    def device_info(self):
        model = self._dev_entry.get(CONF_MODEL, "Tuya generic")
        return {
            "identifiers": {(DOMAIN, f"local_{self._dev_entry[CONF_DEVICE_ID]}")},
            "name": self._dev_entry[CONF_FRIENDLY_NAME],
            "manufacturer": "Tuya",
            "model": f"{model} ({self._dev_entry[CONF_DEVICE_ID]})",
            "sw_version": self._dev_entry[CONF_PROTOCOL_VERSION],
        }


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up LocalTuya climate entities and companion AC temperature sensors."""
    entities = []

    for dev_id, dev_entry in config_entry.data.get("devices", {}).items():
        climate_configs = [
            e for e in dev_entry.get(CONF_ENTITIES, [])
            if e.get(CONF_PLATFORM) == DOMAIN
        ]
        if not climate_configs:
            continue

        tuyainterface = hass.data[DOMAIN][TUYA_DEVICES][dev_id]
        dps_config_fields = list(get_dps_for_platform(flow_schema))
        dev_entities = []

        for entity_config in climate_configs:
            for dp_conf in dps_config_fields:
                if dp_conf in entity_config:
                    tuyainterface.dps_to_request[entity_config[dp_conf]] = None

            dev_entities.append(
                LocaltuyaClimate(tuyainterface, dev_entry, entity_config[CONF_ID])
            )

            if (
                entity_config.get(CONF_TRUE_TEMPERATURE_ENTITY)
                and entity_config.get(CONF_CURRENT_TEMPERATURE_DP)
            ):
                dev_entities.append(
                    LocaltuyaACTemperatureSensor(tuyainterface, dev_entry, entity_config)
                )

        tuyainterface.add_entities(dev_entities)
        entities.extend(dev_entities)

    if entities:
        async_add_entities(entities)
