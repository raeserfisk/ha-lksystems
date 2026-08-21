"""Tests for the LK Systems Cubic Secure valve platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.valve import DOMAIN as VALVE_DOMAIN
from homeassistant.exceptions import HomeAssistantError
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
    assert state.attributes["effective_state"] == "open"
    assert state.attributes["cloud_state"] == "open"
    assert state.attributes["state_source"] == "cloud"

    # The valve and the existing valve-state sensor must attach to the same
    # Cubic Secure device rather than creating a second HA device.
    entity_registry = er.async_get(hass)
    valve_entry = entity_registry.async_get(valve_entity_id)
    state_sensor_id = entity_id(hass, "sensor", VALVE_STATE_SENSOR_UNIQUE_ID)
    state_sensor_entry = entity_registry.async_get(state_sensor_id)

    assert valve_entry is not None
    assert state_sensor_entry is not None
    assert valve_entry.device_id == state_sensor_entry.device_id

    device_entry = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, CUBIC_IDENTITY)}
    )
    assert device_entry is not None
    assert valve_entry.device_id == device_entry.id


async def test_successful_close_updates_effective_state_immediately(hass, fake_manager):
    """A successful close command updates local state without cloud polling."""
    await setup_entry(hass, fake_manager)
    valve_entity_id = entity_id(hass, VALVE_DOMAIN, VALVE_UNIQUE_ID)

    config_calls_before = sum(
        call[0] == "get_cubic_secure_configuration" for call in fake_manager.calls
    )

    with patch(
        "custom_components.lksystems.valve.LKSystemsManager",
        return_value=fake_manager,
    ):
        await hass.services.async_call(
            VALVE_DOMAIN,
            "close_valve",
            {"entity_id": valve_entity_id},
            blocking=True,
        )
        await hass.async_block_till_done()

    state = hass.states[valve_entity_id]
    assert ("cubic_secure_close_valve", CUBIC_IDENTITY) in fake_manager.calls
    assert state.state == "closed"
    assert state.attributes["effective_state"] == "closed"
    assert state.attributes["cloud_state"] == "open"
    assert state.attributes["state_source"] == "command"

    config_calls_after = sum(
        call[0] == "get_cubic_secure_configuration" for call in fake_manager.calls
    )
    assert config_calls_after == config_calls_before


async def test_stale_cloud_state_does_not_undo_recent_command(hass, fake_manager):
    """A contradictory cloud value is ignored during the grace period."""
    entry = await setup_entry(hass, fake_manager)
    valve_entity_id = entity_id(hass, VALVE_DOMAIN, VALVE_UNIQUE_ID)

    with patch(
        "custom_components.lksystems.valve.LKSystemsManager",
        return_value=fake_manager,
    ):
        await hass.services.async_call(
            VALVE_DOMAIN,
            "close_valve",
            {"entity_id": valve_entity_id},
            blocking=True,
        )

    # LK still reports the old open state. A coordinator refresh inside the
    # grace period must not snap the valve UI back to open.
    fake_manager.cubic_configuration_data = build_cubic_configuration("open")
    coordinator = hass.data[DOMAIN][entry.entry_id]
    with patch(
        "custom_components.lksystems.LKSystemsManager",
        return_value=fake_manager,
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    state = hass.states[valve_entity_id]
    assert state.state == "closed"
    assert state.attributes["effective_state"] == "closed"
    assert state.attributes["cloud_state"] == "open"
    assert state.attributes["state_source"] == "command"


async def test_cloud_corrects_effective_state_after_grace(hass, fake_manager):
    """After the grace period, contradictory cloud state becomes authoritative."""
    entry = await setup_entry(hass, fake_manager)
    valve_entity_id = entity_id(hass, VALVE_DOMAIN, VALVE_UNIQUE_ID)

    with patch(
        "custom_components.lksystems.valve.LKSystemsManager",
        return_value=fake_manager,
    ):
        await hass.services.async_call(
            VALVE_DOMAIN,
            "close_valve",
            {"entity_id": valve_entity_id},
            blocking=True,
        )

    fake_manager.cubic_configuration_data = build_cubic_configuration("open")
    coordinator = hass.data[DOMAIN][entry.entry_id]
    with (
        patch(
            "custom_components.lksystems.LKSystemsManager",
            return_value=fake_manager,
        ),
        patch(
            "custom_components.lksystems.valve.VALVE_RECONCILIATION_GRACE",
            0,
        ),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    state = hass.states[valve_entity_id]
    assert state.state == "open"
    assert state.attributes["effective_state"] == "open"
    assert state.attributes["cloud_state"] == "open"
    assert state.attributes["state_source"] == "cloud"


async def test_failed_close_does_not_change_effective_state(hass, fake_manager):
    """A rejected LK command must leave the effective valve state unchanged."""
    await setup_entry(hass, fake_manager)
    valve_entity_id = entity_id(hass, VALVE_DOMAIN, VALVE_UNIQUE_ID)
    fake_manager.cubic_secure_close_valve = AsyncMock(return_value=False)

    with patch(
        "custom_components.lksystems.valve.LKSystemsManager",
        return_value=fake_manager,
    ):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                VALVE_DOMAIN,
                "close_valve",
                {"entity_id": valve_entity_id},
                blocking=True,
            )

    state = hass.states[valve_entity_id]
    assert state.state == "open"
    assert state.attributes["effective_state"] == "open"
    assert state.attributes["state_source"] == "cloud"
