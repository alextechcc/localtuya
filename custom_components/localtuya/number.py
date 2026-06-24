"""Platform to present any Tuya DP as a number."""
import logging
from functools import partial

import voluptuous as vol
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.number import DOMAIN, NumberEntity, NumberMode
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_ID,
    CONF_PLATFORM,
    CONF_TEMPERATURE_UNIT,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .common import LocalTuyaEntity, async_setup_entry as _generic_setup_entry
from .const import (
    CONF_CURRENT_TEMPERATURE_DP,
    CONF_DEFAULT_VALUE,
    CONF_MAX_VALUE,
    CONF_MIN_VALUE,
    CONF_MODEL,
    CONF_PASSIVE_ENTITY,
    CONF_PROTOCOL_VERSION,
    CONF_RESTORE_ON_RECONNECT,
    CONF_STEPSIZE_VALUE,
    CONF_TRUE_TEMPERATURE_ENTITY,
    DOMAIN as LOCALTUYA_DOMAIN,
)
from .pid import get_overshoot_state

_LOGGER = logging.getLogger(__name__)

DEFAULT_MIN = 0
DEFAULT_MAX = 100000
DEFAULT_STEP = 1.0

# Overshoot-hysteresis companion (climate PID).
DEFAULT_HYSTERESIS = 2.0
MIN_HYSTERESIS = 0.5
MAX_HYSTERESIS = 10.0
HYSTERESIS_STEP = 0.5
TEMPERATURE_FAHRENHEIT = "fahrenheit"


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        vol.Optional(CONF_MIN_VALUE, default=DEFAULT_MIN): vol.All(
            vol.Coerce(float),
            vol.Range(min=-1000000.0, max=1000000.0),
        ),
        vol.Required(CONF_MAX_VALUE, default=DEFAULT_MAX): vol.All(
            vol.Coerce(float),
            vol.Range(min=-1000000.0, max=1000000.0),
        ),
        vol.Required(CONF_STEPSIZE_VALUE, default=DEFAULT_STEP): vol.All(
            vol.Coerce(float),
            vol.Range(min=0.0, max=1000000.0),
        ),
        vol.Required(CONF_RESTORE_ON_RECONNECT): bool,
        vol.Required(CONF_PASSIVE_ENTITY): bool,
        vol.Optional(CONF_DEFAULT_VALUE): str,
    }


class LocaltuyaNumber(LocalTuyaEntity, NumberEntity):
    """Representation of a Tuya Number."""

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

        self._min_value = DEFAULT_MIN
        if CONF_MIN_VALUE in self._config:
            self._min_value = self._config.get(CONF_MIN_VALUE)

        self._max_value = DEFAULT_MAX
        if CONF_MAX_VALUE in self._config:
            self._max_value = self._config.get(CONF_MAX_VALUE)

        self._step_size = DEFAULT_STEP
        if CONF_STEPSIZE_VALUE in self._config:
            self._step_size = self._config.get(CONF_STEPSIZE_VALUE)

        # Override standard default value handling to cast to a float
        default_value = self._config.get(CONF_DEFAULT_VALUE)
        if default_value is not None:
            self._default_value = float(default_value)

    @property
    def native_value(self) -> float:
        """Return sensor state."""
        return self._state

    @property
    def native_min_value(self) -> float:
        """Return the minimum value."""
        return self._min_value

    @property
    def native_max_value(self) -> float:
        """Return the maximum value."""
        return self._max_value

    @property
    def native_step(self) -> float:
        """Return the maximum value."""
        return self._step_size

    @property
    def device_class(self):
        """Return the class of this device."""
        return self._config.get(CONF_DEVICE_CLASS)

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        await self._device.set_dp(value, self._dp_id)

    # Default value is the minimum value
    def entity_default_value(self):
        """Return the minimum value as the default for this entity type."""
        return self._min_value


class LocaltuyaHysteresisNumber(RestoreEntity, NumberEntity):
    """Companion number setting the climate overshoot/cycling hysteresis.

    The value (in the device's temperature unit) is the deadband around the
    setpoint: the unit idles for overshoot once the true temperature passes the
    setpoint by half this band, and re-engages only after it drifts back half the
    band - one full width of swing, so a sluggish AC does not short-cycle. Shared
    with the climate entity through ``hass.data`` so the platforms stay in sync.
    """

    def __init__(self, dev_entry, climate_config):
        super().__init__()
        self._dev_entry = dev_entry
        self._climate_config = climate_config
        self._dp_key = str(climate_config[CONF_ID])
        self._value = DEFAULT_HYSTERESIS
        self._state_obj = None
        self._fahrenheit = (
            climate_config.get(CONF_TEMPERATURE_UNIT) == TEMPERATURE_FAHRENHEIT
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._state_obj = get_overshoot_state(
            self.hass,
            LOCALTUYA_DOMAIN,
            self._dev_entry[CONF_DEVICE_ID],
            self._dp_key,
        )
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            "unknown",
            "unavailable",
            None,
        ):
            try:
                self._value = float(last_state.state)
            except (TypeError, ValueError):
                pass
        self._apply(self._value)

    def _apply(self, value):
        self._value = value
        self._state_obj["hysteresis"] = value
        climate = self._state_obj.get("climate")
        if climate is not None:
            climate.on_hysteresis_changed(value)

    async def async_set_native_value(self, value):
        self._apply(value)
        self.async_write_ha_state()

    @property
    def native_value(self):
        return self._value

    @property
    def native_min_value(self):
        return MIN_HYSTERESIS

    @property
    def native_max_value(self):
        return MAX_HYSTERESIS

    @property
    def native_step(self):
        return HYSTERESIS_STEP

    @property
    def native_unit_of_measurement(self):
        return (
            UnitOfTemperature.FAHRENHEIT
            if self._fahrenheit
            else UnitOfTemperature.CELSIUS
        )

    @property
    def mode(self):
        return NumberMode.BOX

    @property
    def unique_id(self):
        return f"local_{self._dev_entry[CONF_DEVICE_ID]}_{self._dp_key}_hysteresis"

    @property
    def name(self):
        return f"{self._climate_config[CONF_FRIENDLY_NAME]} Overshoot Hysteresis"

    @property
    def entity_category(self):
        return EntityCategory.CONFIG

    @property
    def should_poll(self):
        return False

    @property
    def device_info(self):
        model = self._dev_entry.get(CONF_MODEL, "Tuya generic")
        return {
            "identifiers": {(LOCALTUYA_DOMAIN, f"local_{self._dev_entry[CONF_DEVICE_ID]}")},
            "name": self._dev_entry[CONF_FRIENDLY_NAME],
            "manufacturer": "Tuya",
            "model": f"{model} ({self._dev_entry[CONF_DEVICE_ID]})",
            "sw_version": self._dev_entry[CONF_PROTOCOL_VERSION],
        }


_setup_tuya_numbers = partial(
    _generic_setup_entry, DOMAIN, LocaltuyaNumber, flow_schema
)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up Tuya numbers plus the PID overshoot-hysteresis number for climates."""
    # The number platform may be force-loaded purely for the hysteresis companion
    # (no native number DPs). The generic helper assumes at least one matching
    # entity, so only invoke it when native numbers are configured.
    has_native = any(
        entity.get(CONF_PLATFORM) == DOMAIN
        for dev_entry in config_entry.data.get(CONF_DEVICES, {}).values()
        for entity in dev_entry.get(CONF_ENTITIES, [])
    )
    if has_native:
        await _setup_tuya_numbers(hass, config_entry, async_add_entities)

    extra = []
    for dev_entry in config_entry.data.get(CONF_DEVICES, {}).values():
        for entity_config in dev_entry.get(CONF_ENTITIES, []):
            if (
                entity_config.get(CONF_PLATFORM) == CLIMATE_DOMAIN
                and entity_config.get(CONF_TRUE_TEMPERATURE_ENTITY)
                and entity_config.get(CONF_CURRENT_TEMPERATURE_DP)
            ):
                extra.append(LocaltuyaHysteresisNumber(dev_entry, entity_config))

    if extra:
        async_add_entities(extra)
