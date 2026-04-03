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
    CONF_GO_BACK,
    CONF_HUMIDITY_SENSOR,
    CONF_OVERRIDE_COMMAND,
    CONF_OVERRIDE_REMOVE,
    CONF_OVERRIDE_REPEAT_COUNT,
    CONF_OVERRIDE_REPEAT_DELAY,
    CONF_PLATFORM,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEST_COMMAND,
    CONF_TEST_DEVICE,
    DEFAULT_DELAY,
    DOMAIN,
    PLATFORM_TITLES,
    PLATFORMS,
)
from .helpers import (
    async_load_device_data,
    command_path_to_key,
    flatten_command_paths,
    get_command_value_at_path,
    infer_title,
    get_manufacturers,
    get_models_for_manufacturer,
    parse_command_overrides,
    remove_command_override_at_path,
    set_command_override_at_path,
)
from .controller import get_controller

CONF_CONTROLLER = "controller"

CONTROLLERS = [
    "Broadlink",
    "Xiaomi",
    "MQTT",
    "LOOKin",
    "ESPHome",
    "Tuya",
]

TEST_COMMAND_PRIORITIES = (
    ("off", "Power off"),
    ("power_off", "Power off"),
    ("power", "Power toggle"),
    ("toggle", "Power toggle"),
    ("on", "Power on"),
    ("power_on", "Power on"),
)


def _temperature_sensor_selector():
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=[
                {
                    "domain": "sensor",
                    "device_class": "temperature",
                }
            ],
            multiple=False,
        )
    )


def _humidity_sensor_selector():
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=[
                {
                    "domain": "sensor",
                    "device_class": "humidity",
                }
            ],
            multiple=False,
        )
    )


def _optional_entity_field(config_key: str, data: dict[str, Any]):
    if data.get(config_key):
        return vol.Optional(config_key, default=data.get(config_key))
    return vol.Optional(config_key)


class ARSmartIRConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._test_status: str = ""
        self._pending_name_input: dict[str, Any] = {}

    async def _get_test_command_options(self) -> list[selector.SelectOptionDict]:
        device_data = await async_load_device_data(
            self._data[CONF_DEVICE_CODE],
            self._data[CONF_PLATFORM],
        )
        command_paths = flatten_command_paths(device_data.get("commands", {}))

        return [
            selector.SelectOptionDict(
                value=command_path_to_key(path),
                label=self._label_for_test_path(path),
            )
            for path in command_paths
        ]

    async def _get_default_test_command(self) -> str:
        options = await self._get_test_command_options()
        if not options:
            return ""

        by_value = {option["value"]: option for option in options}
        normalized = {
            value.casefold().replace(" ", "").replace("_", ""): value
            for value in by_value
        }

        for preferred, _label in TEST_COMMAND_PRIORITIES:
            match = normalized.get(preferred.casefold().replace("_", ""))
            if match:
                return match

        return options[0]["value"]

    def _label_for_test_path(self, path: tuple[str, ...]) -> str:
        key = command_path_to_key(path)
        leaf = path[-1].casefold().replace("_", "")

        for preferred, label in TEST_COMMAND_PRIORITIES:
            if leaf == preferred.casefold().replace("_", ""):
                return f"{label} ({key})"

        return key

    async def _async_test_selected_command(self, data: dict[str, Any]) -> str:
        device_data = await async_load_device_data(
            data[CONF_DEVICE_CODE],
            data[CONF_PLATFORM],
        )
        commands = device_data.get("commands", {})

        selected_key = data.get(CONF_TEST_COMMAND) or await self._get_default_test_command()
        if not selected_key:
            raise ValueError("No testable commands were found for this code.")

        command_path = tuple(selected_key.split(" / "))
        command = get_command_value_at_path(commands, command_path)
        if command is None:
            raise ValueError("The selected test command could not be found in this code.")

        controller = get_controller(
            self.hass,
            data[CONF_CONTROLLER],
            device_data["commandsEncoding"],
            data[CONF_CONTROLLER_DATA],
            float(data.get(CONF_DELAY, DEFAULT_DELAY)),
        )
        await controller.send(command)

        return selected_key

    async def _async_show_name_form(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ):
        platform = self._data[CONF_PLATFORM]
        code = self._data[CONF_DEVICE_CODE]
        controller = self._data[CONF_CONTROLLER]

        default_name = infer_title(
            {
                "platform": platform,
                "device_code": code,
            }
        )
        test_options = await self._get_test_command_options()
        default_test_command = await self._get_default_test_command()

        current_values = {**self._pending_name_input}
        if user_input is not None:
            current_values.update(user_input)

        if controller in ["Broadlink", "Xiaomi", "ESPHome", "Tuya"]:
            controller_field = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="remote")
            )
        else:
            controller_field = str

        data_schema: dict[Any, Any] = {
            vol.Required(
                CONF_NAME,
                default=current_values.get(CONF_NAME, default_name),
            ): str,
        }

        if CONF_CONTROLLER_DATA in current_values:
            data_schema[
                vol.Required(
                    CONF_CONTROLLER_DATA,
                    default=current_values[CONF_CONTROLLER_DATA],
                )
            ] = controller_field
        else:
            data_schema[vol.Required(CONF_CONTROLLER_DATA)] = controller_field

        if platform == "climate":
            data_schema[
                _optional_entity_field(CONF_TEMPERATURE_SENSOR, current_values)
            ] = _temperature_sensor_selector()
            data_schema[
                _optional_entity_field(CONF_HUMIDITY_SENSOR, current_values)
            ] = _humidity_sensor_selector()

        data_schema[
            vol.Optional(
                CONF_DELAY,
                default=current_values.get(CONF_DELAY, DEFAULT_DELAY),
            )
        ] = vol.Coerce(float)

        if test_options:
            data_schema[
                vol.Optional(
                    CONF_TEST_COMMAND,
                    default=current_values.get(CONF_TEST_COMMAND, default_test_command),
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=test_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
            data_schema[
                vol.Optional(
                    CONF_TEST_DEVICE,
                    default=False,
                )
            ] = bool

        data_schema[vol.Optional(CONF_GO_BACK, default=False)] = bool

        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema(data_schema),
            errors=errors or {},
            description_placeholders={
                "status": self._test_status or "No test sent yet.",
            },
        )

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
            if user_input.get(CONF_GO_BACK):
                return await self.async_step_manufacturer()
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
                    ),
                    vol.Optional(CONF_GO_BACK, default=False): bool,
                }
            ),
        )

    async def async_step_controller(self, user_input=None):
        if user_input is not None:
            if user_input.get(CONF_GO_BACK):
                return await self.async_step_model()
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
                    ),
                    vol.Optional(CONF_GO_BACK, default=False): bool,
                }
            ),
        )

    async def async_step_name(self, user_input=None):
        controller = self._data[CONF_CONTROLLER]

        if user_input is not None:
            data = {**self._data, **user_input}
            self._pending_name_input = {
                key: value
                for key, value in user_input.items()
                if key != CONF_TEST_DEVICE
            }

            data[CONF_DEVICE_CODE] = int(data[CONF_DEVICE_CODE])
            data[CONF_DELAY] = float(data.get(CONF_DELAY, DEFAULT_DELAY))

            data["controller"] = controller

            if user_input.get(CONF_TEST_DEVICE):
                try:
                    tested_command = await self._async_test_selected_command(data)
                except Exception as err:  # noqa: BLE001
                    self._test_status = (
                        "Test failed: "
                        f"{err}"
                    )
                    return await self._async_show_name_form(
                        user_input,
                        errors={"base": "test_failed"},
                    )
                else:
                    self._test_status = (
                        "Test command sent: "
                        f"{tested_command}. Confirm the device reacted, then save."
                    )
                    return await self._async_show_name_form(user_input)

            if user_input.get(CONF_GO_BACK):
                return await self.async_step_controller()

            data["unique_id"] = uuid.uuid4().hex
            data.pop(CONF_GO_BACK, None)
            data.pop(CONF_TEST_DEVICE, None)
            data.pop(CONF_TEST_COMMAND, None)

            await self.async_set_unique_id(data["unique_id"])
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=data[CONF_NAME],
                data=data,
            )

        return await self._async_show_name_form()

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
            else data.get(CONF_OVERRIDE_COMMAND)
            or (command_options[0]["value"] if command_options else "")
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
                    override_data = remove_command_override_at_path(
                        override_data,
                        selected_path,
                    )
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
                self._build_options_schema(
                    data,
                    command_options,
                    selected_key,
                    current_repeat,
                    current_delay,
                    current_remove,
                )
            ),
            errors=errors,
        )

    def _build_options_schema(
        self,
        data: dict[str, Any],
        command_options: list[Any],
        selected_key: str,
        current_repeat: int,
        current_delay: float,
        current_remove: bool,
    ) -> dict[Any, Any]:
        schema: dict[Any, Any] = {
            vol.Optional(
                CONF_NAME,
                default=data.get(CONF_NAME, self._config_entry.title),
            ): str,
            vol.Optional(
                CONF_CONTROLLER_DATA,
                default=data.get(CONF_CONTROLLER_DATA, ""),
            ): str,
        }

        if data.get(CONF_PLATFORM) == "climate":
            schema[
                _optional_entity_field(CONF_TEMPERATURE_SENSOR, data)
            ] = _temperature_sensor_selector()
            schema[
                _optional_entity_field(CONF_HUMIDITY_SENSOR, data)
            ] = _humidity_sensor_selector()

        schema[
            vol.Optional(
                CONF_DELAY,
                default=data.get(CONF_DELAY, DEFAULT_DELAY),
            )
        ] = vol.Coerce(float)

        schema.update(
            {
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
        )

        return schema
