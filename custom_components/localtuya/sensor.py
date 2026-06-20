"""Platform to present any Tuya DP as a sensor.

Besides user-configured DP sensors, this platform also creates the companion
diagnostic sensors for climate entities that use true-temperature PID
compensation (the AC's own measured temperature plus the self-tuning PID
gains/state). They are created here - rather than from the climate platform - so
they get real ``sensor.*`` entity ids and render as graphable numeric sensors.
"""
import logging
from functools import partial

import voluptuous as vol
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.sensor import (
    DEVICE_CLASSES,
    DOMAIN,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_ID,
    CONF_PLATFORM,
    CONF_TEMPERATURE_UNIT,
    CONF_UNIT_OF_MEASUREMENT,
    PRECISION_TENTHS,
    STATE_UNKNOWN,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .common import LocalTuyaEntity, async_setup_entry as _generic_setup_entry
from .const import (
    CONF_CURRENT_TEMPERATURE_DP,
    CONF_MODEL,
    CONF_PRECISION,
    CONF_PROTOCOL_VERSION,
    CONF_SCALING,
    CONF_TRUE_TEMPERATURE_ENTITY,
    DOMAIN as LOCALTUYA_DOMAIN,
    TUYA_DEVICES,
)
from .pid import get_overshoot_state

_LOGGER = logging.getLogger(__name__)

DEFAULT_PRECISION = 2
CLIMATE_DEFAULT_PRECISION = PRECISION_TENTHS
TEMPERATURE_FAHRENHEIT = "fahrenheit"


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        vol.Optional(CONF_UNIT_OF_MEASUREMENT): str,
        vol.Optional(CONF_DEVICE_CLASS): vol.In(DEVICE_CLASSES),
        vol.Optional(CONF_SCALING): vol.All(
            vol.Coerce(float), vol.Range(min=-1000000.0, max=1000000.0)
        ),
    }


class LocaltuyaSensor(LocalTuyaEntity):
    """Representation of a Tuya sensor."""

    def __init__(
        self,
        device,
        config_entry,
        sensorid,
        **kwargs,
    ):
        """Initialize the Tuya sensor."""
        super().__init__(device, config_entry, sensorid, _LOGGER, **kwargs)
        self._state = STATE_UNKNOWN

    @property
    def state(self):
        """Return sensor state."""
        return self._state

    @property
    def device_class(self):
        """Return the class of this device."""
        return self._config.get(CONF_DEVICE_CLASS)

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement of this entity, if any."""
        return self._config.get(CONF_UNIT_OF_MEASUREMENT)

    def status_updated(self):
        """Device status was updated."""
        state = self.dps(self._dp_id)
        scale_factor = self._config.get(CONF_SCALING)
        if scale_factor is not None and isinstance(state, (int, float)):
            state = round(state * scale_factor, DEFAULT_PRECISION)
        self._state = state

    # No need to restore state for a sensor
    async def restore_state_when_connected(self):
        """Do nothing for a sensor."""
        return


def _climate_device_info(dev_entry):
    model = dev_entry.get(CONF_MODEL, "Tuya generic")
    return {
        "identifiers": {(LOCALTUYA_DOMAIN, f"local_{dev_entry[CONF_DEVICE_ID]}")},
        "name": dev_entry[CONF_FRIENDLY_NAME],
        "manufacturer": "Tuya",
        "model": f"{model} ({dev_entry[CONF_DEVICE_ID]})",
        "sw_version": dev_entry[CONF_PROTOCOL_VERSION],
    }


class LocaltuyaACTemperatureSensor(RestoreEntity, SensorEntity):
    """Sensor reporting the temperature as measured by the AC unit itself."""

    def __init__(self, device, dev_entry, climate_config):
        super().__init__()
        self._device = device
        self._dev_entry = dev_entry
        self._climate_config = climate_config
        self._temperature = None
        self._precision = climate_config.get(CONF_PRECISION, CLIMATE_DEFAULT_PRECISION)
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
    def entity_category(self):
        return EntityCategory.DIAGNOSTIC

    @property
    def should_poll(self):
        return False

    @property
    def device_info(self):
        return _climate_device_info(self._dev_entry)


# key, name suffix, and rendering hints for one field of climate.pid_report:
#   precision - decimals (0 => integer, None => string passthrough)
#   temp      - value is in the configured temperature unit
#   duration  - value is a number of seconds
PID_SENSOR_SPECS = [
    {"key": "actual_mode", "name": "Actual Mode"},
    {"key": "tuning_cycles_remaining", "name": "Tuning Cycles Remaining", "precision": 0},
    {"key": "tuning_eta", "name": "Tuning ETA", "precision": 0, "duration": True},
    {"key": "sent", "name": "Actual Set Temperature", "precision": 1, "temp": True},
    {"key": "error", "name": "PID Error", "precision": 3, "temp": True},
    {"key": "integral", "name": "PID Integral", "precision": 3},
    {"key": "output", "name": "PID Output", "precision": 3, "temp": True},
    # Per-mode tuned gains and status (heat and cool tune independently).
    {"key": "cool_status", "name": "Cool Tuning Status"},
    {"key": "cool_kp", "name": "Cool Kp", "precision": 4},
    {"key": "cool_ki", "name": "Cool Ki", "precision": 5},
    {"key": "cool_kd", "name": "Cool Kd", "precision": 3},
    {"key": "heat_status", "name": "Heat Tuning Status"},
    {"key": "heat_kp", "name": "Heat Kp", "precision": 4},
    {"key": "heat_ki", "name": "Heat Ki", "precision": 5},
    {"key": "heat_kd", "name": "Heat Kd", "precision": 3},
    # Power-aware control telemetry (refreshed on the 60s control tick).
    {"key": "power_pct", "name": "Compressor Power", "precision": 0, "unit": "%"},
    {"key": "power_min", "name": "Power Min Learned", "precision": 0, "unit": "W"},
    {"key": "power_max", "name": "Power Max Learned", "precision": 0, "unit": "W"},
    {"key": "power_saturation", "name": "Power Saturation"},
    {"key": "cascade_target", "name": "Cascade Power Target", "precision": 0, "unit": "%"},
    {"key": "cascade_integral", "name": "Cascade Integral", "precision": 0, "unit": "%"},
]


class LocaltuyaPIDReportSensor(SensorEntity):
    """Read-only sensor exposing one field of the self-tuning PID state.

    Reads the climate entity through the shared registry (the two live on
    different platforms) and refreshes on the climate's PID dispatcher signal.
    """

    def __init__(self, dev_entry, climate_config, spec):
        super().__init__()
        self._dev_entry = dev_entry
        self._climate_config = climate_config
        self._spec = spec
        self._key = spec["key"]
        self._precision = spec.get("precision")  # None => string passthrough
        self._dp_key = str(climate_config[CONF_ID])
        self._shared = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._shared = get_overshoot_state(
            self.hass,
            LOCALTUYA_DOMAIN,
            self._dev_entry[CONF_DEVICE_ID],
            self._dp_key,
        )
        signal = f"localtuya_pid_{self._dev_entry[CONF_DEVICE_ID]}_{self._dp_key}"
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal, self.async_write_ha_state)
        )

    @property
    def _climate(self):
        return self._shared.get("climate") if self._shared else None

    @property
    def unique_id(self):
        return (
            f"local_{self._dev_entry[CONF_DEVICE_ID]}_{self._dp_key}_pid_{self._key}"
        )

    @property
    def name(self):
        return f"{self._climate_config[CONF_FRIENDLY_NAME]} {self._spec['name']}"

    @property
    def available(self):
        # Tied only to the climate entity existing, not to the device being
        # connected: these are internal PID/power diagnostics (power comes from a
        # separate meter), so they should survive brief LAN flaps rather than
        # blanking out and showing their last value instead.
        return self._climate is not None

    @property
    def native_value(self):
        climate = self._climate
        if climate is None:
            return None
        value = climate.pid_report.get(self._key)
        if value is None or self._precision is None:
            return value
        if self._precision == 0:
            return int(round(value))
        return round(value, self._precision)

    @property
    def native_unit_of_measurement(self):
        if self._spec.get("unit"):
            return self._spec["unit"]
        if self._spec.get("duration"):
            return UnitOfTime.SECONDS
        if self._spec.get("temp"):
            if self._climate_config.get(CONF_TEMPERATURE_UNIT) == TEMPERATURE_FAHRENHEIT:
                return UnitOfTemperature.FAHRENHEIT
            return UnitOfTemperature.CELSIUS
        return None

    @property
    def device_class(self):
        if self._spec.get("duration"):
            return SensorDeviceClass.DURATION
        return None

    @property
    def state_class(self):
        # Numeric fields are measurements (graphable); string fields are not.
        if self._precision is None:
            return None
        return SensorStateClass.MEASUREMENT

    @property
    def entity_category(self):
        return EntityCategory.DIAGNOSTIC

    @property
    def should_poll(self):
        return False

    @property
    def device_info(self):
        return _climate_device_info(self._dev_entry)


def _climate_pid_configs(config_entry):
    """Yield (dev_entry, entity_config) for climates using true-temp PID."""
    for dev_entry in config_entry.data.get(CONF_DEVICES, {}).values():
        for entity_config in dev_entry.get(CONF_ENTITIES, []):
            if (
                entity_config.get(CONF_PLATFORM) == CLIMATE_DOMAIN
                and entity_config.get(CONF_TRUE_TEMPERATURE_ENTITY)
                and entity_config.get(CONF_CURRENT_TEMPERATURE_DP)
            ):
                yield dev_entry, entity_config


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up Tuya DP sensors plus climate PID companion sensors."""
    # The sensor platform may be force-loaded purely for the climate companions.
    # The generic helper assumes at least one matching entity, so only call it
    # when native sensor entities are configured.
    has_native = any(
        entity.get(CONF_PLATFORM) == DOMAIN
        for dev_entry in config_entry.data.get(CONF_DEVICES, {}).values()
        for entity in dev_entry.get(CONF_ENTITIES, [])
    )
    if has_native:
        await _generic_setup_entry(
            DOMAIN, LocaltuyaSensor, flow_schema, hass, config_entry, async_add_entities
        )

    companions = []
    for dev_entry, entity_config in _climate_pid_configs(config_entry):
        companions.append(
            LocaltuyaACTemperatureSensor(
                hass.data[LOCALTUYA_DOMAIN][TUYA_DEVICES][dev_entry[CONF_DEVICE_ID]],
                dev_entry,
                entity_config,
            )
        )
        companions.extend(
            LocaltuyaPIDReportSensor(dev_entry, entity_config, spec)
            for spec in PID_SENSOR_SPECS
        )

    if companions:
        async_add_entities(companions)
