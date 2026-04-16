# AR Smart IR

[![GitHub release](https://img.shields.io/github/v/release/marsh4200/ar_smart_ir.svg)](https://github.com/marsh4200/ar_smart_ir/releases)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub stars](https://img.shields.io/github/stars/marsh4200/ar_smart_ir.svg?style=social)](https://github.com/marsh4200/ar_smart_ir/stargazers)

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
- **Xiaomi**
- **LOOKin**
- **Tuya**

Controller support continues to improve, especially for newer MQTT- and raw-based workflows.

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



📌 Notes
This project was originally based on and tested around Broadlink, but support has expanded significantly beyond that
MQTT, ESPHome, HEX, and Raw workflows are actively being improved
Some setups may still require device-specific testing depending on the IR blaster and command format
Real-world compatibility can vary based on the quality and structure of the source code file being used
🙌 Credits
AR Smart IR is inspired by the original SmartIR project and the wider Home Assistant community.





