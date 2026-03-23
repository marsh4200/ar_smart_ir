from __future__ import annotations
from typing import Any
import uuid

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector

from .const import (
    CONF_COMMAND_OVERRIDES,
    CONF_CONTROLLER_DATA,
    CONF_DELAY,
    CONF_DEVICE_CODE,
    CONF_OVERRIDE_COMMAND,
    CONF_OVERRIDE_REMOVE,
    CONF_OVERRIDE_REPEAT_COUNT,
    CONF_OVERRIDE_REPEAT_DELAY,
    CONF_PLATFORM,
    DEFAULT_DELAY,
    DOMAIN,
    PLATFORM_TITLES,
    PLATFORMS,
)

from .helpers import (
    command_path_to_key,
    flatten_command_paths,
    get_command_value_at_path,
    get_manufacturers,
    get_models_for_manufacturer,
    infer_title,
    parse_command_overrides,
    remove_command_override_at_path,
    set_command_override_at_path,
    async_load_device_data,
)

CONF_CONTROLLER = "controller"

CONTROLLERS = [
    "Broadlink",
    "Xiaomi",
    "MQTT",
    "LOOKin",
    "ESPHome",
    "Tuya",
]


class ARSmartIRConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input=None):

        if user_input is not None:
            self._data[CONF_PLATFORM] = user_input[CONF_PLATFORM]
            return await self.async_step_manufacturer()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PLATFORM): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=p,
                                    label=PLATFORM_TITLES[p],
                                )
                                for p in PLATFORMS
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_manufacturer(self, user_input=None):

        platform = self._data[CONF_PLATFORM]
        manufacturers = get_manufacturers(platform)

        if user_input is not None:
            self._data["manufacturer"] = user_input["manufacturer"]
            return await self.async_step_model()

        return self.async_show_form(
            step_id="manufacturer",
            data_schema=vol.Schema(
                {
                    vol.Required("manufacturer"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=manufacturers,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_model(self, user_input=None):

        platform = self._data[CONF_PLATFORM]
        manufacturer = self._data["manufacturer"]

        models = get_models_for_manufacturer(platform, manufacturer)

        if user_input is not None:
            self._data[CONF_DEVICE_CODE] = int(user_input[CONF_DEVICE_CODE])
            return await self.async_step_controller()

        options = [
            selector.SelectOptionDict(
                value=item["code"],
                label=item["label"],
            )
            for item in models
        ]

        return self.async_show_form(
            step_id="model",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_CODE): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_controller(self, user_input=None):

        if user_input is not None:
            self._data[CONF_CONTROLLER] = user_input[CONF_CONTROLLER]
            return await self.async_step_name()

        return self.async_show_form(
            step_id="controller",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CONTROLLER): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=CONTROLLERS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_name(self, user_input=None):

        platform = self._data[CONF_PLATFORM]
        code = self._data[CONF_DEVICE_CODE]
        controller = self._data[CONF_CONTROLLER]

        default_name = infer_title(
            {
                "platform": platform,
                "device_code": code,
            }
        )

        if user_input is not None:

            data = {**self._data, **user_input}

            data[CONF_DEVICE_CODE] = int(data[CONF_DEVICE_CODE])
            data[CONF_DELAY] = DEFAULT_DELAY

            data["controller"] = controller
            data["unique_id"] = uuid.uuid4().hex

            await self.async_set_unique_id(data["unique_id"])
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=data[CONF_NAME],
                data=data,
            )

        if controller in ["Broadlink", "Xiaomi", "ESPHome", "Tuya"]:
            controller_field = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="remote")
            )
        else:
            controller_field = str

        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=default_name): str,
                    vol.Required(CONF_CONTROLLER_DATA): controller_field,
                }
            ),
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return ARSmartIROptionsFlow(config_entry)


class ARSmartIROptionsFlow(config_entries.OptionsFlow):

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors = {}

        data = {**self._config_entry.data, **self._config_entry.options}
        override_data = parse_command_overrides(data.get(CONF_COMMAND_OVERRIDES, {}))
        device_data = await async_load_device_data(
            data.get(CONF_DEVICE_CODE),
            data.get(CONF_PLATFORM),
        )
        command_paths = flatten_command_paths(device_data.get("commands", {}))
        command_options = [
            selector.SelectOptionDict(
                value=command_path_to_key(path),
                label=(
                    f"{command_path_to_key(path)} [saved]"
                    if isinstance(get_command_value_at_path(override_data, path), dict)
                    else command_path_to_key(path)
                ),
            )
            for path in command_paths
        ]
        selected_key = (
            user_input.get(CONF_OVERRIDE_COMMAND)
            if user_input is not None
            else data.get(CONF_OVERRIDE_COMMAND) or (command_options[0]["value"] if command_options else "")
        )
        selected_path = tuple(selected_key.split(" / ")) if selected_key else ()
        current_override = (
            get_command_value_at_path(override_data, selected_path)
            if selected_path
            else None
        )
        current_repeat = 1
        current_delay = 0.0
        current_remove = False
        if isinstance(current_override, dict):
            current_repeat = int(current_override.get("repeat_count", 1) or 1)
            current_delay = float(current_override.get("repeat_delay_secs", 0.0) or 0.0)

        if user_input is not None:
            if selected_path:
                remove_override = bool(user_input.get(CONF_OVERRIDE_REMOVE, False))
                repeat_count = int(user_input.get(CONF_OVERRIDE_REPEAT_COUNT, 1) or 1)
                repeat_delay = float(user_input.get(CONF_OVERRIDE_REPEAT_DELAY, 0.0) or 0.0)
                if remove_override or (repeat_count <= 1 and repeat_delay <= 0):
                    override_data = remove_command_override_at_path(override_data, selected_path)
                else:
                    override_data = set_command_override_at_path(
                        override_data,
                        selected_path,
                        repeat_count,
                        repeat_delay,
                    )

            cleaned_input = dict(user_input)
            cleaned_input[CONF_COMMAND_OVERRIDES] = override_data
            cleaned_input[CONF_OVERRIDE_COMMAND] = selected_key
            return self.async_create_entry(title="", data=cleaned_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_NAME,
                        default=data.get(CONF_NAME, self._config_entry.title),
                    ): str,
                    vol.Optional(
                        CONF_CONTROLLER_DATA,
                        default=data.get(CONF_CONTROLLER_DATA, ""),
                    ): str,
                    vol.Optional(
                        CONF_DELAY,
                        default=data.get(CONF_DELAY, DEFAULT_DELAY),
                    ): vol.Coerce(float),
                    vol.Optional(
                        CONF_OVERRIDE_COMMAND,
                        default=selected_key,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=command_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_OVERRIDE_REPEAT_COUNT,
                        default=current_repeat,
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
                    vol.Optional(
                        CONF_OVERRIDE_REPEAT_DELAY,
                        default=current_delay,
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=30)),
                    vol.Optional(
                        CONF_OVERRIDE_REMOVE,
                        default=current_remove,
                    ): bool,
                }
            ),
            errors=errors,
        )
