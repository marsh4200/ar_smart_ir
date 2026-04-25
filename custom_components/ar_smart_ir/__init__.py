from __future__ import annotations

import asyncio
from base64 import b64encode
import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry

from .const import CONF_COMMAND_OVERRIDES, DOMAIN, PLATFORMS
from .helpers import (
    parse_command_overrides,
    set_command_override_at_path,
)

_LOGGER = logging.getLogger(__name__)

# ── service schema ────────────────────────────────────────────────────────────

LEARN_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Required("command_path"): cv.string,
        vol.Required("broadlink_entity"): cv.entity_id,
        vol.Optional("timeout", default=30): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=120)
        ),
    }
)

# ── helpers ───────────────────────────────────────────────────────────────────

def _get_broadlink_wrapper(hass: HomeAssistant, entity_id: str):
    """
    Return HA's BroadlinkDevice wrapper for the given remote entity.

    Structure in hass.data:
      hass.data["broadlink"]        -> BroadlinkData (dataclass)
        .devices                    -> dict[config_entry_id, BroadlinkDevice]

    BroadlinkDevice has:
      .async_request(api_method)    -> coroutine that handles auth/retry
      .api                          -> raw python-broadlink device object
        .enter_learning             -> method to pass to async_request
        .check_data                 -> method to pass to async_request
    """
    broadlink_data = hass.data.get("broadlink")
    if broadlink_data is None:
        _LOGGER.debug("AR Smart IR: 'broadlink' not in hass.data")
        return None

    # BroadlinkData is a dataclass — use attribute access, not .get()
    devices: dict = getattr(broadlink_data, "devices", {})

    entity_reg = er.async_get(hass)
    entity_entry = entity_reg.async_get(entity_id)
    if entity_entry is None:
        _LOGGER.debug("AR Smart IR: entity '%s' not found in registry", entity_id)
        return None

    wrapper = devices.get(entity_entry.config_entry_id)
    if wrapper is None:
        _LOGGER.debug(
            "AR Smart IR: no Broadlink device for config entry '%s'",
            entity_entry.config_entry_id,
        )
    return wrapper


async def _async_broadlink_learn(
    hass: HomeAssistant,
    remote_entity: str,
    timeout: int,
) -> str:
    """
    Put a Broadlink remote into IR learn mode and return the captured
    code as a Base64 string.

    Uses HA's BroadlinkDevice wrapper (which owns async_request) together
    with the raw python-broadlink api object (which owns enter_learning /
    check_data).  The two must be kept separate.
    """
    wrapper = _get_broadlink_wrapper(hass, remote_entity)
    if wrapper is None:
        raise HomeAssistantError(
            f"AR Smart IR: Could not find a Broadlink device for entity "
            f"'{remote_entity}'. Make sure it is a Broadlink remote entity "
            "and the Broadlink integration is loaded."
        )

    api = getattr(wrapper, "api", None)
    if api is None:
        raise HomeAssistantError(
            f"AR Smart IR: BroadlinkDevice for '{remote_entity}' has no .api — "
            "the Broadlink integration may not have finished setting up."
        )

    _LOGGER.debug(
        "AR Smart IR: Entering IR learn mode on %s (timeout %ss)",
        remote_entity,
        timeout,
    )

    # wrapper.async_request(api_method) is the correct call pattern.
    try:
        await wrapper.async_request(api.enter_learning)
    except Exception as err:
        raise HomeAssistantError(
            f"AR Smart IR: Failed to enter learn mode on '{remote_entity}': {err}"
        ) from err

    # Poll for a captured code every 0.6 s.
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.6)
        try:
            data = await wrapper.async_request(api.check_data)
        except Exception:
            # check_data raises when no code has arrived yet — keep polling.
            continue

        if data:
            code = b64encode(data).decode("utf-8")
            _LOGGER.debug("AR Smart IR: Captured IR code from %s", remote_entity)
            return code

    raise HomeAssistantError(
        f"AR Smart IR: Timed out after {timeout}s — no IR signal received on "
        f"'{remote_entity}'. Point your physical remote directly at the Broadlink "
        "device and press the button firmly, then try again."
    )


async def _async_handle_learn_command(call: ServiceCall) -> None:
    """Service handler for ar_smart_ir.learn_command."""
    hass: HomeAssistant = call.hass
    entry_id: str = call.data["entry_id"]
    command_path_str: str = call.data["command_path"]
    remote_entity: str = call.data["broadlink_entity"]
    timeout: int = call.data["timeout"]

    # ── resolve config entry ──────────────────────────────────────────────────
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise HomeAssistantError(
            f"AR Smart IR: No config entry found with ID '{entry_id}'."
        )
    if entry.domain != DOMAIN:
        raise HomeAssistantError(
            f"AR Smart IR: Entry '{entry_id}' does not belong to {DOMAIN}."
        )

    # ── parse command path ────────────────────────────────────────────────────
    command_path = tuple(part.strip() for part in command_path_str.split("/"))
    if not all(command_path):
        raise HomeAssistantError(
            f"AR Smart IR: Invalid command path '{command_path_str}'. "
            "Use the slash-separated format shown in the options dropdown, "
            "e.g. 'cool / auto / 24' or 'power'."
        )

    # ── learn the IR code ─────────────────────────────────────────────────────
    _LOGGER.info(
        "AR Smart IR: Starting IR learn for entry '%s', path '%s' via %s",
        entry_id,
        command_path_str,
        remote_entity,
    )
    learned_code = await _async_broadlink_learn(hass, remote_entity, timeout)

    # ── merge into command overrides and persist ──────────────────────────────
    current_options = dict(entry.options)
    override_data = parse_command_overrides(
        current_options.get(CONF_COMMAND_OVERRIDES, {})
    )

    override_data = set_command_override_at_path(
        override_data,
        command_path,
        repeat_count=1,
        repeat_delay_secs=0.0,
    )

    # Patch the learned Base64 code into the leaf dict.
    leaf_parent = override_data
    for part in command_path[:-1]:
        leaf_parent = leaf_parent[part]
    leaf_key = command_path[-1]
    if isinstance(leaf_parent.get(leaf_key), dict):
        leaf_parent[leaf_key]["code"] = learned_code
    else:
        leaf_parent[leaf_key] = {
            "code": learned_code,
            "repeat_count": 1,
            "repeat_delay_secs": 0.0,
        }

    new_options = {**current_options, CONF_COMMAND_OVERRIDES: override_data}
    hass.config_entries.async_update_entry(entry, options=new_options)

    _LOGGER.info(
        "AR Smart IR: Learned IR code saved for entry '%s' at path '%s'.",
        entry_id,
        command_path_str,
    )

    await hass.config_entries.async_reload(entry_id)


# ── integration lifecycle ─────────────────────────────────────────────────────

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up AR Smart IR component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AR Smart IR from a config entry."""

    platform: str | None = entry.data.get("platform")

    if platform not in PLATFORMS:
        _LOGGER.error("Unsupported AR Smart IR platform: %s", platform)
        return False

    if not hass.services.has_service(DOMAIN, "learn_command"):
        hass.services.async_register(
            DOMAIN,
            "learn_command",
            _async_handle_learn_command,
            schema=LEARN_COMMAND_SCHEMA,
        )
        _LOGGER.debug("AR Smart IR: Registered learn_command service")

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, [platform])

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload AR Smart IR config entry."""

    platform: str | None = entry.data.get("platform")

    if platform not in PLATFORMS:
        return True

    return await hass.config_entries.async_unload_platforms(entry, [platform])


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
