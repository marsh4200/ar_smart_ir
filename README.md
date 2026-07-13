# AR Smart IR

[![Latest Release](https://img.shields.io/github/v/release/marsh4200/ar_smart_ir?include_prereleases)](https://github.com/marsh4200/ar_smart_ir/releases)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![Stars](https://img.shields.io/github/stars/marsh4200/ar_smart_ir)](https://github.com/marsh4200/ar_smart_ir/stargazers)



[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](
  https://my.home-assistant.io/redirect/hacs_repository/?owner=marsh4200&repository=ar_smart_ir&category=integration
)

**AR Smart IR** is a modern Home Assistant custom integration for infrared-controlled devices, built to simplify SmartIR-style setups through the Home Assistant UI.

Originally built around Broadlink, AR Smart IR is actively being expanded as support for newer devices and controller methods is developed and tested over time. Current work includes ongoing improvements around MQTT, ESPHome, HEX-based IR codes, and raw command conversion.

It is designed for users who want a cleaner, more modern SmartIR experience without relying on legacy YAML setup.

---

## ✨ Features

- 🌡️ Control **climate devices** such as air conditioners
- 📺 Control **media players** such as TVs, projectors, amps, and receivers
- 🌀 Control **fans**
- 💡 Control **lights**
- ⚙️ Uses modern **Config Flow**
- 🖥️ Setup directly from the **Home Assistant UI**
- 🚫 No full legacy YAML setup required
- 📦 Includes a bundled **local IR codes database**
- 🔁 Supports **command repeat** and **sequence handling**
- 🛠️ Supports **command override** workflows
- 📚 Includes a **Broadlink learn service** for saving replacement commands
- 🔄 Includes command conversion support between **Base64**, **HEX**, **Pronto**, and **Raw** where applicable
- 🧩 Supports Home Assistant's native **Infrared** emitter entities on HA 2026.4+
- 🎛️ Optional **AC presets** (Eco, Quiet, Comfort, …) via an optional `presetModes` key in climate codesets
- 🛑 Optional **Passive mode** for climate — never re-sends unchanged state, letting the AC unit's own thermostat manage temperature
- ⚡ Updated for newer Home Assistant patterns and compatibility

---

## 🚀 Supported Platforms

AR Smart IR currently supports:

- `climate`
- `media_player`
- `fan`
- `light`

---

## 📡 Supported Controllers

AR Smart IR supports multiple controller methods used in Home Assistant:

- **Broadlink**
- **MQTT**
- **ESPHome**
- **Infrared** (`infrared.*` emitter entities)
- **Xiaomi**
- **LOOKin**
- **Tuya**
- **UFO-R11**

The **Infrared** controller consumes an existing Home Assistant native infrared
emitter entity. It does not expose AR Smart IR itself as an infrared emitter or
receiver, and native infrared receiver-based learning is not supported yet.

Controller support continues to improve, especially for newer MQTT- and raw-based workflows.

---

## 🔌 ESPHome Controller Setup

The **ESPHome** controller sends raw IR timings to a user-defined action on your
ESPHome node. You define the action once in your ESPHome YAML, and AR Smart IR
calls it with the decoded `int[]` timing array.

### 1. ESPHome configuration

Define an API action plus a `remote_transmitter` for your IR LED. If you also
drive a 433 MHz RF transmitter from the same node, give it its own
`remote_transmitter` with `carrier_duty_percent: 100%`:

```yaml
api:
  encryption:
    key: ...
  # Needed by bt_adv_proxy
  custom_services: true
  homeassistant_services: true
  actions:
    - action: send_raw_ir_command
      variables:
        command: int[]
      then:
        - remote_transmitter.transmit_raw:
            transmitter_id: ir_transmitter
            carrier_frequency: 38kHz      # REQUIRED for IR — see note below
            code: !lambda 'return command;'

remote_transmitter:
  - pin:
      number: ${RF_TX_PIN}
    # OOK modulation for RF433 — keep duty at 100%
    carrier_duty_percent: 100%
    non_blocking: true
    id: rf_transmitter
  - pin:
      number: ${IR_TX_PIN}
      inverted: false
    carrier_duty_percent: 50%             # 50% for IR LEDs
    non_blocking: true
    id: ir_transmitter
```

> On older ESPHome versions the `actions:` key is named `services:`.

### 2. Point AR Smart IR at the action

In the AR Smart IR setup flow, select **ESPHome** as the controller and set
**Controller data / service name** to the action exactly as it appears in Home
Assistant (Developer Tools → Actions), including the node-name prefix — for
example:

```text
livingroom_ir_send_raw_ir_command
```

### ⚠️ Device doesn't respond? Check the carrier frequency

If there are **no errors** in the log but the AC/TV ignores the command, you are
almost certainly **missing `carrier_frequency: 38kHz`** on `transmit_raw`.
ESPHome defaults the carrier to `0 Hz` (no modulation), and IR receivers only
respond to a modulated ~38 kHz carrier. Broadlink modulates internally, which is
why the same code works there but not over a bare ESPHome `transmit_raw`. Add the
`carrier_frequency` line and reflash the node.

---

## 🆕 What Makes AR Smart IR Different?

AR Smart IR modernizes the classic SmartIR-style experience by focusing on UI-driven setup, cleaner structure, and broader controller flexibility.

### Improvements

- ✅ Setup through **Settings → Devices & Services**
- ✅ Modern **Config Flow**
- ✅ Better support for current Home Assistant versions
- ✅ Local bundled code database
- ✅ Improved controller flexibility
- ✅ Support for command normalization and format conversion
- ✅ Ongoing work for **MQTT**, **ESPHome**, and **HEX/raw** compatibility
- ✅ Easier setup and maintenance for users and installers
- ✅ Learn Function (BroadLink)

---

## 🛠 Compatibility Progress

This project started from a Broadlink-focused base, but development has expanded well beyond that.

Recent work has focused on:

- 📡 Improving **MQTT** command handling
- 🔣 Better support for **HEX-based IR codes**
- 🔄 Improving **raw conversion paths**
- 🧪 Expanding compatibility for **Zigbee2MQTT-style workflows**
- 🔌 Continued refinement of **ESPHome** controller support
- 🧹 General cleanup, reliability fixes, and modernization work

Some controller and device combinations may still need real-world validation, but the integration is actively moving toward broader compatibility across different IR ecosystems.

---

## 📦 Installation

### Install via HACS

Click below to open the repository in HACS:

[![Open your Home Assistant instance and open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=marsh4200&repository=ar_smart_ir&category=integration)

### Manual Installation

Copy the integration into your Home Assistant `custom_components` directory:

```text
config/
└── custom_components/
    └── ar_smart_ir/
Then restart Home Assistant.

🔧 Setup
After installation:

Restart Home Assistant
Go to Settings → Devices & Services
Click Add Integration
Search for AR Smart IR
Follow the setup flow in the UI
📡 IR Code Database
AR Smart IR uses a bundled local IR code database stored inside the integration.

Example location:

custom_components/ar_smart_ir/codes/
Each supported platform has its own folder, such as:

codes/climate/
codes/media_player/
codes/fan/
codes/light/
Each device is defined with a JSON file containing controller and command information.

Example structure:

{
  "manufacturer": "ExampleBrand",
  "supportedModels": ["Model123"],
  "supportedController": "Broadlink",
  "commandsEncoding": "Base64",
  "commands": {
    "off": "JgBQAAAB...",
    "on": "JgBQAAAB..."
  }
}
Depending on the device and controller workflow, commands may use formats such as:

Base64
HEX
Pronto
Raw

🎛️ Climate Presets (optional)
Climate codesets can optionally declare AC presets (e.g. Eco, Quiet, Comfort on Samsung units) by adding a `presetModes` key. When present, an extra preset level sits between the fan/swing level and the temperatures:

{
  "presetModes": ["eco", "quiet"],
  "commands": {
    "off": "JgBQAAAB...",
    "cool": {
      "auto": {
        "none": { "18": "JgBQ...", "19": "JgBQ..." },
        "eco":  { "18": "JgBQ...", "19": "JgBQ..." }
      }
    }
  }
}

The "none" preset holds the standard commands. If a preset is missing at any point in the tree, the integration falls back to "none" (or the flat temperature layout). Existing codesets without `presetModes` are completely unaffected and keep the original format.

🛑 Passive Mode (optional)
Climate entities have an optional Passive mode toggle in the setup and options flow. When enabled, AR Smart IR never re-sends a command unless something actually changed (mode, fan, swing, preset, or target temperature), and only sends the discrete "on" command when the unit is genuinely being switched on. This lets the AC unit's own built-in thermostat and hysteresis manage the temperature — AR Smart IR only transmits when you make a control change. It is off by default, so existing setups behave exactly as before.

📌 Notes
This project was originally based on and tested around Broadlink, but support has expanded significantly beyond that
MQTT, ESPHome, HEX, and Raw workflows are actively being improved
Some setups may still require device-specific testing depending on the IR blaster and command format
Real-world compatibility can vary based on the quality and structure of the source code file being used
Native Home Assistant Infrared support requires Home Assistant 2026.4.0 or newer
🙌 Credits
AR Smart IR is inspired by the original SmartIR project and the wider Home Assistant community.




