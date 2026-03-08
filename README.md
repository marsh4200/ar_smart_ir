# AR Smart IR

[![GitHub release](https://img.shields.io/github/v/release/marsh4200/ar_smart_ir.svg)](https://github.com/marsh4200/ar_smart_ir/releases)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)

[![Open your Home Assistant instance and open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?repository=marsh4200/ar_smart_ir&category=integration)

**AR Smart IR** is a modern infrared control integration for **Home Assistant** that allows you to control devices such as **TVs, air conditioners, projectors, fans, lights, and media players** using IR transmitters.

Built for modern Home Assistant systems, **AR Smart IR removes the need for YAML configuration** and allows devices to be added directly through the **Integrations UI**.

---

# ✨ Features

- 🌡️ Control **climate devices** (air conditioners)
- 📺 Control **media players** (TVs, projectors, receivers)
- 🌀 Control **fans**
- 💡 Control **lights**
- ⚙️ Uses modern **Config Flow**
- 🖥️ Setup directly from **Home Assistant UI**
- 🚫 **No YAML configuration required**
- ⚡ Updated compatibility with modern Home Assistant versions
- 📡 Uses a **local IR codes database**

---

# 🚀 Supported Controller Methods

AR Smart IR works with multiple IR transmitters supported by Home Assistant:

- **ESPHome IR transmitters**
- **MQTT publish services**
- **Broadlink IR controllers**
- **Xiaomi IR Remote**
- Other compatible **Home Assistant remote platforms**

---

# 🆕 What Makes AR Smart IR Different?

AR Smart IR modernizes legacy infrared integrations by removing complex setup steps.

### Improvements

- ✅ No more `configuration.yaml`
- ✅ Setup through **Settings → Devices & Services**
- ✅ Modern **Config Flow installation**
- ✅ Cleaner integration structure
- ✅ Better compatibility with newer Home Assistant versions
- ✅ Faster async processing
- ✅ Easier installation for users and installers

---

# 📦 Installation

## Install via HACS (Recommended)

Click the button below to open the repository in HACS:

[![Open your Home Assistant instance and open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?repository=marsh4200/ar_smart_ir&category=integration)

---

##### Manual Installation

Copy the integration into your Home Assistant `custom_components` directory:

```text
config/
└── custom_components/
    └── ar_smart_ir/
        ├── __init__.py
        ├── manifest.json
        ├── config_flow.py
        ├── climate.py
        ├── fan.py
        ├── light.py
        ├── media_player.py
        ├── controller.py
        ├── services.yaml
        ├── strings.json
        ├── translations/
        │   └── en.json
        ├── codes/
        │   └── climate
        │   └── media_player
        │   └── light
        └── icons.json

---

# 🔧 Setup

After installation:

1. Restart **Home Assistant**
2. Go to **Settings → Devices & Services**
3. Click **Add Integration**
4. Search for **AR Smart IR**
5. Follow the setup wizard

---

# 📡 IR Codes Database

AR Smart IR uses a **local IR code database** stored in the integration.

Location:


custom_components/ar_smart_ir/codes/


Each supported device type has its own folder.

Example:


codes/climate
codes/media_player
codes/fan
codes/light


Each device is defined using a **JSON command file**.

Example structure:

```json
{
  "manufacturer": "ExampleBrand",
  "supportedModels": ["Model123"],
  "commands": {
    "power_on": "2600 0000 006D 0022 ...",
    "power_off": "2600 0000 006D 0022 ..."
  }
}

This system allows new devices to be easily added to the database.

🏠 Supported Device Types

AR Smart IR currently supports:

Climate devices

Media players

Fans

Lights

Device control is achieved by sending infrared commands through supported controller platforms.
