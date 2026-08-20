"""Tests for the LK Systems Cubic Secure valve platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.valve import DOMAIN as VALVE_DOMAIN
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.lksystems.const import DOMAIN

from .conftest import (
    CUBIC_IDENTITY,
    build_cubic_configuration,
    entity_id,
    setup_entry,
)

VALVE_UNIQUE_ID = f"LkUid_valve_{CUBIC_IDENTITY}"
VALVE_STATE_SENSOR_UNIQUE_ID = f"LkUid_valveState_{CUBIC_IDENTITY}"


async def test_cubic_secure_exposes_native_valve(hass, fake_manager):
    """A Cubic Secure device exposes a native water valve entity."""
    await setup_entry(hass, fake_manager)

    valve_entity_id = entity_id(hass, VALVE_DOMAIN, VALVE_UNIQUE_ID)
    state = hass.states.get(valve_entity_id)

    assert state is not None
    assert state.state == "open"
    assert state.attributes["device_class"] == "water"

    # The valve and the existing valve-state sensor must attach to the same
    # Cubic Secure device rather than creating a second HA device.
    entity_registry = er.async_get(hass)
    valve_entry = entity_registry.async_get(valve_entity_id)
    state_sensor_id = entity_id(
        hass, "sensor", VALVE_STATE_SENSOR_UNIQUE_ID
    )
    state_sensor_entry = entity_registry.async_get(state_sensor_id)

    assert valve_entry is not None
    assert state_sensor_entry is not None
    assert valve_entry.device_id == state_sensor_entry.device_id

    device_entry = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, CUBIC_IDENTITY)}
    )
    assert device_entry is not None
    assert valve_entry.device_id == device_entry.id


async def test_close_valve_refreshes_cubic_state_immediately(hass, fake_manager):
    """Closing the native valve refreshes configuration without waiting."""
    await setup_entry(hass, fake_manager)
    valve_entity_id = entity_id(hass, VALVE_DOMAIN, VALVE_UNIQUE_ID)

    # The initial setup reports open. Make the next coordinator refresh
    # represent the state returned by LK after the close command.
    fake_manager.cubic_configuration_data = build_cubic_configuration("closed")

    with (
        patch(
            "custom_components.lksystems.services.LKSystemsManager",
            return_value=fake_manager,
        ),
        patch(
            "custom_components.lksystems.LKSystemsManager",
            return_value=fake_manager,
        ),
        patch(
            "custom_components.lksystems.valve.asyncio.sleep",
            new=AsyncMock(),
        ),
    ):
        await hass.services.async_call(
            VALVE_DOMAIN,
            "close_valve",
            {"entity_id": valve_entity_id},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert ("cubic_secure_close_valve", CUBIC_IDENTITY) in fake_manager.calls
    assert hass.states[valve_entity_id].state == "closed"

    # At least one configuration request happened during initial setup and
    # another must have happened as part of the post-command refresh.
    config_calls = [
        call
        for call in fake_manager.calls
        if call[0] == "get_cubic_secure_configuration"
    ]
    assert len(config_calls) >= 2
