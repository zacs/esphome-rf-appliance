# esphome-rf-appliance

A receive-only RF hub for Home Assistant's radio frequency platform. An M5Stack Unit PoE-P4 (ESP32-P4) with a NooElec NESDR SMArt v5 RTL-SDR hears OOK bursts on 433.92 MHz and 915 MHz and hands them to Home Assistant as raw timings, through the standard ESPHome integration.

Status: Phase 0 (research). No firmware yet. See [docs/API-STATUS.md](docs/API-STATUS.md) for the pinned upstreams, what was confirmed in source, and the go/no-go.

License: the firmware will link esp_rtl_sdr (AGPL-3.0-only), so it will be AGPL-3.0.
