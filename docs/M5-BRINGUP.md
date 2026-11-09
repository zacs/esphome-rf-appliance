# M5 Unit PoE-P4 bring-up (Phase 1)

Status, 2026-11-06: the firmware builds and passes its config tests. The bench steps below need the board. The measurement table is still blank.

## What is done and what is left

Done without hardware:

- The `rtl_sdr` hub component: driver, USB reader task, DSP task (counts samples for now), band hopping, fault recovery, diagnostics, status LED. See `components/rtl_sdr/`.
- `examples/m5-unit-poe-p4.yaml`. It compiles with ESPHome 2026.10.2 and ESP-IDF 6.1.0, with no warnings. Image 640 KB, static RAM 15 %.
- `bringup/esphome-rtl433-m5.yaml`: esphome-rtl433's own example with only the board block changed, for step 1.
- `tests/config_tests.py`: 45 cases (valid and invalid) plus the example. All pass.
- `tests/hw_smoke.py`: the smoke test and the 24-hour soak monitor.

Left for the bench (the owner):

- Steps 1 to 5 below, and the [measurement table](#measurements).
- Phase 1 exit: 24 hours with 0 dropped samples and 0 USB overruns.

## Board facts

All confirmed in a vendor source. "Docs" is the [Unit PoE-P4 page](https://docs.m5stack.com/en/unit/Unit_PoE-P4). "Schematic" is `SCH_StickPoE_SCH_Main_V0.2_20250829` linked from it. "Demo" is [m5stack/M5Unit-PoE-P4-UserDemo](https://github.com/m5stack/M5Unit-PoE-P4-UserDemo) at `2840dce`.

| Item | Fact | Source |
| --- | --- | --- |
| SoC | ESP32-P4NRW32, 360 MHz dual core | Docs |
| Flash, PSRAM | 16 MB flash, 32 MB PSRAM. The docs call it "Octal"; the P4 drives in-package PSRAM in hex mode. Check the boot log. | Docs |
| Two SKUs | Unit PoE-P4 (U213): silicon below v3, `engineering_sample: "true"`. Unit PoE-P4X: v3.x silicon, `"false"`. | Demo README |
| Ethernet | IP101GRI, PHY address 1. MDC GPIO31, MDIO GPIO52, PHY reset GPIO51. | Docs pin map, demo `sdkconfig:717-725` |
| RMII | CRS_DV GPIO28, RXD0 29, RXD1 30, TXD0 34, TXD1 35, TX_EN 49, REF_CLK in on GPIO50 | Docs pin map; ESP-IDF P4 EMAC defaults (`esp_eth_mac_esp.h` v6.1 `:232-260`) |
| GPIO51 | PHY reset, not a power enable. ESPHome's `power_pin` drives the PHY reset, so `power_pin: GPIO51` is right. | Demo `ethernet_init.c:76`, ESPHome `ethernet_component_esp32.cpp:318` |
| USB host port | USB-C J1 on the P4's high-speed OTG pins, through a common-mode choke | Schematic p.4 |
| **Host port VBUS** | **Always on.** SYS_5V feeds VBUS through Schottky D5 (DSK34). No GPIO controls it. CC1 and CC2 have 5.1 kΩ pull-downs. U5 (AW32901FCR) only lets power flow the other way, port to SYS_5V, with its enable tied low. | Schematic p.4 |
| Download port | USB-C on GPIO24/25 (USB0 DP/DN) | Docs pin map |
| Console UART | UART0: TX GPIO37, RX GPIO38, on the SDIO & ISP header (3.3 V) | Docs pin map |
| RGB LED | NH-B2020RGBA-HF, common anode. Green GPIO15, blue GPIO16, red GPIO17. Drive inverted. | Docs pin map, demo `led_ctrl.cpp` |
| Button | GPIO45 | Docs pin map |
| IR transmitter | GPIO14. Unused. | Docs pin map |
| Grove | GPIO53, GPIO54. Unused. | Docs pin map |
| PoE | TPS23753A, 802.3at in, 6 W max out | Docs |
| Board current | 5 V at 73.8 mA idle (0.37 W). 277 mA at full load with display, camera, IR and RGB. | Docs |
| Temperature | 0 to 40 °C | Docs |
| Download mode | "Press and hold the side power button until the internal green LED lights up, then release it. The green LED turns off." | Docs |

### What the always-on VBUS means

- esp_rtl_sdr power-cycles the root port after a failed enumeration. On this board that only toggles the P4's port-power bit. It does not cut power to the dongle.
- A dongle that stops answering cannot be power-cycled from firmware. The fallbacks are a physical replug, or bouncing the PoE port on the switch.
- The firmware still recovers from an unplug, a USB error and a stalled stream on its own. The driver's other recovery paths do not need VBUS control.
- VBUS sits one Schottky drop below SYS_5V, about 4.6-4.7 V under the dongle's load. Measure it (table below).

## Measurements

Fill in on the bench.

| What | How | Value |
| --- | --- | --- |
| Silicon revision | `esptool --chip esp32p4 chip_id` (look for "Chip is ESP32-P4 (revision vX.Y)") | |
| Flash size | `esptool --chip esp32p4 flash_id` | |
| PSRAM size and mode | Boot log, `esp_psram` lines | |
| Dongle enumeration | Boot log: "Dongle: ... (high speed)" | |
| PoE draw, idle (no dongle) | Switch port power reading | |
| PoE draw, streaming 433.92 MHz | Switch port, during a 433 dwell | |
| PoE draw, streaming 915 MHz | Switch port, during a 915 dwell | |
| Dongle current | Inline USB meter on the C-to-A adapter | |
| VBUS at the dongle | Inline USB meter, while streaming | |
| Chip temperature | `Chip Temperature` sensor after 1 h, in its box | |
| CPU load, core 0 / core 1 | `CPU Load Core 0/1` sensors | |
| Free internal heap, PSRAM | `Heap Free`, `PSRAM Free` at start and after 24 h | |
| Stack headroom | `Reader Stack Free`, `DSP Stack Free` (bytes) | |
| Measured rate per band | `Effective Sample Rate` while on each band | |

## Bench procedure

You need: the M5, the NooElec SMArt v5 with its antenna, a short USB-C (male) to USB-A (female) adapter, an 802.3at PoE switch port, a USB-C cable for flashing, and optionally a 3.3 V USB-UART on GPIO37/38. Keep the bias tee off. The firmware forces it off at every stream start.

### Step 0: identify the board

1. Enter download mode (see the table above). Connect the download USB-C to the PC.
2. Run `esptool --chip esp32p4 chip_id` and `esptool --chip esp32p4 flash_id`.
3. Set `engineering_sample` in both YAML files from the revision: below v3.0 is `"true"`, v3.x is `"false"`. Fix `flash_size` if it is not 16 MB.
4. Copy `examples/secrets.yaml.example` to `examples/secrets.yaml` and to `bringup/secrets.yaml`. Fill in a fresh API key. It encrypts both the native API and OTA uploads, so there is no OTA password.

### Step 1: prove the hardware with esphome-rtl433

```bash
esphome run bringup/esphome-rtl433-m5.yaml
```

Pass: the log shows the dongle attached at high speed, both bands visited, and frames detected. `http://<device>:8433/cmd?cmd=get_stats` shows frame counts. This only proves the board and dongle. Nothing from it is kept.

### Step 2: flash the puck firmware

```bash
esphome run examples/m5-unit-poe-p4.yaml
```

The LED shows the radio state, dim and steady:

| Colour | State |
| --- | --- |
| Blue | Driver starting |
| Amber | No dongle on the port |
| Green | Streaming |
| Red | Stream will not start; retrying with backoff |
| Magenta | USB disabled after repeated USB crashes; power-cycle to clear |

### Step 3: five-minute smoke test

```bash
pip install aioesphomeapi
python3 tests/hw_smoke.py <device-ip> --key <api key>
```

Pass: `PASS`, with both bands seen, sample rates within 2 % of 250 kS/s and 2048 kS/s, and no dropped samples, overruns, restarts, reboots or API drops.

### Step 4: unplug test (early look at an acceptance item)

1. While streaming, pull the dongle. The LED turns amber and `Dongle` goes off.
2. Plug it back. Within a few seconds the LED is green again and `Stream Restarts` has gone up by one.
3. The device must not reboot (`Uptime` keeps counting).

### Step 5: the Phase 1 exit soak

```bash
python3 tests/hw_smoke.py <device-ip> --key <api key> --duration 24h --csv soak-24h.csv
```

Pass: `PASS`. That means 0 dropped samples, 0 USB overruns, 0 stream restarts, no reboots, no API drops and flat internal heap over 24 hours. Keep the CSV and fill in the table above.

If samples drop, raise `usb_buffer` (for example to `2MB`) or `blocks` and repeat. Note what changed.

## Placement and the Seattle install

From Home Assistant's Radio frequency page (`radio_frequency.markdown:58-69`):

- Keep the puck away from large metal objects. They block and reflect RF.
- Keep it away from Wi-Fi routers, USB 3.0 hardware and other transmitters on 433.92 or 915 MHz.
- Orient the antenna for coverage. Vertical suits most remotes and sensors.
- If something is out of range, add another adapter closer to it.

For Seattle:

- The board is rated 0 to 40 °C. Use a shaded, vented box. Watch `Chip Temperature` in the first warm week.
- Wet winters: mount the box with its cable entry facing down, and add a drip loop on the Ethernet cable.
- PoE: an 802.3at port. The 6 W output is enough for the board (0.37 W) plus the dongle. Fill in the measured draw above.
- Bias tee stays off. Use a passive antenna.
- The dongle cannot be power-cycled from firmware (always-on VBUS). If it ever wedges, bounce the PoE port from the switch.

## Open items

- Two silicon variants exist. Which one is on the bench decides `engineering_sample`.
- PSRAM: the vendor page says "Octal". ESPHome runs the P4's in-package PSRAM in hex mode, which worked on the Waveshare board with the same chip. Confirm in the boot log.
- No automatic reboot exists for a reader task stuck inside a driver call. The hub flags it as an error after 2 minutes. Whether to add a reboot there is a decision for after the soak.
