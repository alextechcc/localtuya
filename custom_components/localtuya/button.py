"""Companion button entities for LocalTuya climate PID control.

The only button provided today forces the self-tuning PID to discard its
current gains and re-run the relay-feedback bootstrap. It is created for any
climate entity configured with a true-temperature sensor, regardless of whether
the device exposes a native button DP.
"""
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_ID,
    CONF_PLATFORM,
)
from homeassistant.helpers.entity import EntityCategory

from .const import (
    CONF_CURRENT_TEMPERATURE_DP,
    CONF_MODEL,
    CONF_PROTOCOL_VERSION,
    CONF_TRUE_TEMPERATURE_ENTITY,
    DOMAIN as LOCALTUYA_DOMAIN,
)
from .pid import get_overshoot_state

_LOGGER = logging.getLogger(__name__)


class LocaltuyaRetuneButton(ButtonEntity):
    """Press to restart the climate PID relay-feedback auto-tune."""

    def __init__(self, dev_entry, climate_config):
        super().__init__()
        self._dev_entry = dev_entry
        self._climate_config = climate_config
        self._dp_key = str(climate_config[CONF_ID])

    def _climate(self):
        state = get_overshoot_state(
            self.hass,
            LOCALTUYA_DOMAIN,
            self._dev_entry[CONF_DEVICE_ID],
            self._dp_key,
        )
        return state.get("climate")

    async def async_press(self):
        climate = self._climate()
        if climate is not None:
            climate.force_retune()
        else:
            _LOGGER.warning("Re-tune pressed but climate entity is not ready")

    @property
    def unique_id(self):
        return f"local_{self._dev_entry[CONF_DEVICE_ID]}_{self._dp_key}_pid_retune"

    @property
    def name(self):
        return f"{self._climate_config[CONF_FRIENDLY_NAME]} Re-tune PID"

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


class LocaltuyaStopTuningButton(LocaltuyaRetuneButton):
    """Press to freeze the current PID gains (stop relay tuning + refinement)."""

    async def async_press(self):
        climate = self._climate()
        if climate is not None:
            climate.stop_tuning()
        else:
            _LOGGER.warning("Stop-tuning pressed but climate entity is not ready")

    @property
    def unique_id(self):
        return f"local_{self._dev_entry[CONF_DEVICE_ID]}_{self._dp_key}_pid_stop_tuning"

    @property
    def name(self):
        return f"{self._climate_config[CONF_FRIENDLY_NAME]} Stop Tuning"


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up PID re-tune / stop-tuning buttons for true-temp climates."""
    entities = []
    for dev_entry in config_entry.data.get(CONF_DEVICES, {}).values():
        for entity_config in dev_entry.get(CONF_ENTITIES, []):
            if (
                entity_config.get(CONF_PLATFORM) == CLIMATE_DOMAIN
                and entity_config.get(CONF_TRUE_TEMPERATURE_ENTITY)
                and entity_config.get(CONF_CURRENT_TEMPERATURE_DP)
            ):
                entities.append(LocaltuyaRetuneButton(dev_entry, entity_config))
                entities.append(LocaltuyaStopTuningButton(dev_entry, entity_config))

    if entities:
        async_add_entities(entities)
