"""Tests for the LK Systems Cubic Secure pause duration number platform."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.helpers import entity_registry as er

from custom_components.lksystems.const import DOMAIN

from .conftest import (
    CUBIC_IDENTITY,
    entity_id,
    setup_entry,
)

NUMBER_UNIQUE_ID = f"LkUid_pause_duration_{CUBIC_IDENTITY}"
SWITCH_UNIQUE_ID = f"LkUid_leak_detection_{CUBIC_IDENTITY}"


def pause_duration_entity_id(hass) -> str:
    """Return the entity_id of the pause duration number entity."""
    return entity_id(hass, NUMBER_DOMAIN, NUMBER_UNIQUE_ID)


def leak_detection_entity(hass):
    """Return the live leak detection switch entity for the test device."""
    coordinator = next(iter(hass.data[DOMAIN].values()))
    return coordinator.leak_detection_entities[CUBIC_IDENTITY]


async def test_cubic_secure_exposes_pause_duration_number(hass, fake_manager):
    """A Cubic Secure device exposes a pause duration number entity."""
    await setup_entry(hass, fake_manager)

    number_entity_id = pause_duration_entity_id(hass)
    state = hass.states.get(number_entity_id)

    assert state is not None
    assert state.state == "60"
    assert state.attributes["min"] == 5
    assert state.attributes["max"] == 1440
    assert state.attributes["step"] == 5
    assert state.attributes["unit_of_measurement"] == "min"

    # The number and the leak detection switch must attach to the same
    # Cubic Secure device rather than creating a second HA device.
    entity_registry = er.async_get(hass)
    number_entry = entity_registry.async_get(number_entity_id)
    switch_entity_id = entity_id(hass, SWITCH_DOMAIN, SWITCH_UNIQUE_ID)
    switch_entry = entity_registry.async_get(switch_entity_id)

    assert number_entry is not None
    assert switch_entry is not None
    assert number_entry.device_id == switch_entry.device_id


async def test_pause_duration_set_value(hass, fake_manager):
    """Setting the pause duration updates the entity state."""
    await setup_entry(hass, fake_manager)
    number_entity_id = pause_duration_entity_id(hass)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        "set_value",
        {"entity_id": number_entity_id, "value": 30},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(number_entity_id)
    assert state is not None
    assert state.state == "30.0"


async def test_switch_turn_off_uses_number_value(hass, fake_manager):
    """Without explicit seconds, the number entity supplies the duration."""
    await setup_entry(hass, fake_manager)
    number_entity_id = pause_duration_entity_id(hass)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        "set_value",
        {"entity_id": number_entity_id, "value": 10},
        blocking=True,
    )
    await hass.async_block_till_done()

    with patch(
        "custom_components.lksystems.switch.LKSystemsManager",
        return_value=fake_manager,
    ):
        await leak_detection_entity(hass).async_turn_off()
        await hass.async_block_till_done()

    assert ("cubic_secure_pause_leak_detection", CUBIC_IDENTITY, 600) in (
        fake_manager.calls
    )


async def test_pause_duration_is_restored_after_restart(hass, fake_manager):
    """A user-chosen pause duration survives an unload/reload cycle."""
    entry = await setup_entry(hass, fake_manager)
    number_entity_id = pause_duration_entity_id(hass)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        "set_value",
        {"entity_id": number_entity_id, "value": 25},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with patch("custom_components.lksystems.LKSystemsManager", return_value=fake_manager):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(number_entity_id)
    assert state is not None
    assert state.state == "25.0"
