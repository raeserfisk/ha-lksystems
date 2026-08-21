"""Support for LK Systems Cubic Secure valves."""

from __future__ import annotations

import logging
import time

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import LKSystemCoordinator
from .const import CUBIC_SECURE_MODEL, DOMAIN, MANUFACTURER
from .pylksystems import LKSystemsManager

_LOGGER = logging.getLogger(__name__)

# LK's cloud can briefly report the pre-command valve state even after a
# successful physical operation. During this window, a contradictory cloud
# value is treated as stale. A matching cloud value confirms the command
# immediately, and any contradictory value after the grace period wins.
VALVE_RECONCILIATION_GRACE = 60


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

        self._effective_state = self._cloud_valve_state
        self._state_source = "cloud" if self._effective_state is not None else "unknown"
        self._last_command_at: float | None = None
        self._action_in_progress: str | None = None

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
    def _cloud_valve_state(self) -> str | None:
        """Return the normalized valve state last reported by LK cloud."""
        configuration = (
            self.coordinator.data.get("cubic_devices", {})
            .get(self._device_identity, {})
            .get("configuration")
            or {}
        )
        state = configuration.get("valveState")
        if state is None:
            return None
        normalized = str(state).lower()
        return normalized if normalized in {"open", "closed"} else None

    def _reconcile_with_cloud(self) -> None:
        """Reconcile the effective local state with the latest cloud value."""
        cloud_state = self._cloud_valve_state
        if cloud_state is None:
            return

        if self._effective_state is None:
            self._effective_state = cloud_state
            self._state_source = "cloud"
            self._last_command_at = None
            return

        # A matching cloud value confirms a recent local command immediately.
        if cloud_state == self._effective_state:
            self._state_source = "cloud"
            self._last_command_at = None
            return

        if self._last_command_at is not None:
            command_age = time.monotonic() - self._last_command_at
            if command_age < VALVE_RECONCILIATION_GRACE:
                _LOGGER.debug(
                    "Ignoring stale Cubic Secure cloud state %s for %s; "
                    "effective state is %s and command is %.1fs old",
                    cloud_state,
                    self._device_identity,
                    self._effective_state,
                    command_age,
                )
                return

        _LOGGER.warning(
            "Cubic Secure cloud state corrected effective valve state for %s: %s -> %s",
            self._device_identity,
            self._effective_state,
            cloud_state,
        )
        self._effective_state = cloud_state
        self._state_source = "cloud"
        self._last_command_at = None

    @property
    def available(self) -> bool:
        """Return whether the valve is available."""
        return self.coordinator.last_update_success and (
            self._device_identity
            in self.coordinator.data.get("cubic_devices", {})
        )

    @property
    def is_closed(self) -> bool | None:
        """Return whether the valve is effectively closed."""
        if self._effective_state == "closed":
            return True
        if self._effective_state == "open":
            return False
        return None

    @property
    def is_opening(self) -> bool | None:
        """Return whether an open command is currently being sent."""
        return self._action_in_progress == "opening"

    @property
    def is_closing(self) -> bool | None:
        """Return whether a close command is currently being sent."""
        return self._action_in_progress == "closing"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Reconcile effective state whenever LK cloud data is refreshed."""
        self._reconcile_with_cloud()
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        """Expose effective and cloud states for diagnostics."""
        return {
            "effective_state": self._effective_state,
            "cloud_state": self._cloud_valve_state,
            "state_source": self._state_source,
            "reconciliation_grace_seconds": VALVE_RECONCILIATION_GRACE,
        }

    async def _send_valve_command(self, target_state: str) -> None:
        """Send a checked command directly to LK Systems."""
        username = self.coordinator._entry.data.get(CONF_USERNAME)
        password = self.coordinator._entry.data.get(CONF_PASSWORD)

        try:
            async with LKSystemsManager(username, password) as lk_inst:
                if not await lk_inst.login():
                    raise HomeAssistantError("Failed to login to LK Systems")

                if target_state == "open":
                    result = await lk_inst.cubic_secure_open_valve(
                        self._device_identity
                    )
                else:
                    result = await lk_inst.cubic_secure_close_valve(
                        self._device_identity
                    )

                if result is False:
                    raise HomeAssistantError(
                        f"LK Systems rejected {target_state} command for valve "
                        f"{self._device_identity}"
                    )
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to {target_state} Cubic Secure valve "
                f"{self._device_identity}"
            ) from err

    async def _async_set_valve(self, target_state: str) -> None:
        """Send the valve command and update the effective local state."""
        action = "opening" if target_state == "open" else "closing"
        self._action_in_progress = action
        self.async_write_ha_state()

        try:
            await self._send_valve_command(target_state)
        except Exception:
            # A failed command must never change the effective state.
            self._action_in_progress = None
            self.async_write_ha_state()
            raise

        self._effective_state = target_state
        self._state_source = "command"
        self._last_command_at = time.monotonic()
        self._action_in_progress = None
        _LOGGER.info(
            "Cubic Secure valve %s effective state set to %s after successful command",
            self._device_identity,
            target_state,
        )
        self.async_write_ha_state()

    async def async_open_valve(self) -> None:
        """Open the Cubic Secure valve."""
        await self._async_set_valve("open")

    async def async_close_valve(self) -> None:
        """Close the Cubic Secure valve."""
        await self._async_set_valve("closed")
