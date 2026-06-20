"""Platform to locally control Tuya-based switch devices."""
import logging
from functools import partial

import voluptuous as vol
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.switch import DOMAIN, SwitchEntity
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_ID,
    CONF_PLATFORM,
    STATE_ON,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .common import LocalTuyaEntity, async_setup_entry as _generic_setup_entry
from .const import (
    ATTR_CURRENT,
    ATTR_CURRENT_CONSUMPTION,
    ATTR_STATE,
    ATTR_VOLTAGE,
    CONF_CURRENT,
    CONF_CURRENT_CONSUMPTION,
    CONF_CURRENT_TEMPERATURE_DP,
    CONF_DEFAULT_VALUE,
    CONF_MODEL,
    CONF_PASSIVE_ENTITY,
    CONF_POWER_LEVEL_ENTITY,
    CONF_PROTOCOL_VERSION,
    CONF_RESTORE_ON_RECONNECT,
    CONF_TRUE_TEMPERATURE_ENTITY,
    CONF_VOLTAGE,
    DOMAIN as LOCALTUYA_DOMAIN,
    TUYA_DEVICES,
)
from .pid import get_overshoot_state

_LOGGER = logging.getLogger(__name__)


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        vol.Optional(CONF_CURRENT): vol.In(dps),
        vol.Optional(CONF_CURRENT_CONSUMPTION): vol.In(dps),
        vol.Optional(CONF_VOLTAGE): vol.In(dps),
        vol.Required(CONF_RESTORE_ON_RECONNECT): bool,
        vol.Required(CONF_PASSIVE_ENTITY): bool,
        vol.Optional(CONF_DEFAULT_VALUE): str,
    }


class LocaltuyaSwitch(LocalTuyaEntity, SwitchEntity):
    """Representation of a Tuya switch."""

    def __init__(
        self,
        device,
        config_entry,
        switchid,
        **kwargs,
    ):
        """Initialize the Tuya switch."""
        super().__init__(device, config_entry, switchid, _LOGGER, **kwargs)
        self._state = None
        _LOGGER.debug("Initialized switch [%s]", self.name)

    @property
    def is_on(self):
        """Check if Tuya switch is on."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return device state attributes."""
        attrs = {}
        if self.has_config(CONF_CURRENT):
            attrs[ATTR_CURRENT] = self.dps(self._config[CONF_CURRENT])
        if self.has_config(CONF_CURRENT_CONSUMPTION):
            attrs[ATTR_CURRENT_CONSUMPTION] = (
                self.dps(self._config[CONF_CURRENT_CONSUMPTION]) / 10
            )
        if self.has_config(CONF_VOLTAGE):
            attrs[ATTR_VOLTAGE] = self.dps(self._config[CONF_VOLTAGE]) / 10

        # Store the state
        if self._state is not None:
            attrs[ATTR_STATE] = self._state
        elif self._last_state is not None:
            attrs[ATTR_STATE] = self._last_state
        return attrs

    async def async_turn_on(self, **kwargs):
        """Turn Tuya switch on."""
        await self._device.set_dp(True, self._dp_id)

    async def async_turn_off(self, **kwargs):
        """Turn Tuya switch off."""
        await self._device.set_dp(False, self._dp_id)

    # Default value is the "OFF" state
    def entity_default_value(self):
        """Return False as the default value for this entity type."""
        return False


class LocaltuyaOvershootSwitch(RestoreEntity, SwitchEntity):
    """Companion toggle that arms the climate PID overshoot cutoff.

    Defaults to on. When off, the PID keeps adjusting the setpoint but never
    forces the unit off to curb overshoot. State is shared with the climate
    entity through ``hass.data`` so the two platforms stay in sync.
    """

    def __init__(self, dev_entry, climate_config):
        super().__init__()
        self._dev_entry = dev_entry
        self._climate_config = climate_config
        self._dp_key = str(climate_config[CONF_ID])
        self._is_on = True
        self._state_obj = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._state_obj = get_overshoot_state(
            self.hass,
            LOCALTUYA_DOMAIN,
            self._dev_entry[CONF_DEVICE_ID],
            self._dp_key,
        )
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._is_on = last_state.state == STATE_ON
        self._apply(self._is_on)

    def _apply(self, enabled):
        self._is_on = enabled
        self._state_obj["enabled"] = enabled
        climate = self._state_obj.get("climate")
        if climate is not None:
            climate.on_overshoot_cutoff_changed(enabled)

    async def async_turn_on(self, **kwargs):
        self._apply(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        self._apply(False)
        self.async_write_ha_state()

    @property
    def is_on(self):
        return self._is_on

    @property
    def unique_id(self):
        return f"local_{self._dev_entry[CONF_DEVICE_ID]}_{self._dp_key}_overshoot_cutoff"

    @property
    def name(self):
        return f"{self._climate_config[CONF_FRIENDLY_NAME]} Turn Off If Overshoot"

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


class LocaltuyaTrueAutoSwitch(RestoreEntity, SwitchEntity):
    """Companion toggle enabling autonomous heat/cool/off ('True Auto').

    Defaults to off. When on, the climate entity drives the device between heat,
    cool and off itself (with valve-protection dwell) and presents AUTO in the
    UI. State is shared with the climate entity through ``hass.data``.
    """

    def __init__(self, dev_entry, climate_config):
        super().__init__()
        self._dev_entry = dev_entry
        self._climate_config = climate_config
        self._dp_key = str(climate_config[CONF_ID])
        self._is_on = False
        self._state_obj = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._state_obj = get_overshoot_state(
            self.hass,
            LOCALTUYA_DOMAIN,
            self._dev_entry[CONF_DEVICE_ID],
            self._dp_key,
        )
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._is_on = last_state.state == STATE_ON
        self._apply(self._is_on)

    def _apply(self, enabled):
        self._is_on = enabled
        self._state_obj["true_auto"] = enabled
        climate = self._state_obj.get("climate")
        if climate is not None:
            climate.on_true_auto_changed(enabled)

    async def async_turn_on(self, **kwargs):
        self._apply(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        self._apply(False)
        self.async_write_ha_state()

    @property
    def is_on(self):
        return self._is_on

    @property
    def unique_id(self):
        return f"local_{self._dev_entry[CONF_DEVICE_ID]}_{self._dp_key}_true_auto"

    @property
    def name(self):
        return f"{self._climate_config[CONF_FRIENDLY_NAME]} True Auto"

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


class LocaltuyaPowerCascadeSwitch(RestoreEntity, SwitchEntity):
    """Companion toggle enabling the inner compressor-power cascade.

    Defaults to off. When on, an inner loop servos the setpoint so measured
    compressor power tracks a target from the (predictive) thermal loop - wrapping
    the AC's own controller and skipping the relay experiment. Requires a power
    input. State is shared with the climate entity through ``hass.data``.
    """

    def __init__(self, dev_entry, climate_config):
        super().__init__()
        self._dev_entry = dev_entry
        self._climate_config = climate_config
        self._dp_key = str(climate_config[CONF_ID])
        self._is_on = False
        self._state_obj = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._state_obj = get_overshoot_state(
            self.hass,
            LOCALTUYA_DOMAIN,
            self._dev_entry[CONF_DEVICE_ID],
            self._dp_key,
        )
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._is_on = last_state.state == STATE_ON
        self._apply(self._is_on)

    def _apply(self, enabled):
        self._is_on = enabled
        self._state_obj["power_cascade"] = enabled
        climate = self._state_obj.get("climate")
        if climate is not None:
            climate.on_power_cascade_changed(enabled)

    async def async_turn_on(self, **kwargs):
        self._apply(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        self._apply(False)
        self.async_write_ha_state()

    @property
    def is_on(self):
        return self._is_on

    @property
    def unique_id(self):
        return f"local_{self._dev_entry[CONF_DEVICE_ID]}_{self._dp_key}_power_cascade"

    @property
    def name(self):
        return f"{self._climate_config[CONF_FRIENDLY_NAME]} Power Cascade"

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


_setup_tuya_switches = partial(
    _generic_setup_entry, DOMAIN, LocaltuyaSwitch, flow_schema
)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up Tuya switches plus PID overshoot-cutoff toggles for climates."""
    # The switch platform may be force-loaded purely for the overshoot toggle
    # (no native switch DPs). The generic setup helper assumes at least one
    # matching entity, so only invoke it when native switches are configured.
    has_native_switch = any(
        entity.get(CONF_PLATFORM) == DOMAIN
        for dev_entry in config_entry.data.get(CONF_DEVICES, {}).values()
        for entity in dev_entry.get(CONF_ENTITIES, [])
    )
    if has_native_switch:
        await _setup_tuya_switches(hass, config_entry, async_add_entities)

    extra = []
    for dev_id, dev_entry in config_entry.data.get(CONF_DEVICES, {}).items():
        for entity_config in dev_entry.get(CONF_ENTITIES, []):
            if (
                entity_config.get(CONF_PLATFORM) == CLIMATE_DOMAIN
                and entity_config.get(CONF_TRUE_TEMPERATURE_ENTITY)
                and entity_config.get(CONF_CURRENT_TEMPERATURE_DP)
            ):
                extra.append(LocaltuyaOvershootSwitch(dev_entry, entity_config))
                extra.append(LocaltuyaTrueAutoSwitch(dev_entry, entity_config))
                if entity_config.get(CONF_POWER_LEVEL_ENTITY):
                    extra.append(
                        LocaltuyaPowerCascadeSwitch(dev_entry, entity_config)
                    )

    if extra:
        async_add_entities(extra)
