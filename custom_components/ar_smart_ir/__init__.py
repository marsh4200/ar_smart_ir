from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up AR Smart IR component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AR Smart IR from a config entry."""

    platform: str | None = entry.data.get("platform")

    if platform not in PLATFORMS:
        _LOGGER.error("Unsupported AR Smart IR platform: %s", platform)
        return False

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
