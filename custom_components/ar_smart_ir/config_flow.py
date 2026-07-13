from __future__ import annotations

import json
from typing import Any
import uuid

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector

from .const import (
    CONF_COMMAND_OVERRIDES,
    CONF_CONTROLLER,
    CONF_CONTROLLER_DATA,
    CONF_CONTROLLER_ENTITY,
    CONF_DELAY,
    CONF_DEVICE_CLASS,
    CONF_DEVICE_CODE,
    CONF_GO_BACK,
    CONF_HUMIDITY_SENSOR,
    CONF_INFRARED_ENTITY,
    CONF_OVERRIDE_COMMAND,
    CONF_OVERRIDE_REMOVE,
    CONF_OVERRIDE_REPEAT_COUNT,
    CONF_OVERRIDE_REPEAT_DELAY,
    CONF_PLATFORM,
    CONF_PASSIVE_MODE,
    CONF_POWER_SENSOR,
    CONF_POWER_SENSOR_RESTORE_STATE,
    CONF_SOURCE_NAMES,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEST_COMMAND,
    CONF_TEST_DEVICE,
    DEFAULT_DEVICE_CLASS,
    DEFAULT_DELAY,
    DOMAIN,
    PLATFORM_TITLES,
    PLATFORMS,
    CONF_LEARN_COMMAND,
    CONF_LEARN_BROADLINK_ENTITY,
    CONF_LEARN_TIMEOUT,
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

CONTROLLERS = [
    "Broadlink",
    "LinkNLink",
    "Xiaomi",
    "MQTT",
    "LOOKin",
    "ESPHome",
    "Infrared",
    "Tuya",
    "UFOR11",
]

TEST_COMMAND_PRIORITIES = (
    ("off", "Power off"),
    ("power_off", "Power off"),
    ("power", "Power toggle"),
    ("toggle", "Power toggle"),
    ("on", "Power on"),
    ("power_on", "Power on"),
)


RAW_BASED_CONTROLLERS = {
    "Xiaomi",
    "MQTT",
    "LOOKin",
    "ESPHome",
    "Infrared",
    "Tuya",
    "UFOR11",
}


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


def _entity_selector():
    return selector.EntitySelector(
        selector.EntitySelectorConfig(multiple=False)
    )


def _infrared_entity_selector():
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=[
                {
                    "domain": "infrared",
                }
            ],
            multiple=False,
        )
    )


def _optional_entity_field(config_key: str, data: dict[str, Any]):
    if data.get(config_key):
        return vol.Optional(config_key, default=data.get(config_key))
    return vol.Optional(config_key)


def _optional_entity_key(config_key: str, value: Any):
    # An EntitySelector rejects an empty-string default ("Entity is neither a
    # valid entity ID nor a valid UUID"), so only set a default when we have a
    # real value; otherwise leave the field truly optional/empty.
    if isinstance(value, str):
        value = value.strip()
    if value:
        return vol.Optional(config_key, default=value)
    return vol.Optional(config_key)


# Which kind of target each controller accepts.
#   - REMOTE: a Home Assistant remote.* entity (remote.send_command).
#   - TEXT:   free text — an MQTT "/set" topic, a service name, an IP, or JSON.
# Tuya is in both: a UFO-R11 is a Tuya device you can drive either via a
# remote.* entity OR by publishing to its Zigbee2MQTT topic, so it gets both
# inputs and the user fills whichever applies. Infrared is handled separately
# via its own infrared.* entity field.
REMOTE_TARGET_CONTROLLERS = ("Broadlink", "LinkNLink", "Xiaomi", "Tuya")
TEXT_TARGET_CONTROLLERS = ("MQTT", "LOOKin", "ESPHome", "Tuya", "UFOR11")


def _remote_entity_selector():
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="remote")
    )


def _controller_data_field(controller: str):
    # Free-text box: accepts an MQTT "/set" topic, a service name / JSON config
    # (ESPHome), an IP address (LOOKin), or a JSON config (Tuya). An explicit
    # TextSelector is used instead of a bare `str` schema type so the box
    # always renders in the frontend (issue #24).
    return selector.TextSelector(
        selector.TextSelectorConfig(multiline=False)
    )


def _looks_like_remote_entity(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith("remote.")


def _add_controller_target_fields(
    schema: dict[Any, Any], controller: str, values: dict[str, Any]
) -> None:
    """Add the controller-data input(s) for ``controller`` into ``schema``.

    Broadlink / LinkNLink / Xiaomi get a remote.* entity picker; MQTT / UFOR11 /
    ESPHome / LOOKin get a text box; Tuya gets both. The two Tuya inputs are
    folded back into a single ``controller_data`` value by
    ``_normalize_controller_target`` on submit. Infrared is handled by the
    caller and never reaches here.
    """
    wants_remote = controller in REMOTE_TARGET_CONTROLLERS
    wants_text = controller in TEXT_TARGET_CONTROLLERS
    if not wants_remote and not wants_text:
        wants_text = True  # safe default for any unknown controller

    entity_default = (values.get(CONF_CONTROLLER_ENTITY) or "").strip()
    data_default = (values.get(CONF_CONTROLLER_DATA) or "").strip()

    # On first load / edit there is only the merged ``controller_data``. If it
    # is actually a remote entity, surface it in the picker rather than the box.
    if wants_remote and not entity_default and _looks_like_remote_entity(data_default):
        entity_default = data_default
        data_default = ""

    if wants_remote:
        schema[
            _optional_entity_key(CONF_CONTROLLER_ENTITY, entity_default)
        ] = _remote_entity_selector()

    if wants_text:
        schema[
            vol.Optional(CONF_CONTROLLER_DATA, default=data_default)
        ] = _controller_data_field(controller)


def _controller_target_conflict(values: dict[str, Any]) -> bool:
    """True when the user filled both the remote entity and the text box."""
    return bool(
        (values.get(CONF_CONTROLLER_ENTITY) or "").strip()
        and (values.get(CONF_CONTROLLER_DATA) or "").strip()
    )


def _normalize_controller_target(data: dict[str, Any]) -> None:
    if data.get(CONF_CONTROLLER) == "Infrared":
        infrared_entity = data.get(CONF_INFRARED_ENTITY) or data.get(CONF_CONTROLLER_DATA)
        data[CONF_INFRARED_ENTITY] = infrared_entity
        data[CONF_CONTROLLER_DATA] = infrared_entity
        return

    # Fold the optional remote-entity picker into the single controller_data
    # value the rest of the integration uses. A genuine both-filled case is
    # blocked earlier by _controller_target_conflict(); text wins as a fallback.
    entity = (data.pop(CONF_CONTROLLER_ENTITY, "") or "").strip()
    text = (data.get(CONF_CONTROLLER_DATA, "") or "").strip()
    data[CONF_CONTROLLER_DATA] = text or entity


def _controller_target_error_key(controller: str) -> str:
    if controller == "Infrared":
        return CONF_INFRARED_ENTITY
    wants_remote = controller in REMOTE_TARGET_CONTROLLERS
    wants_text = controller in TEXT_TARGET_CONTROLLERS
    if wants_remote and wants_text:
        return "base"  # either field satisfies (Tuya)
    if wants_remote:
        return CONF_CONTROLLER_ENTITY
    return CONF_CONTROLLER_DATA


def _source_names_default(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True)


def _build_compatibility_message(
    selected_controller: str,
    device_controller: str,
    command_encoding: str,
) -> str:
    # Broadlink and LinkNLink share the exact same base64 wire format, so a
    # code authored for one needs no adaptation for the other.
    broadlink_family = {"Broadlink", "LinkNLink"}
    if selected_controller in broadlink_family and device_controller in broadlink_family:
        return "No test sent yet."

    if selected_controller == "Infrared":
        return (
            "This code will be converted to native Home Assistant infrared raw "
            "timings. Test a command before saving."
        )

    if (
        selected_controller in RAW_BASED_CONTROLLERS
        and device_controller in broadlink_family
        and command_encoding in {"Base64", "Hex", "Pronto"}
    ):
        return (
            "This code was authored for Broadlink and will be converted to raw "
            f"format for {selected_controller}. Test a command before saving."
        )

    if selected_controller != device_controller:
        return (
            f"This code was authored for {device_controller} and will be adapted "
            f"for {selected_controller}. Test a command before saving."
        )

    return "No test sent yet."


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
        device_data = await async_load_device_data(code, platform)

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

        data_schema: dict[Any, Any] = {
            vol.Required(
                CONF_NAME,
                default=current_values.get(CONF_NAME, default_name),
            ): str,
        }

        if controller == "Infrared":
            infrared_default = (
                current_values.get(CONF_INFRARED_ENTITY)
                or current_values.get(CONF_CONTROLLER_DATA)
            )
            data_schema[
                _optional_entity_key(CONF_INFRARED_ENTITY, infrared_default)
            ] = _infrared_entity_selector()
        else:
            _add_controller_target_fields(data_schema, controller, current_values)

        if platform == "climate":
            data_schema[
                _optional_entity_field(CONF_TEMPERATURE_SENSOR, current_values)
            ] = _temperature_sensor_selector()
            data_schema[
                _optional_entity_field(CONF_HUMIDITY_SENSOR, current_values)
            ] = _humidity_sensor_selector()
            data_schema[
                _optional_entity_field(CONF_POWER_SENSOR, current_values)
            ] = _entity_selector()
            data_schema[
                vol.Optional(
                    CONF_POWER_SENSOR_RESTORE_STATE,
                    default=current_values.get(CONF_POWER_SENSOR_RESTORE_STATE, False),
                )
            ] = bool
            data_schema[
                vol.Optional(
                    CONF_PASSIVE_MODE,
                    default=current_values.get(CONF_PASSIVE_MODE, False),
                )
            ] = bool

        if platform in {"fan", "light", "media_player"}:
            data_schema[
                _optional_entity_field(CONF_POWER_SENSOR, current_values)
            ] = _entity_selector()

        if platform == "media_player":
            data_schema[
                vol.Optional(
                    CONF_DEVICE_CLASS,
                    default=current_values.get(CONF_DEVICE_CLASS, DEFAULT_DEVICE_CLASS),
                )
            ] = str
            data_schema[
                vol.Optional(
                    CONF_SOURCE_NAMES,
                    default=_source_names_default(current_values.get(CONF_SOURCE_NAMES)),
                )
            ] = selector.TextSelector(
                selector.TextSelectorConfig(
                    multiline=True,
                )
            )

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
                "status": self._test_status
                or _build_compatibility_message(
                    controller,
                    device_data["supportedController"],
                    device_data["commandsEncoding"],
                ),
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

            if user_input.get(CONF_GO_BACK):
                return await self.async_step_controller()

            data[CONF_DEVICE_CODE] = int(data[CONF_DEVICE_CODE])
            data[CONF_DELAY] = float(data.get(CONF_DELAY, DEFAULT_DELAY))

            data["controller"] = controller

            if _controller_target_conflict(data):
                return await self._async_show_name_form(
                    user_input,
                    errors={"base": "controller_target_conflict"},
                )

            _normalize_controller_target(data)

            if not data.get(CONF_CONTROLLER_DATA):
                error_key = _controller_target_error_key(controller)
                return await self._async_show_name_form(
                    user_input,
                    errors={error_key: "required"},
                )

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
        self._learn_status: str = ""
        self._draft_data: dict[str, Any] = {}

    def _get_current_data(self) -> dict[str, Any]:
        return {
            **self._config_entry.data,
            **self._config_entry.options,
            **self._draft_data,
        }

    # ── step: init (main options page) ───────────────────────────────────────

    async def async_step_init(self, user_input=None):
        errors = {}

        data = self._get_current_data()
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
            # Navigate to the learn step if requested.
            if user_input.get(CONF_LEARN_COMMAND):
                # Persist the currently selected command path so the learn
                # step can pre-populate its dropdown to the same selection.
                self._pending_override_command = selected_key
                return await self.async_step_learn()

            if user_input.get(CONF_CONTROLLER) != data.get(CONF_CONTROLLER):
                updated_data = {**data, **user_input}
                updated_data[CONF_CONTROLLER_DATA] = ""
                updated_data.pop(CONF_CONTROLLER_ENTITY, None)
                updated_data.pop(CONF_INFRARED_ENTITY, None)
                self._draft_data = updated_data
                return self.async_show_form(
                    step_id="init",
                    data_schema=vol.Schema(
                        self._build_options_schema(
                            updated_data,
                            command_options,
                            selected_key,
                            current_repeat,
                            current_delay,
                            current_remove,
                        )
                    ),
                    errors=errors,
                )

            cleaned_input = dict(user_input)

            if _controller_target_conflict(cleaned_input):
                errors["base"] = "controller_target_conflict"
                updated_data = {**data, **cleaned_input}
                return self.async_show_form(
                    step_id="init",
                    data_schema=vol.Schema(
                        self._build_options_schema(
                            updated_data,
                            command_options,
                            selected_key,
                            current_repeat,
                            current_delay,
                            current_remove,
                        )
                    ),
                    errors=errors,
                )

            _normalize_controller_target(cleaned_input)

            if not cleaned_input.get(CONF_CONTROLLER_DATA):
                error_key = _controller_target_error_key(
                    cleaned_input.get(CONF_CONTROLLER, "")
                )
                errors[error_key] = "required"
                updated_data = {**data, **cleaned_input}
                return self.async_show_form(
                    step_id="init",
                    data_schema=vol.Schema(
                        self._build_options_schema(
                            updated_data,
                            command_options,
                            selected_key,
                            current_repeat,
                            current_delay,
                            current_remove,
                        )
                    ),
                    errors=errors,
                )

            if selected_path:
                remove_override = bool(user_input.get(CONF_OVERRIDE_REMOVE, False))
                repeat_count = int(user_input.get(CONF_OVERRIDE_REPEAT_COUNT, 1) or 1)
                repeat_delay = float(user_input.get(CONF_OVERRIDE_REPEAT_DELAY, 0.0) or 0.0)

                if remove_override or (
                    repeat_count <= 1
                    and repeat_delay <= 0
                ):
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

            cleaned_input[CONF_COMMAND_OVERRIDES] = override_data
            cleaned_input[CONF_OVERRIDE_COMMAND] = selected_key
            cleaned_input.pop(CONF_LEARN_COMMAND, None)
            self._draft_data = {}
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

    # ── step: learn ───────────────────────────────────────────────────────────

    async def async_step_learn(self, user_input=None):
        """
        UI step that lets the user pick a command, point their physical remote
        at the Broadlink device, and capture a live IR code which is then saved
        as a command override.
        """
        errors: dict[str, str] = {}
        data = self._get_current_data()

        # Build command list for the dropdown.
        device_data = await async_load_device_data(
            data.get(CONF_DEVICE_CODE),
            data.get(CONF_PLATFORM),
        )
        command_paths = flatten_command_paths(device_data.get("commands", {}))
        override_data = parse_command_overrides(data.get(CONF_COMMAND_OVERRIDES, {}))
        command_options = [
            selector.SelectOptionDict(
                value=command_path_to_key(path),
                label=(
                    f"{command_path_to_key(path)} [learned]"
                    if isinstance(get_command_value_at_path(override_data, path), dict)
                    else command_path_to_key(path)
                ),
            )
            for path in command_paths
        ]

        # Default the dropdown to whatever was selected on the init page.
        preselected = getattr(self, "_pending_override_command", None) or (
            command_options[0]["value"] if command_options else ""
        )

        if user_input is not None:
            # "Go back" link returns to the main options page.
            if user_input.get(CONF_GO_BACK):
                return await self.async_step_init()

            selected_key: str = user_input.get(CONF_OVERRIDE_COMMAND, preselected)
            broadlink_entity: str = user_input.get(CONF_LEARN_BROADLINK_ENTITY, "")
            timeout: int = int(user_input.get(CONF_LEARN_TIMEOUT, 30))

            if not broadlink_entity:
                errors[CONF_LEARN_BROADLINK_ENTITY] = "required"
            else:
                # Trigger learning via the service handler in __init__.py.
                try:
                    await self.hass.services.async_call(
                        DOMAIN,
                        "learn_command",
                        {
                            "entry_id": self._config_entry.entry_id,
                            "command_path": selected_key,
                            "broadlink_entity": broadlink_entity,
                            "timeout": timeout,
                        },
                        blocking=True,
                    )
                except Exception as err:  # noqa: BLE001
                    self._learn_status = f"Learn failed: {err}"
                    errors["base"] = "learn_failed"
                else:
                    # The service already saved the override and reloaded the
                    # entry.  Let the user know and stay on the learn step so
                    # they can learn another command if they want.
                    self._learn_status = (
                        f"✅ Learned and saved: {selected_key}. "
                        "Point your remote and learn another, or go back to finish."
                    )
                    preselected = selected_key

        return self.async_show_form(
            step_id="learn",
            data_schema=vol.Schema(
                self._build_learn_schema(command_options, preselected, data)
            ),
            errors=errors,
            description_placeholders={
                "status": self._learn_status or (
                    "Select a command, choose your Broadlink or LinkNLink remote "
                    "entity, then click Learn. When prompted, point your physical "
                    "remote at the hub and press the button you want to capture."
                ),
            },
        )

    # ── schema builders ───────────────────────────────────────────────────────

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
                CONF_CONTROLLER,
                default=data.get(CONF_CONTROLLER, CONTROLLERS[0]),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=CONTROLLERS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }

        controller = data.get(CONF_CONTROLLER, CONTROLLERS[0])
        if controller == "Infrared":
            schema[
                _optional_entity_key(
                    CONF_INFRARED_ENTITY,
                    data.get(CONF_INFRARED_ENTITY)
                    or data.get(CONF_CONTROLLER_DATA, ""),
                )
            ] = _infrared_entity_selector()
        else:
            _add_controller_target_fields(schema, controller, data)

        if data.get(CONF_PLATFORM) == "climate":
            schema[
                _optional_entity_field(CONF_TEMPERATURE_SENSOR, data)
            ] = _temperature_sensor_selector()
            schema[
                _optional_entity_field(CONF_HUMIDITY_SENSOR, data)
            ] = _humidity_sensor_selector()
            schema[
                _optional_entity_field(CONF_POWER_SENSOR, data)
            ] = _entity_selector()
            schema[
                vol.Optional(
                    CONF_POWER_SENSOR_RESTORE_STATE,
                    default=data.get(CONF_POWER_SENSOR_RESTORE_STATE, False),
                )
            ] = bool
            schema[
                vol.Optional(
                    CONF_PASSIVE_MODE,
                    default=data.get(CONF_PASSIVE_MODE, False),
                )
            ] = bool

        if data.get(CONF_PLATFORM) in {"fan", "light", "media_player"}:
            schema[
                _optional_entity_field(CONF_POWER_SENSOR, data)
            ] = _entity_selector()

        if data.get(CONF_PLATFORM) == "media_player":
            schema[
                vol.Optional(
                    CONF_DEVICE_CLASS,
                    default=data.get(CONF_DEVICE_CLASS, DEFAULT_DEVICE_CLASS),
                )
            ] = str
            schema[
                vol.Optional(
                    CONF_SOURCE_NAMES,
                    default=_source_names_default(data.get(CONF_SOURCE_NAMES)),
                )
            ] = selector.TextSelector(
                selector.TextSelectorConfig(
                    multiline=True,
                )
            )

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
                # ── learn shortcut ────────────────────────────────────────────
                vol.Optional(CONF_LEARN_COMMAND, default=False): bool,
            }
        )

        return schema

    def _build_learn_schema(
        self,
        command_options: list[Any],
        preselected: str,
        data: dict[str, Any],
    ) -> dict[Any, Any]:
        """Schema for the learn step form."""
        # Pre-fill the remote entity from the device's controller_data if the
        # controller is Broadlink or LinkNLink (both expose a learn-capable
        # remote entity), so the user doesn't have to type it.
        default_broadlink = ""
        if data.get(CONF_CONTROLLER) in ("Broadlink", "LinkNLink"):
            default_broadlink = data.get(CONF_CONTROLLER_DATA, "")

        schema: dict[Any, Any] = {
            vol.Optional(
                CONF_OVERRIDE_COMMAND,
                default=preselected,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=command_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }

        if default_broadlink:
            schema[
                vol.Optional(
                    CONF_LEARN_BROADLINK_ENTITY,
                    default=default_broadlink,
                )
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="remote")
            )
        else:
            schema[
                vol.Optional(CONF_LEARN_BROADLINK_ENTITY)
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="remote")
            )

        schema[
            vol.Optional(CONF_LEARN_TIMEOUT, default=30)
        ] = vol.All(vol.Coerce(int), vol.Range(min=5, max=120))

        schema[vol.Optional(CONF_GO_BACK, default=False)] = bool

        return schema
