"""Support for LK Systems Cubic Secure pause duration configuration."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import StateType

from . import LKSystemCoordinator
from .const import CUBIC_SECURE_MODEL, DOMAIN, MANUFACTURER

_LOGGER = logging.getLogger(__name__)

# Bounds for the default pause duration, in minutes.
DEFAULT_PAUSE_MINUTES = 60
MIN_PAUSE_MINUTES = 5
MAX_PAUSE_MINUTES = 1440
PAUSE_MINUTES_STEP = 5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LK Systems Cubic Secure pause duration entities."""
    coordinator: LKSystemCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = {
        device_identity: LKCubicSecurePauseDuration(coordinator, device_identity)
        for device_identity in coordinator.data.get("cubic_devices", {})
    }
    # Keep live references so the leak detection switch (switch.py) can read
    # the user's preferred pause duration at turn_off time.
    coordinator.pause_duration_entities = entities
    async_add_entities(entities.values())


class LKCubicSecurePauseDuration(NumberEntity, RestoreEntity):
    """User-configurable default pause duration for leak detection.

    The leak detection switch (switch.py) reads this value when it is told
    to pause without an explicit duration.
    """

    _attr_has_entity_name = True
    _attr_name = "Pause duration"
    _attr_icon = "mdi:timer-settings-outline"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = MIN_PAUSE_MINUTES
    _attr_native_max_value = MAX_PAUSE_MINUTES
    _attr_native_step = PAUSE_MINUTES_STEP
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: LKSystemCoordinator,
        device_identity: str,
    ) -> None:
        """Initialize the pause duration entity."""
        super().__init__()
        self.coordinator = coordinator
        self._device_identity = device_identity
        self._attr_unique_id = f"LkUid_pause_duration_{device_identity}"

        # None until the user (or a restored state) supplies a value; the
        # native_value property falls back to the default in the meantime.
        self._value: float | None = None

        machine_info = coordinator.data["cubic_devices"][device_identity][
            "machine_info"
        ]
        zone_name = machine_info.get("zone", {}).get("zoneName")
        device_name = (
            f"Cubic Secure {zone_name}" if zone_name else "Cubic Secure"
        )

        # These identifiers deliberately match the leak detection switch so
        # the number attaches to the existing Cubic Secure device in HA
        # rather than creating a duplicate device.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_identity)},
            manufacturer=MANUFACTURER,
            model=CUBIC_SECURE_MODEL,
            name=device_name,
            serial_number=device_identity,
        )

    @property
    def native_value(self) -> StateType:
        """Return the configured pause duration in minutes."""
        if self._value is None:
            return DEFAULT_PAUSE_MINUTES
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        """Store a new pause duration in minutes."""
        self._value = value
        _LOGGER.info(
            "Leak detection pause duration for %s set to %.0f minutes",
            self._device_identity,
            value,
        )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore the last configured value after a restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            try:
                self._value = float(last_state.state)
            except ValueError:
                _LOGGER.debug(
                    "Could not restore pause duration from state %r",
                    last_state.state,
                )

    @callback
    async def async_will_remove_from_hass(self) -> None:
        """Clear the live reference when the entity is removed."""
        self.coordinator.pause_duration_entities.pop(
            self._device_identity, None
        )
