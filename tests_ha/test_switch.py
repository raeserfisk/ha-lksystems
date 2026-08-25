"""Tests for the LK Systems Cubic Secure leak detection switch platform."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.lksystems import switch as switch_module
from custom_components.lksystems.const import DOMAIN

from .conftest import (
    CUBIC_IDENTITY,
    build_cubic_measurement,
    entity_id,
    setup_entry,
)

SWITCH_UNIQUE_ID = f"LkUid_leak_detection_{CUBIC_IDENTITY}"
VALVE_STATE_SENSOR_UNIQUE_ID = f"LkUid_valveState_{CUBIC_IDENTITY}"


def build_forceopen_measurement() -> dict:
    """A Cubic Secure measurement whose leak state is a paused (forceOpen) one."""
    data = build_cubic_measurement()
    data["leak"]["leakState"] = "forceOpen"
    return data


def leak_detection_entity(hass) -> switch_module.LKCubicSecureLeakDetection:
    """Return the live leak detection switch entity for the test device."""
    coordinator = next(iter(hass.data[DOMAIN].values()))
    return coordinator.leak_detection_entities[CUBIC_IDENTITY]


async def test_cubic_secure_exposes_leak_detection_switch(hass, fake_manager):
    """A Cubic Secure device exposes a leak detection switch entity."""
    await setup_entry(hass, fake_manager)

    switch_entity_id = entity_id(hass, SWITCH_DOMAIN, SWITCH_UNIQUE_ID)
    state = hass.states.get(switch_entity_id)

    assert state is not None
    assert state.state == "on"
    assert state.attributes["state_source"] == "cloud"
    assert state.attributes["cloud_paused"] is False
    assert state.attributes["paused_until"] is None

    # The switch and the existing valve-state sensor must attach to the same
    # Cubic Secure device rather than creating a second HA device.
    entity_registry = er.async_get(hass)
    switch_entry = entity_registry.async_get(switch_entity_id)
    state_sensor_id = entity_id(hass, "sensor", VALVE_STATE_SENSOR_UNIQUE_ID)
    state_sensor_entry = entity_registry.async_get(state_sensor_id)

    assert switch_entry is not None
    assert state_sensor_entry is not None
    assert switch_entry.device_id == state_sensor_entry.device_id


async def test_cloud_forceopen_reports_off(hass, fake_manager):
    """A cloud-reported pause (forceOpen) shows as off."""
    await setup_entry(hass, fake_manager)
    switch_entity_id = entity_id(hass, SWITCH_DOMAIN, SWITCH_UNIQUE_ID)

    fake_manager.cubic_measurement_data = build_forceopen_measurement()
    coordinator = next(iter(hass.data[DOMAIN].values()))
    with patch(
        "custom_components.lksystems.LKSystemsManager",
        return_value=fake_manager,
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get(switch_entity_id)
    assert state is not None
    assert state.state == "off"
    assert state.attributes["state_source"] == "cloud"
    assert state.attributes["cloud_paused"] is True


async def test_turn_off_sends_pause_and_updates_state_immediately(hass, fake_manager):
    """A turn_off pauses on LK's side and flips the state without polling."""
    await setup_entry(hass, fake_manager)
    switch_entity_id = entity_id(hass, SWITCH_DOMAIN, SWITCH_UNIQUE_ID)

    with patch(
        "custom_components.lksystems.switch.LKSystemsManager",
        return_value=fake_manager,
    ):
        await leak_detection_entity(hass).async_turn_off(seconds=300)
        await hass.async_block_till_done()

    state = hass.states.get(switch_entity_id)
    assert state is not None
    assert ("cubic_secure_pause_leak_detection", CUBIC_IDENTITY, 300) in (
        fake_manager.calls
    )
    assert state.state == "off"
    assert state.attributes["state_source"] == "command"
    assert state.attributes["pause_seconds"] == 300
    assert state.attributes["paused_until"] is not None


async def test_turn_off_defaults_to_duration_helper_value(hass, fake_manager):
    """Without a number entity, the legacy pause duration helper supplies it."""
    await setup_entry(hass, fake_manager)
    switch_entity_id = entity_id(hass, SWITCH_DOMAIN, SWITCH_UNIQUE_ID)

    hass.states.async_set("input_number.cubic_secure_pause_duration", "15")

    # Hide the integration's own pause duration entity so the legacy
    # helper fallback (for installs predating the entity) is exercised.
    coordinator = next(iter(hass.data[DOMAIN].values()))
    saved_duration_entities = coordinator.pause_duration_entities
    coordinator.pause_duration_entities = {}

    try:
        with patch(
            "custom_components.lksystems.switch.LKSystemsManager",
            return_value=fake_manager,
        ):
            await hass.services.async_call(
                SWITCH_DOMAIN,
                "turn_off",
                {"entity_id": switch_entity_id},
                blocking=True,
            )
            await hass.async_block_till_done()
    finally:
        coordinator.pause_duration_entities = saved_duration_entities

    assert ("cubic_secure_pause_leak_detection", CUBIC_IDENTITY, 900) in (
        fake_manager.calls
    )


async def test_integration_service_routes_through_entity(hass, fake_manager):
    """lksystems.pause_leak_detection pauses via the switch entity."""
    await setup_entry(hass, fake_manager)
    switch_entity_id = entity_id(hass, SWITCH_DOMAIN, SWITCH_UNIQUE_ID)

    device_entry = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, CUBIC_IDENTITY)}
    )
    assert device_entry is not None

    with patch(
        "custom_components.lksystems.switch.LKSystemsManager",
        return_value=fake_manager,
    ):
        await hass.services.async_call(
            DOMAIN,
            "pause_leak_detection",
            {"device_id": device_entry.id, "seconds": 600},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert ("cubic_secure_pause_leak_detection", CUBIC_IDENTITY, 600) in (
        fake_manager.calls
    )
    state = hass.states.get(switch_entity_id)
    assert state is not None
    assert state.state == "off"
    assert state.attributes["pause_seconds"] == 600


async def test_turn_off_is_noop_when_already_paused_locally(hass, fake_manager):
    """A second turn_off during a local pause must not send another API call."""
    await setup_entry(hass, fake_manager)

    with patch(
        "custom_components.lksystems.switch.LKSystemsManager",
        return_value=fake_manager,
    ):
        for _ in range(2):
            await leak_detection_entity(hass).async_turn_off(seconds=300)
            await hass.async_block_till_done()

    pause_calls = [
        call
        for call in fake_manager.calls
        if call[0] == "cubic_secure_pause_leak_detection"
    ]
    assert len(pause_calls) == 1


async def test_turn_on_is_noop_when_active(hass, fake_manager):
    """A turn_on while already active must not send an API call."""
    await setup_entry(hass, fake_manager)
    switch_entity_id = entity_id(hass, SWITCH_DOMAIN, SWITCH_UNIQUE_ID)

    with patch(
        "custom_components.lksystems.switch.LKSystemsManager",
        return_value=fake_manager,
    ), patch.object(
        switch_module, "RESUME_CONFIRM_DELAY", 0
    ):
        await hass.services.async_call(
            SWITCH_DOMAIN,
            "turn_on",
            {"entity_id": switch_entity_id},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert not [
        call
        for call in fake_manager.calls
        if call[0] == "cubic_secure_pause_leak_detection"
    ]


async def test_turn_on_sends_resume_nudge(hass, fake_manager):
    """A turn_on during a local pause nudges LK for one second and flips on."""
    await setup_entry(hass, fake_manager)
    switch_entity_id = entity_id(hass, SWITCH_DOMAIN, SWITCH_UNIQUE_ID)

    with (
        patch(
            "custom_components.lksystems.switch.LKSystemsManager",
            return_value=fake_manager,
        ),
        patch.object(switch_module, "RESUME_CONFIRM_DELAY", 0),
        patch(
            "custom_components.lksystems.LKSystemsManager",
            return_value=fake_manager,
        ),
    ):
        await leak_detection_entity(hass).async_turn_off(seconds=300)
        await hass.services.async_call(
            SWITCH_DOMAIN,
            "turn_on",
            {"entity_id": switch_entity_id},
            blocking=True,
        )
        await hass.async_block_till_done()

    pause_calls = [
        call
        for call in fake_manager.calls
        if call[0] == "cubic_secure_pause_leak_detection"
    ]
    assert pause_calls[-1] == (
        "cubic_secure_pause_leak_detection",
        CUBIC_IDENTITY,
        switch_module.RESUME_NUDGE_SECONDS,
    )

    state = hass.states.get(switch_entity_id)
    assert state is not None
    assert state.state == "on"
    assert state.attributes["pause_seconds"] is None


async def test_failed_pause_does_not_change_effective_state(hass, fake_manager):
    """A rejected LK pause must leave the effective state unchanged."""
    await setup_entry(hass, fake_manager)
    switch_entity_id = entity_id(hass, SWITCH_DOMAIN, SWITCH_UNIQUE_ID)
    fake_manager.cubic_secure_pause_leak_detection_result = False

    with patch(
        "custom_components.lksystems.switch.LKSystemsManager",
        return_value=fake_manager,
    ):
        with pytest.raises(HomeAssistantError):
            await leak_detection_entity(hass).async_turn_off(seconds=300)

    state = hass.states.get(switch_entity_id)
    assert state is not None
    assert state.state == "on"
    assert state.attributes["state_source"] == "cloud"


async def test_stale_cloud_state_does_not_undo_recent_pause(hass, fake_manager):
    """A NoLeak cloud value inside the grace period must not snap back on."""
    await setup_entry(hass, fake_manager)
    switch_entity_id = entity_id(hass, SWITCH_DOMAIN, SWITCH_UNIQUE_ID)

    with patch(
        "custom_components.lksystems.switch.LKSystemsManager",
        return_value=fake_manager,
    ):
        await leak_detection_entity(hass).async_turn_off(seconds=300)

    # LK's cloud still reports NoLeak. A coordinator refresh inside the
    # grace period must not snap the switch back to on.
    coordinator = next(iter(hass.data[DOMAIN].values()))
    with patch(
        "custom_components.lksystems.LKSystemsManager",
        return_value=fake_manager,
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get(switch_entity_id)
    assert state is not None
    assert state.state == "off"
    assert state.attributes["state_source"] == "command"


async def test_local_pause_expiry_flips_state_on(hass, fake_manager):
    """When a locally issued pause expires, the effective state flips on."""
    await setup_entry(hass, fake_manager)
    switch_entity_id = entity_id(hass, SWITCH_DOMAIN, SWITCH_UNIQUE_ID)

    with patch(
        "custom_components.lksystems.switch.LKSystemsManager",
        return_value=fake_manager,
    ):
        await leak_detection_entity(hass).async_turn_off(seconds=300)

    assert hass.states.get(switch_entity_id).state == "off"

    # Move the clock past the pause's end; the entity must flip on by itself,
    # and the lingering forceOpen cloud flag must not flip it back within
    # the grace window.
    future = dt_util.now() + timedelta(seconds=3600)
    fake_manager.cubic_measurement_data = build_forceopen_measurement()
    coordinator = next(iter(hass.data[DOMAIN].values()))
    with (
        patch.object(switch_module.dt_util, "now", return_value=future),
        patch(
            "custom_components.lksystems.LKSystemsManager",
            return_value=fake_manager,
        ),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get(switch_entity_id)
    assert state is not None
    assert state.state == "on"
    assert state.attributes["pause_seconds"] == 300
