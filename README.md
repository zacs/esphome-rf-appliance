# esphome-rf-appliance

A receive-only RF hub for Home Assistant's radio frequency platform. An M5Stack Unit PoE-P4 (ESP32-P4) with a NooElec NESDR SMArt v5 RTL-SDR hears OOK bursts on 433.92 MHz and 915 MHz and hands them to Home Assistant as raw timings, through the standard ESPHome integration.

Status: Phase 1 (board bring-up). The `rtl_sdr` hub streams both bands and counts samples; the bench soak is next. There is no receiver entity yet (Phase 3).

- [docs/API-STATUS.md](docs/API-STATUS.md): pinned upstreams, what was confirmed in source, and the owner's decisions.
- [docs/M5-BRINGUP.md](docs/M5-BRINGUP.md): board facts, bench procedure and the Phase 1 exit soak.

## Layout

- `components/rtl_sdr/`: the ESPHome component (hub).
- `examples/m5-unit-poe-p4.yaml`: the M5 config. Copy `examples/secrets.yaml.example` to `examples/secrets.yaml` first.
- `bringup/esphome-rtl433-m5.yaml`: bring-up step 1, esphome-rtl433 used unmodified to prove the hardware.
- `tests/config_tests.py`: config validation tests, no hardware needed.
- `tests/hw_smoke.py`: smoke test and soak monitor for a running puck.

## Tests

Use the pinned ESPHome:

```bash
pip install esphome==2026.10.2
```

```bash
python3 tests/config_tests.py
```

```bash
esphome compile examples/m5-unit-poe-p4.yaml
```

License: the firmware will link esp_rtl_sdr (AGPL-3.0-only), so it will be AGPL-3.0.
