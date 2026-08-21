"""Support for LK Systems Cubic Secure valves."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import LKSystemCoordinator
from .const import CUBIC_SECURE_MODEL, DOMAIN, MANUFACTURER

_LOGGER = logging.getLogger(__name__)

# Give Cubic Secure and the LK cloud a moment to apply the command before
# asking for a fresh configuration. This avoids waiting for the normal
# coordinator polling interval while not repeatedly polling the cloud API.
VALVE_REFRESH_DELAY = 15


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LK Systems Cubic Secure valve entities."""
    coordinator: LKSystemCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        LKCubicSecureValve(coordinator, device_identity)
        for device_identity in coordinator.data.get("cubic_devices", {})
    )


class LKCubicSecureValve(CoordinatorEntity[LKSystemCoordinator], ValveEntity):
    """Representation of an LK Systems Cubic Secure shutoff valve."""

    _attr_device_class = ValveDeviceClass.WATER
    _attr_has_entity_name = True
    _attr_name = "Valve"
    _attr_reports_position = False
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE

    def __init__(
        self,
        coordinator: LKSystemCoordinator,
        device_identity: str,
    ) -> None:
        """Initialize the valve entity."""
        super().__init__(coordinator)
        self._device_identity = device_identity
        self._attr_unique_id = f"LkUid_valve_{device_identity}"
        self._action_in_progress = False
        self._expected_action: str | None = None
        self._last_action: str | None = None

        machine_info = coordinator.data["cubic_devices"][device_identity][
            "machine_info"
        ]
        zone_name = machine_info.get("zone", {}).get("zoneName")
        device_name = (
            f"Cubic Secure {zone_name}" if zone_name else "Cubic Secure"
        )

        # These identifiers deliberately match AbstractLkCubicSensor so the
        # valve is attached to the existing Cubic Secure device in HA rather
        # than creating a duplicate device.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_identity)},
            manufacturer=MANUFACTURER,
            model=CUBIC_SECURE_MODEL,
            name=device_name,
            serial_number=device_identity,
        )

    @property
    def _raw_valve_state(self) -> str | None:
        """Return the normalized valve state from Cubic Secure config."""
        configuration = (
            self.coordinator.data.get("cubic_devices", {})
            .get(self._device_identity, {})
            .get("configuration")
            or {}
        )
        state = configuration.get("valveState")
        return str(state).lower() if state is not None else None

    @property
    def available(self) -> bool:
        """Return whether the valve is available."""
        return self.coordinator.last_update_success and (
            self._device_identity
            in self.coordinator.data.get("cubic_devices", {})
        )

    @property
    def state(self) -> str | None:
        """Return the valve state, using action intent during transition."""
        if self._action_in_progress:
            if self._expected_action == "closing":
                _LOGGER.debug("state=closed (in_progress closing)")
                return "closed"
            if self._expected_action == "opening":
                _LOGGER.debug("state=open (in_progress opening)")
                return "open"
        if self._last_action == "close_valve":
            _LOGGER.debug("state=closed (last_action close_valve)")
            return "closed"
        if self._last_action == "open_valve":
            _LOGGER.debug("state=open (last_action open_valve)")
            return "open"
        result = super().state
        _LOGGER.debug("state=%s (fallback, _raw=%s, _last=%s)", result, self._raw_valve_state, self._last_action)
        return result

    @property
    def is_closed(self) -> bool | None:
        """Return whether the valve is closed."""
        state = self._raw_valve_state
        if state == "closed":
            return True
        if state == "open":
            return False
        return None

    @property
    def is_opening(self) -> bool | None:
        """Return whether the valve is opening."""
        if self._action_in_progress and self._expected_action == "opening":
            return True
        state = self._raw_valve_state
        if state is None:
            return None
        return state == "opening"

    @property
    def is_closing(self) -> bool | None:
        """Return whether the valve is closing."""
        if self._action_in_progress and self._expected_action == "closing":
            return True
        state = self._raw_valve_state
        if state is None:
            return None
        return state == "closing"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Clear action-in-progress flags after coordinator refresh."""
        self._last_action = None
        if self._action_in_progress:
            self._action_in_progress = False
            self._expected_action = None
            self.async_write_ha_state()
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return status attribute during valve transition."""
        if self._action_in_progress and self._expected_action:
            return {
                "status": f"{self._expected_action.capitalize()} — waiting for cloud",
            }
        return {}

    def _device_registry_id(self) -> str:
        """Resolve the existing Home Assistant device registry ID."""
        device_entry = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, self._device_identity)}
        )
        if device_entry is None:
            raise HomeAssistantError(
                f"No Home Assistant device found for Cubic Secure "
                f"{self._device_identity}"
            )
        return device_entry.id

    async def _async_set_valve(self, service: str) -> None:
        """Run the existing LK valve service and refresh its state."""
        _LOGGER.info("Valve action started: %s", service)
        self._action_in_progress = True
        self._expected_action = "opening" if service == "open_valve" else "closing"
        self.async_write_ha_state()

        await self.hass.services.async_call(
            DOMAIN,
            service,
            {"device_id": self._device_registry_id()},
            blocking=True,
        )

        # Record the action intent *before* the sleep so the valve entity
        # reflects the commanded state during the entire transition window.
        # The LK cloud may still report the old state for ~10 s after the
        # physical valve moves, so we deliberately skip a coordinator
        # refresh. The existing valve-state sensor will pick up the new
        # state on the next normal coordinator poll.
        self._last_action = service
        _LOGGER.info("Valve action complete, _last_action=%s, state=%s", self._last_action, self.state)
        self.async_write_ha_state()

        await asyncio.sleep(VALVE_REFRESH_DELAY)
        _LOGGER.info("After sleep: _last_action=%s, state=%s", self._last_action, self.state)
        self._action_in_progress = False
        self._expected_action = None
        self.async_write_ha_state()

    async def async_open_valve(self) -> None:
        """Open the Cubic Secure valve."""
        _LOGGER.info("Opening Cubic Secure valve %s", self._device_identity)
        await self._async_set_valve("open_valve")

    async def async_close_valve(self) -> None:
        """Close the Cubic Secure valve."""
        _LOGGER.info("Closing Cubic Secure valve %s", self._device_identity)
        await self._async_set_valve("close_valve")
