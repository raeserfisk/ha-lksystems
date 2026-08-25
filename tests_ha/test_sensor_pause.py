"""Tests for the LK Systems Cubic Secure pause remaining sensor."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.lksystems import sensor as sensor_module
from custom_components.lksystems import switch as switch_module
from custom_components.lksystems.const import DOMAIN

from .conftest import (
    CUBIC_IDENTITY,
    build_cubic_measurement,
    entity_id,
    setup_entry,
)

SENSOR_UNIQUE_ID = f"LkUid_pause_remaining_{CUBIC_IDENTITY}"
SWITCH_UNIQUE_ID = f"LkUid_leak_detection_{CUBIC_IDENTITY}"
VALVE_STATE_SENSOR_UNIQUE_ID = f"LkUid_valveState_{CUBIC_IDENTITY}"


def build_forceopen_measurement() -> dict:
    """A Cubic Secure measurement whose leak state is a paused (forceOpen) one."""
    data = build_cubic_measurement()
    data["leak"]["leakState"] = "forceOpen"
    return data


def pause_remaining_entity_id(hass) -> str:
    """Return the entity_id of the pause remaining sensor."""
    return entity_id(hass, "sensor", SENSOR_UNIQUE_ID)


def leak_detection_entity(hass):
    """Return the live leak detection switch entity for the test device."""
    coordinator = next(iter(hass.data[DOMAIN].values()))
    return coordinator.leak_detection_entities[CUBIC_IDENTITY]


async def pause_leak_detection(hass, fake_manager, seconds: int) -> None:
    """Pause leak detection through the switch entity."""
    with patch(
        "custom_components.lksystems.switch.LKSystemsManager",
        return_value=fake_manager,
    ):
        await leak_detection_entity(hass).async_turn_off(seconds=seconds)
        await hass.async_block_till_done()


async def resume_leak_detection(hass, fake_manager) -> None:
    """Resume leak detection through the switch entity."""
    with (
        patch(
            "custom_components.lksystems.switch.LKSystemsManager",
            return_value=fake_manager,
        ),
        patch.object(switch_module, "RESUME_CONFIRM_DELAY", 0),
        patch("custom_components.lksystems.LKSystemsManager", return_value=fake_manager),
    ):
        await leak_detection_entity(hass).async_turn_on()
        await hass.async_block_till_done()


async def test_cubic_secure_exposes_pause_remaining_sensor(hass, fake_manager):
    """A Cubic Secure device exposes a pause remaining sensor entity."""
    await setup_entry(hass, fake_manager)

    sensor_entity_id = pause_remaining_entity_id(hass)
    state = hass.states.get(sensor_entity_id)

    assert state is not None
    assert state.state == "0"
    assert state.attributes["unit_of_measurement"] == "s"
    assert state.attributes["state_class"] == "measurement"
    assert state.attributes["device_class"] == "duration"
    assert state.attributes["paused_until"] is None
    assert state.attributes["pause_seconds"] is None

    # The sensor and the existing valve-state sensor must attach to the
    # same Cubic Secure device rather than creating a second HA device.
    entity_registry = er.async_get(hass)
    sensor_entry = entity_registry.async_get(sensor_entity_id)
    state_sensor_id = entity_id(hass, "sensor", VALVE_STATE_SENSOR_UNIQUE_ID)
    state_sensor_entry = entity_registry.async_get(state_sensor_id)

    assert sensor_entry is not None
    assert state_sensor_entry is not None
    assert sensor_entry.device_id == state_sensor_entry.device_id


async def test_pause_remaining_counts_down_during_local_pause(hass, fake_manager):
    """While a locally issued pause runs, the sensor counts seconds down."""
    await setup_entry(hass, fake_manager)
    sensor_entity_id = pause_remaining_entity_id(hass)

    await pause_leak_detection(hass, fake_manager, seconds=300)

    state = hass.states.get(sensor_entity_id)
    assert state is not None
    assert 298 <= int(state.state) <= 300
    assert state.attributes["pause_seconds"] == 300
    assert state.attributes["paused_until"] is not None

    # Advance the wall clock by a minute: the per-second tick must have
    # recomputed the remaining time, and the tick must keep running.
    future_now = dt_util.now() + timedelta(seconds=60)
    with patch.object(sensor_module.dt_util, "now", return_value=future_now):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
        await hass.async_block_till_done()

    state = hass.states.get(sensor_entity_id)
    assert state is not None
    assert 238 <= int(state.state) <= 240


async def test_pause_remaining_resets_on_resume(hass, fake_manager):
    """Resuming leak detection drops the countdown back to zero."""
    await setup_entry(hass, fake_manager)
    sensor_entity_id = pause_remaining_entity_id(hass)

    await pause_leak_detection(hass, fake_manager, seconds=300)
    assert int(hass.states.get(sensor_entity_id).state) > 0

    await resume_leak_detection(hass, fake_manager)

    state = hass.states.get(sensor_entity_id)
    assert state is not None
    assert state.state == "0"
    assert state.attributes["paused_until"] is None
    assert state.attributes["pause_seconds"] is None


async def test_pause_remaining_is_unknown_for_cloud_pause(hass, fake_manager):
    """A pause started elsewhere (LK app) has no known duration."""
    await setup_entry(hass, fake_manager)
    sensor_entity_id = pause_remaining_entity_id(hass)

    fake_manager.cubic_measurement_data = build_forceopen_measurement()
    coordinator = next(iter(hass.data[DOMAIN].values()))
    with patch(
        "custom_components.lksystems.LKSystemsManager",
        return_value=fake_manager,
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get(sensor_entity_id)
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes["cloud_paused"] is True
    assert state.attributes["paused_until"] is None


async def test_pause_remaining_reaches_zero_at_expiry(hass, fake_manager):
    """Once the local pause expires, the countdown reads zero and stops."""
    await setup_entry(hass, fake_manager)
    sensor_entity_id = pause_remaining_entity_id(hass)

    await pause_leak_detection(hass, fake_manager, seconds=300)

    # Move the clock past the pause's end and let the tick run: the sensor
    # must settle at zero. The jump also crosses the coordinator's next
    # scheduled refresh, so the fake manager must stay in place for it.
    future_now = dt_util.now() + timedelta(seconds=3600)
    with (
        patch.object(sensor_module.dt_util, "now", return_value=future_now),
        patch.object(switch_module.dt_util, "now", return_value=future_now),
        patch("custom_components.lksystems.LKSystemsManager", return_value=fake_manager),
    ):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=310))
        await hass.async_block_till_done()

    state = hass.states.get(sensor_entity_id)
    assert state is not None
    assert state.state == "0"
