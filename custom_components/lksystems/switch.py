"""Support for LK Systems Cubic Secure leak detection control."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import LKSystemCoordinator
from .const import CUBIC_SECURE_MODEL, DOMAIN, MANUFACTURER
from .pylksystems import LKSystemsManager

_LOGGER = logging.getLogger(__name__)

# LK's cloud lags behind reality in both directions: a pause shows up as
# forceOpen a few minutes after it starts, and the flag can linger well
# past the pause's end. During this window after a local command, a
# contradictory cloud value is treated as stale. A matching cloud value
# confirms the command immediately, and any contradictory value after the
# grace period wins.
LEAK_DETECTION_RECONCILIATION_GRACE = 1800

# Fallback pause duration (seconds) when neither the service call nor the
# duration helper provides one.
DEFAULT_PAUSE_SECONDS = 3600

# Optional HA-side helper carrying the user's preferred pause duration in
# minutes. When present and usable, it supplies the default for turn_off.
PAUSE_DURATION_HELPER = "input_number.cubic_secure_pause_duration"

# LK's API has no dedicated resume endpoint; a one-second pause forces the
# device to emit a fresh report that clears the forceOpen flag.
RESUME_NUDGE_SECONDS = 1

# Delay before requesting a coordinator refresh after a resume, giving LK's
# cache time to turn over.
RESUME_CONFIRM_DELAY = 3


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LK Systems Cubic Secure leak detection entities."""
    coordinator: LKSystemCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = {
        device_identity: LKCubicSecureLeakDetection(coordinator, device_identity)
        for device_identity in coordinator.data.get("cubic_devices", {})
    }
    # Keep live references so the integration services (services.py) can
    # dispatch parameterized pauses/resumes straight to the entities.
    coordinator.leak_detection_entities = entities
    async_add_entities(entities.values())


class LKCubicSecureLeakDetection(
    CoordinatorEntity[LKSystemCoordinator], SwitchEntity
):
    """Representation of an LK Systems Cubic Secure leak detection state.

    ON means leak detection is active; OFF means it is paused. The pause
    is timed on LK's side, so the entity also tracks locally issued pauses
    exactly (the cloud only reports them with a lag).
    """

    _attr_has_entity_name = True
    _attr_name = "Leak detection"
    _attr_icon = "mdi:water-leak"

    def __init__(
        self,
        coordinator: LKSystemCoordinator,
        device_identity: str,
    ) -> None:
        """Initialize the leak detection entity."""
        super().__init__(coordinator)
        self._device_identity = device_identity
        self._attr_unique_id = f"LkUid_leak_detection_{device_identity}"

        # Start from the last cloud report: entities are created after the
        # coordinator's first refresh, so no update callback will fire for
        # it. _handle_coordinator_update keeps this reconciled afterwards.
        cloud_paused = self._cloud_paused
        if cloud_paused is not None:
            self._effective_on: bool | None = not cloud_paused
            self._state_source = "cloud"
        else:
            self._effective_on = None
            self._state_source = "unknown"
        self._last_command_at: float | None = None
        self._command_in_progress: str | None = None

        # Local pause bookkeeping: we know exactly when a pause we issued
        # ends, which the cloud only reports with a lag.
        self._pause_ends_at: datetime | None = None
        self._pause_seconds: int | None = None

        machine_info = coordinator.data["cubic_devices"][device_identity][
            "machine_info"
        ]
        zone_name = machine_info.get("zone", {}).get("zoneName")
        device_name = (
            f"Cubic Secure {zone_name}" if zone_name else "Cubic Secure"
        )

        # These identifiers deliberately match AbstractLkCubicSensor so the
        # switch is attached to the existing Cubic Secure device in HA rather
        # than creating a duplicate device.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_identity)},
            manufacturer=MANUFACTURER,
            model=CUBIC_SECURE_MODEL,
            name=device_name,
            serial_number=device_identity,
        )

    @property
    def _cloud_paused(self) -> bool | None:
        """Return whether LK cloud currently reports leak detection paused."""
        leak = (
            self.coordinator.data.get("cubic_devices", {})
            .get(self._device_identity, {})
            .get("last_measurement", {})
            .get("leak")
            or {}
        )
        state = leak.get("leakState")
        if state is None:
            return None
        return str(state).lower() == "forceopen"

    def _local_pause_active(self) -> bool:
        """Return whether a locally issued pause is still running."""
        return self._pause_ends_at is not None and dt_util.now() < self._pause_ends_at

    def _sync_local_pause_expiry(self) -> None:
        """Flip the effective state on when a locally issued pause expires."""
        if (
            self._pause_ends_at is not None
            and self._effective_on is False
            and dt_util.now() >= self._pause_ends_at
        ):
            self._effective_on = True
            self._state_source = "command"
            # Start a fresh grace window: the cloud's forceOpen flag
            # routinely lingers well past the pause's end.
            self._last_command_at = time.monotonic()
            _LOGGER.info(
                "Local leak detection pause expired for %s; effective state on",
                self._device_identity,
            )

    def _reconcile_with_cloud(self) -> None:
        """Reconcile the effective state with the latest cloud value."""
        self._sync_local_pause_expiry()
        cloud_paused = self._cloud_paused
        if cloud_paused is None:
            return

        cloud_on = not cloud_paused
        if self._effective_on is None:
            self._effective_on = cloud_on
            self._state_source = "cloud"
            self._last_command_at = None
            return

        # A matching cloud value confirms the current state immediately.
        if cloud_on == self._effective_on:
            self._state_source = "cloud"
            self._last_command_at = None
            return

        if self._last_command_at is not None:
            command_age = time.monotonic() - self._last_command_at
            if command_age < LEAK_DETECTION_RECONCILIATION_GRACE:
                _LOGGER.debug(
                    "Ignoring stale Cubic Secure cloud leak state for %s; "
                    "effective state is %s and command is %.1fs old",
                    self._device_identity,
                    "on" if self._effective_on else "off",
                    command_age,
                )
                return

        _LOGGER.warning(
            "Cubic Secure cloud state corrected effective leak detection "
            "state for %s: %s -> %s",
            self._device_identity,
            "on" if self._effective_on else "off",
            "on" if cloud_on else "off",
        )
        self._effective_on = cloud_on
        self._state_source = "cloud"
        self._last_command_at = None

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        return self.coordinator.last_update_success and (
            self._device_identity
            in self.coordinator.data.get("cubic_devices", {})
        )

    @property
    def is_on(self) -> bool:
        """Return whether leak detection is effectively active."""
        self._sync_local_pause_expiry()
        if self._effective_on is None:
            return True
        return self._effective_on

    @callback
    def _handle_coordinator_update(self) -> None:
        """Reconcile effective state whenever LK cloud data is refreshed."""
        self._reconcile_with_cloud()
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, str | int | bool | None]:
        """Expose effective, cloud and local pause data for diagnostics."""
        paused_until: str | None = None
        if self._pause_ends_at is not None:
            paused_until = self._pause_ends_at.isoformat()
        return {
            "cloud_paused": self._cloud_paused,
            "state_source": self._state_source,
            "command_in_progress": self._command_in_progress,
            "pause_seconds": self._pause_seconds,
            "paused_until": paused_until,
            "reconciliation_grace_seconds": LEAK_DETECTION_RECONCILIATION_GRACE,
        }

    def _default_pause_seconds(self) -> int:
        """Return the default pause duration from the duration helper, if any."""
        state = self.hass.states.get(PAUSE_DURATION_HELPER)
        if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            try:
                return max(int(float(state.state) * 60), 60)
            except ValueError:
                _LOGGER.debug(
                    "Could not parse %s value %r; using default pause duration",
                    PAUSE_DURATION_HELPER,
                    state.state,
                )
        return DEFAULT_PAUSE_SECONDS

    async def _send_pause_command(self, seconds: int) -> None:
        """Send a checked pause command directly to LK Systems."""
        username = self.coordinator._entry.data.get(CONF_USERNAME)
        password = self.coordinator._entry.data.get(CONF_PASSWORD)

        try:
            async with LKSystemsManager(username, password) as lk_inst:
                if not await lk_inst.login():
                    raise HomeAssistantError("Failed to login to LK Systems")

                result = await lk_inst.cubic_secure_pause_leak_detection(
                    self._device_identity, seconds
                )

                if result is False:
                    raise HomeAssistantError(
                        f"LK Systems rejected pause command for leak detection "
                        f"{self._device_identity}"
                    )
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to pause leak detection for {self._device_identity}"
            ) from err

    async def async_turn_off(self, **kwargs) -> None:
        """Pause leak detection for the requested (or default) duration."""
        if not self.is_on and self._local_pause_active():
            # Already paused by a local command - sending another pause
            # would just restart the timer on LK's side.
            _LOGGER.debug(
                "Leak detection for %s already paused locally; ignoring turn_off",
                self._device_identity,
            )
            return

        seconds = int(kwargs.get("seconds") or self._default_pause_seconds())

        self._command_in_progress = "pausing"
        self.async_write_ha_state()

        try:
            await self._send_pause_command(seconds)
        except Exception:
            # A failed command must never change the effective state.
            self._command_in_progress = None
            self.async_write_ha_state()
            raise

        self._effective_on = False
        self._state_source = "command"
        self._last_command_at = time.monotonic()
        pause_ends_at = dt_util.now() + timedelta(seconds=seconds)
        self._pause_ends_at = pause_ends_at
        self._pause_seconds = seconds
        self._command_in_progress = None
        _LOGGER.info(
            "Leak detection %s paused until %s (%d seconds)",
            self._device_identity,
            pause_ends_at.isoformat(),
            seconds,
        )
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        """Resume leak detection."""
        if self.is_on:
            _LOGGER.debug(
                "Leak detection for %s already active; ignoring turn_on",
                self._device_identity,
            )
            return

        self._command_in_progress = "resuming"
        self.async_write_ha_state()

        try:
            await self._send_pause_command(RESUME_NUDGE_SECONDS)
        except Exception:
            # A failed command must never change the effective state.
            self._command_in_progress = None
            self.async_write_ha_state()
            raise

        self._effective_on = True
        self._state_source = "command"
        self._last_command_at = time.monotonic()
        self._pause_ends_at = None
        self._pause_seconds = None
        self._command_in_progress = None
        _LOGGER.info(
            "Leak detection %s resumed after successful command",
            self._device_identity,
        )
        self.async_write_ha_state()

        # Pull a fresh cloud report a moment later so the state is
        # confirmed as soon as LK's cache turns over.
        self.hass.async_create_task(self._delayed_confirm())

    async def _delayed_confirm(self) -> None:
        """Request a coordinator refresh shortly after a resume command."""
        await asyncio.sleep(RESUME_CONFIRM_DELAY)
        try:
            self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.debug(
                "Post-resume refresh for %s failed: %s",
                self._device_identity,
                err,
            )
