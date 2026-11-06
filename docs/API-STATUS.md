# API status (Phase 0)

Research only. No firmware code exists yet. Written 2026-11-06.

Every claim below was checked in source at the commit in the pin table. File and line references are at those commits. Where I could not check something, it says so.

## Verdict

**Go for Phase 1 (board bring-up).** The driver, the ESPHome entity and the HA receiver all exist and fit together. Nothing in Phase 1 depends on the open questions below.

**Three things in the brief cannot be met by stock HA and ESPHome as they stand.** Each needs an owner decision before Phase 3. Details are in [Decisions for the owner](#decisions-for-the-owner).

1. An ESPHome radio frequency entity reports **one** frequency range. A two-band puck cannot report two honest ranges on one entity.
2. The receive message carries **no frequency**. HA always gets `frequency=None`.
3. HA's **Settings > Connectivity > Radio frequency** panel lists **transmitters only**. A receive-only puck does not show up there.

## Pin table

| Upstream | Pin | Notes |
| --- | --- | --- |
| Home Assistant core | `dev` at `6d60348abcce14040dbce48ce38e38a5c2ceb6fc` | Merge of core#189484, 2026-11-02. Version `2026.12.0.dev0` (`homeassistant/const.py:24-26`). |
| Home Assistant core, last release | `2026.11.0` at `74cf5a49c63ed8d4d87717844bf14f79c28c9cea` | Has the generic receiver entity, but **not** the ESPHome receiver. |
| aioesphomeapi | `v46.25.0` at `696f12f277651e43b60874e8745dd8945850d92d` | The version HA pins (`homeassistant/components/esphome/manifest.json:22`). |
| ESPHome | `2026.10.2` at `9a78022ded2285861f10de50168a0aa71e2a4322` | Current stable. Proposed pin. |
| ESPHome, checked for drift | `2026.11.0b2` at `3cfe8231c72c4aa787f89b6352bdd77b05dfd56b`, `dev` at `d8d485d11c3b0e22bc32f409073cafcab2b7fc4c` | See [ESPHome drift](#esphome-drift). |
| ESP-IDF | `v6.1` at `fff9895c82d744c7237be8847347bdd1b07c6643` | Proposed. Fallback `v5.5.6` at `880a203ae603d08a94d06aa01e0d6761958e5e9e`. To confirm with the first compile. |
| esp_rtl_sdr | `davidcoulson/esp-rtl-sdr` tag `v0.9.4-dc.1` = `78d420d188971c941e211904e214678073e16d8d` | The fork esphome-rtl433 pins. Pin by SHA, not tag. |
| espressif/usb | `1.6.0` | Required by esp_rtl_sdr on IDF 6 (`idf_component.yml`). |
| rtl_433 (port source) | `merbanan/rtl_433` at `bd9073191aff109d7ea013bdcb921992b4f5a11f` | Same base as esphome-rtl433, so CPU numbers compare. Master `f35183aa` differs in the DSP files only cosmetically. |
| rtl_433_tests | `1aa8c29d6b9383739d1aeed495dbc5069b704392` | Capture source for host tests. |
| esphome-rtl433 (read only) | `v0.1.13` at `c06a76106e286833ce226d62e5136cc8f54314e6` | Lessons only. Not forked. |
| HA frontend (checked) | `dev` at `5cfdff0e21690256ea780efc76f354f6c1f0c3a1` | Radio frequency panel. |
| HA user docs (checked) | `home-assistant.io` `next` at `e8f44a1`, `current` at `8f3d51c` | |
| HA contributor docs (checked) | `contributors.home-assistant.io` at `abdc493` | |
| M5Stack vendor demo (checked) | `m5stack/M5Unit-PoE-P4-UserDemo` at `2840dce` | Pin source while the M5 docs site is unreachable. |

## Home Assistant: core#189484

### Release

- Merged 2026-11-02 as `6d60348` onto `dev`, which is `2026.12.0.dev0` (`homeassistant/const.py:24-26`).
- `2026.11.0` does not contain it. Its `esphome/radio_frequency.py` has no receiver class.
- First release that will carry it: **2026.12.0**. Use a `dev` build until then.
- Docs PRs: home-assistant.io#49508 (receivers in the Radio frequency page, merged 2026-10-23) and home-assistant.io#50031 ("Add RF receivers to ESPHome", merged 2026-11-02).

### Capability flags

- `RadioFrequencyCapability.TRANSMITTER = 1 << 0`, `RECEIVER = 1 << 1` (aioesphomeapi `model.py:230-232`).
- HA sets up an entity if either flag is set (`esphome/radio_frequency.py:154-163`, filter at `:161`).
- **TRANSMITTER and RECEIVER together give a receiver-only entity.** `_make_radio_frequency_entity` picks the receiver class whenever RECEIVER is set (`esphome/radio_frequency.py:138-151`, check at `:148`). The test `test_radio_frequency_multiple_entities_mixed_capabilities` asserts a transceiver becomes `receiver` (`tests/components/esphome/test_radio_frequency.py:108-150`).
- Several radio frequency entities on one device are fine. That same test sets up three.

### Receive path

- HA subscribes with `client.subscribe_infrared_rf_receive(...)` (`esphome/radio_frequency.py:105-114`). In aioesphomeapi that only registers a local callback. It sends nothing to the device (`client.py:760-771`).
- The message model is `InfraredRFReceiveEvent(device_id, key, timings)` (aioesphomeapi `model.py:220-223`).
- HA drops any event whose `key` or `device_id` does not match the entity (`esphome/radio_frequency.py:126-131`). Every burst must carry the receiver entity's key.
- HA builds `RadioFrequencyReceivedSignal(timings=event.timings)` (`esphome/radio_frequency.py:134`). **No frequency, no modulation.** The dataclass defaults are `frequency=None`, `modulation=OOK` (`radio_frequency/entity.py:29-35`).
- Each signal sets the state to "now" and calls `async_write_ha_state()` (`radio_frequency/entity.py:161-176`). **Every forwarded burst is a state change, and the recorder stores it.** The rate cap protects the database as well as the event loop.

### Frequency ranges

- The ESPHome entity returns exactly one range: `[(frequency_min, frequency_max)]` (`esphome/radio_frequency.py:44-46`).
- The base class supports a list (`radio_frequency/entity.py:50-53`). The ESPHome mapping and the API message do not.
- Consumers find receivers with `async_get_receivers(hass, frequency, modulation)`, which checks `low <= frequency <= high` (`radio_frequency/__init__.py:128-167`, `entity.py:55-61`).
- Modulation is always OOK (`entity.py:63-67`).

### State and the Radio frequency panel

- A receiver's state is the ISO timestamp of the last signal. The base class owns it and marks it final (`radio_frequency/entity.py:141-146`). We add nothing to it.
- `radio_frequency/list` (admin websocket) returns `transmitters` and `receivers` (`radio_frequency/websocket_api.py:22-63`).
- **The frontend panel reads only `transmitters`.** The router keeps `result.transmitters` (`radio-frequency-config-dashboard-router.ts:45-48`). The online count loops over transmitters (`radio-frequency-config-dashboard.ts:32-38`). The type file has no receiver type (`src/data/radio_frequency.ts`). I found no frontend PR adding receivers.
- So the acceptance check "shows as online under Settings > Connectivity > Radio frequency" fails on stock HA today, for any receive-only device, not just ours.

### User-facing docs

- The brief says the HA page "still describes transmitters only". That is out of date. Both `current` and `next` now describe receivers.
- Terms: "radio frequency remote adapter (proxy)", "receiver", "radio frequency entity" (`radio_frequency.markdown:86-114`).
- State: no on/off, timestamp of last use (`:116-130`).
- Placement guidance to copy into `docs/M5-BRINGUP.md` (`:58-69`).
- The contributor docs say: "If your hardware does both, create one entity of each" (`radio-frequency.md:56`). ESPHome's mapping instead turns a dual-flag entity into a receiver. That matters only for transmitters, so it does not affect us.

## ESPHome: how ir_rf_proxy looks to HA

### Advertising RECEIVER

- Capability flags live in `ir_rf_base` (`ir_rf_base/ir_rf_base.h:24-27`).
- A remote_receiver-backed entity gets `supports_receiver_` in `IrRfEntity::setup()` (`ir_rf_base.cpp:35-41`).
- A platform with its own hardware calls `set_supports_receiver(true)` before that, and the flags are merged (`ir_rf_base.h:91-96`, comment at `:91-92`).
- The listing message is `ListEntitiesRadioFrequencyResponse` (id 148): `capabilities`, `frequency_min`, `frequency_max`, `supported_modulations` (`api/api.proto:2858-2875`). It is filled from the entity's traits (`api/api_connection.cpp:1804-1813`).
- ir_rf_proxy advertises OOK in `RfProxy::setup()` (`ir_rf_proxy/ir_rf_proxy.cpp:36-40`). Its optional `frequency:` is a **fixed** frequency, min == max (`ir_rf_proxy/radio_frequency.py:20`, `:61-62`; `ir_rf_proxy.h:56`).
- An ir_rf_proxy entity has a receiver **or** a transmitter, never both (`ir_rf_proxy/radio_frequency.py:29`).

### Sending received timings

- The message is `InfraredRFReceiveEvent` (id 137): `device_id`, `key`, `repeated sint32 timings`, microseconds, positive mark, negative space (`api/api.proto:2821-2831`).
- `RadioFrequency::on_receive(RemoteReceiveData)` runs local callbacks, then `IrRfEntity::on_receive` (`radio_frequency/radio_frequency.cpp:28-31`).
- `IrRfEntity::on_receive` sends the event with `get_object_id_hash()` as the key (`ir_rf_base.cpp:114-126`).
- The API server sends it to **every** connected client. There is no subscription on the device side (`api/api_server.cpp:517-528`).
- If a client's TCP buffer is full, the event is dropped with only a verbose log (`api/api_connection.cpp:1626-1632`).

### What our platform must do to look identical

- Subclass `radio_frequency::RadioFrequency` and register it with `radio_frequency.new_radio_frequency()` (`radio_frequency/__init__.py:66-75`; schema at `:35`). That gives the same entity type, listing message and key.
- In `setup()`: `set_supports_receiver(true)`, then `IrRfEntity::setup()`, then set traits and OOK.
- Build `remote_base::RemoteReceiveData(timings, tolerance, mode)` (`remote_base/remote_base.h:55-58`) and call `this->on_receive(data)` from the **main loop**. The API is not thread-safe, so design rule 8 (queue to the loop) is required, not optional.
- `control()` is pure virtual (`radio_frequency/radio_frequency.h:119`). We implement it to return `false`. HA never calls it on a receiver.
- No new API messages and no HA change are needed for the basic path.

### ESPHome drift

- `2026.11.0b2`: receive path unchanged, with two additions. Internal entities no longer send receive events (`ir_rf_base.cpp`). A config validator rejects IR and RF entities that share a key, because "the API messages for both kinds identify the entity by key alone" (`ir_rf_base/__init__.py`).
- `dev`: `RemoteReceiveData` now takes a `RawTimingsView`. The vector constructor is kept (`remote_base.h:91-97` on dev). Our call still compiles, but this is why ESPHome must be pinned.
- radio_frequency and ir_rf_base are marked EXPERIMENTAL in their headers. Expect more drift.

## Driver: esp_rtl_sdr

### Which fork, and the #26 lead

- The brief says esphome-rtl433 pins a fork "until esp-rtl-sdr#26 lands". **#26 will not land.** It was closed unmerged.
- Its parts were split into #45 to #48. Upstream took the band-select and unplug-recovery parts as #49 and #50, plus its own #43 and #44, in v0.9.3.
- #46 (in-place rate change) and #47 (R820T2 IF filter follows the rate) were closed unmerged.
- esphome-rtl433 now pins `davidcoulson/esp-rtl-sdr` `v0.9.4-dc.1` (`components/rtl_433/__init__.py:47-48`). That is upstream v0.9.4 plus recovery fixes and the IF filter as a Kconfig choice.
- The Kconfig default is `VENDOR_FIXED`. esphome-rtl433 sets `IF_TABLE` (`__init__.py:472`), which took 915 MHz from deaf to working on the NooElec (README `:143`). We must set it too.
- Upstream `hardcoreerik/esp-rtl-sdr` is at `11c7b56` (v0.9.4, 2026-11-04).

### API we will use (fork `include/esp_rtl_sdr.h`)

| Call | Line | Use |
| --- | --- | --- |
| `esp_rtl_sdr_config_t` | `:667-712` | `delivery_mode = READ` (`:693`), `pull_ring_bytes` (`:702`), `usb_task_core_id`, `usb_task_priority`, `event_cb`. |
| `esp_rtl_sdr_install` | `:850` | Once per boot. Works with no dongle. Returns `ERR_USB_SAFE_MODE` after repeated enumeration panics. |
| `esp_rtl_sdr_start_hz` | `:1029` | Start at a frequency and rate. |
| `esp_rtl_sdr_set_center_freq` / `retune_hz` | `:988`, `:946` | Hot retune while streaming. |
| `esp_rtl_sdr_set_sample_rate` | `:1001` | Returns `ERR_BUSY` while streaming. A rate change needs stop and start. |
| `esp_rtl_sdr_read` | `:1021` | Blocking pull of CU8 bytes. |
| `esp_rtl_sdr_stop` / `reset` | `:959`, `:968` | Clear FAULT. |
| `esp_rtl_sdr_get_metrics` | `:905`, struct `:413-454` | Overruns, ring drops (separate counters), effective rate. |
| `esp_rtl_sdr_set_tuner_gain_mode(AUTO)` | `:1241` | Auto gain, after the stream starts. |
| `esp_rtl_sdr_set_bias_tee` | `:1335` | We never call it. Bias tee stays off. |

### Recovery already in the driver

- After 10/20/40/60 s with nothing enumerated, the driver power-cycles the root port with `usb_host_lib_set_root_port_power(false/true)`, 300 ms off (`src/esp_rtl_sdr.cpp:4135-4192`, `:4055`).
- **Whether that removes VBUS on the M5 is unknown.** It only toggles the controller's port-power bit. If the board has no load switch on that signal, VBUS stays on. To measure in Phase 1.

## Lessons from esphome-rtl433 (read, not forked)

- **Tasks.** Decode task on the core the ESPHome loop is not on, 64 KB stack (`rtl_433_component.cpp:20`, `:76-84`). Acquire thread and USB task on the loop core, 8 KB stack, measured peak 1.6 KB (`:97-103`). Our DSP needs far less stack than rtl_433's decoders. Measure it.
- **Driver install.** Install once and keep it. Uninstalling with a dongle attached fails (`rtl_433-esp.patch:1156-1159`). READ delivery mode (`:1234`).
- **Start failures.** On FAULT: stop, reset, retry. Back off 1 s to 30 s (`:1330-1369`).
- **After retune.** Drop what is buffered, and drop any block that a retune landed in (`:1373-1382`, `:1405-1494`).
- **Unplug.** Count disconnect and error events in the callback, and act on them in the reader. Never touch the reader's state from the USB task (`:1181-1214`).
- **Liveness.** Flag an error if the loop or the sample flow stalls for 30 s (`rtl_433_component.cpp:155-186`).
- **Buffers.** The default pull ring rides out only about 50-125 ms at 2 MS/s. Raise it to 1 MB or more if drops appear (README `:115`). PSRAM through malloc (`__init__.py:471`). Control transfers up to 1024 bytes (`:476`). Task watchdog 10 s (`:481`).
- **Logs.** A log flood on the 115200-baud UART stalled the ESPHome loop. Rate-limit noisy messages (`rtl_433_component.cpp:23-44`).
- **Measured cost.** 915 MHz at 2048 kS/s: 89 % of the decode core (33 % with squelch), 45 % of the USB core, at 360 MHz (README `:31-38`). That includes every rtl_433 decoder. Ours runs no decoders, so expect less. To measure.
- **Hops.** A hop that changes the rate restarts the stream, about 1 s deaf (README `:38`).
- **Latency.** rtl_433's block is 262,144 bytes (`include/rtl_433.h:17`). That is 64 ms at 2048 kS/s but **524 ms at 250 kS/s**. Our 1-second budget needs a smaller block at 250 kS/s. The "small blocks starve the loop" lesson came from rtl_433's socket hand-off between tasks. A FreeRTOS ring does not cost that.

## rtl_433: smallest port set

At `bd90731`. GPL-2.0-or-later, which can be combined into an AGPL-3.0 firmware.

| File | Keep | Why |
| --- | --- | --- |
| `src/baseband.c`, `include/baseband.h` | `envelope_detect` (`:36`), `baseband_low_pass_filter` (`:139`), `baseband_demod_FM` (`:204`), `baseband_init` (`:362`) | AM envelope, AM low-pass, FM demod for the FSK check and the carrier estimate. Drop CS16 paths. Stub `print_logf` (`:217`). |
| `src/pulse_detect.c`, `include/pulse_detect.h` | `pulse_detect_package` (`:199-483`) and setup | The OOK/FSK pulse detector. Strip the verbose histograms (`:189-196`, `:280`). |
| `src/pulse_detect_fsk.c`, `include/pulse_detect_fsk.h` | All | Needed to classify FSK so we can drop and count it. |
| `include/pulse_data.h` | Struct and limits | Plus `pulse_data_clear` from `src/pulse_data.c:22-35`. Drop the printers. |
| `include/c_util.h` | `MIN`, `MAX` | |
| `src/r_flow.c` | Reference only | We write our own small driver around these calls, modeled on `:196-300`, and `calc_rssi_snr` (`:35-64`) for SNR and carrier frequency. |

Facts that shape the bridge:

- Limits: at most 1200 pulses per package, a pulse under 10 samples is noise, a package ends after a gap over 10 ms that is 10x the widest pulse, or any gap over 100 ms (`include/pulse_data.h:21-27`, `pulse_detect.c:447-450`).
- So the trailing gap is at most 100 ms. Design rule 1 says cap or drop it. ir_rf_proxy frames come from remote_receiver. I propose dropping it.
- `pulse_data_t` holds two 1200-int arrays, about 9.6 KB. Two of them (OOK and FSK) must live in static or PSRAM memory, never on a task stack. Running rtl_433's `-A` analyzer blew the decode stack for this reason (README `:145`).
- A carrier estimate exists for OOK: the FM value is averaged over each pulse (`pulse_detect.c:365`) and becomes `freq1_hz` (`r_flow.c:42-46`). It needs FM demod switched on.
- SNR comes from `calc_rssi_snr` (`r_flow.c:35-64`). That is the `min_snr` filter input.
- Master `f35183aa` switches to `envelope_detect_nolut` (`r_flow.c:157`) and widens a loop index. Same values. No reason to move off `bd90731`.

## Test captures

From `rtl_433_tests` at `1aa8c29`: 3,620 `.cu8` files.

- 250 kS/s: 1,046 files, 759 of them at 433.9x MHz. Plenty of OOK.
- 2048 kS/s: only 10 files. One is OOK: `tests/Neptune-R900/01/g001_912M_2048k.cu8` (`neptune_r900` is `OOK_PULSE_PCM`). The rest are FSK (elero, elster, olympia). They make good **FSK-reject** tests.
- Plan: real 250k OOK captures, the one real 2048k OOK capture, real 2048k FSK captures for rejection, plus synthetic 2048k OOK files generated from known timings on the host.

## Hardware: what is confirmed so far

`docs.m5stack.com` and `mouser.lt` are blocked in this session, so I have not read the schematic. These come from M5Stack's own demo firmware and ESP-IDF.

| Item | Status | Source |
| --- | --- | --- |
| Two SKUs | **Unit PoE-P4** (U213, pre-v3 silicon, IDF 5.4.2) and **Unit PoE-P4X** (rev 3.x silicon, IDF 6.1+). Not binary compatible. | M5 demo `README.md` |
| Silicon flag | ESPHome sets `CONFIG_ESP32P4_SELECTS_REV_LESS_V3` from `engineering_sample` (`esp32/__init__.py:3049-3058`). IDF 5.5.6 and 6.1 both have it. Pre-v3 runs at 360 MHz (`:561-562`). 400 MHz is marked experimental (`:572-576`). | ESPHome, IDF |
| PHY | IP101, address 1. MDC GPIO31, MDIO GPIO52. | M5 `sdkconfig:717-725` |
| GPIO51 | **PHY reset**, not a power enable. | M5 `sdkconfig:724`, `ethernet_init.c:76` |
| RMII and clock | The demo overrides only the PHY address, reset, MDC and MDIO (`ethernet_init.c:75-81`). The rest is IDF's P4 default: clock **external in on GPIO50**, TX_EN 49, TXD0 34, TXD1 35, CRS_DV 28, RXD0 29, RXD1 30. | IDF `esp_eth_mac_esp.h` v5.5.1 `:261-301`, v6.1 `:232-260` |
| ESPHome mapping | `power_pin` is passed to the PHY as its reset GPIO (`ethernet/ethernet_component_esp32.cpp:318`). So esphome-rtl433's Waveshare block (`power_pin: GPIO51`, `clk: CLK_EXT_IN GPIO50`, `phy_addr: 1`) matches the M5 exactly. | ESPHome |
| RGB LED | Discrete RGB on PWM: R GPIO17, G GPIO15, B GPIO16. Not a WS2812. | M5 `main/app_led/led_ctrl.h:7-9` |
| Button | GPIO45. | M5 `main/app_gpio/user_key.h:13` |
| Flash size | Not confirmed. The demo's `sdkconfig` says 2 MB, but that is a demo default. Check with `esptool flash_id`. | |
| Host-port VBUS switch | Unknown. Not in the demo. Needs the schematic or a meter. | |
| PoE draw, dongle draw, temperature | Phase 1 measurements. | |

## Decisions for the owner

These block Phase 3, not Phase 1.

**D1. One range per entity.** A puck on 433.92 MHz at 250 kS/s and 915 MHz at 2048 kS/s hears 433.795-434.045 MHz and 913.976-916.024 MHz. One ESPHome entity can only say one range.

- (a) **One receiver entity per band**, on the same device. Each range is honest. Each burst carries the key of its band's entity. Consumer setup flows find the right one through `async_get_receivers`. HA's docs already say an adapter shows each receiver as its own entity. *Recommended.* It changes the acceptance line "one Radio frequency receiver entity" to one per band.
- (b) One entity spanning 433.795-916.024 MHz. Simple, but it claims to hear 868 MHz and everything in between. Consumers would pick it wrongly.
- (c) Ask ESPHome, aioesphomeapi and HA to support several ranges per entity. API change. Needs your approval, and is public.

**D2. No frequency in the received signal.** Design rule 2 cannot reach HA. HA always gets `frequency=None`.

- (a) Accept it. Keep the band in the entity (D1a makes that exact). Expose the last carrier offset as a diagnostic sensor, disabled by default. *Recommended.*
- (b) Propose a `frequency` field on `InfraredRFReceiveEvent` upstream. API change. Needs your approval.

**D3. The Radio frequency panel ignores receivers.** The acceptance check cannot pass on stock HA.

- (a) Replace it for now with: the receiver entity is on the device page, `radio_frequency/list` returns it under `receivers`, and its state timestamp moves when a remote is pressed. *Recommended.*
- (b) File a frontend issue or PR to list receivers. Public. Needs your approval.

Not a decision, but worth knowing: every forwarded burst writes a state row to HA's recorder. I plan a low default rate cap (about 2 per second) and a 1-second dedupe window, to tune on real traffic.

## Not checked

- The M5 schematic and docs page (blocked host). Pins above come from vendor firmware instead.
- The three comments on rtl-433-hass/rtl_433#296. I read the opening post and the esphome-rtl433 README it points to.
- That ESPHome 2026.10.2 builds with ESP-IDF 6.1. esphome-rtl433's compile test uses 6.1.0 (`tests/p4-wifi6-poe-eth.yaml`), but against an unpinned ESPHome. The first Phase 1 compile settles it.

## Links

- [home-assistant/core#189484](https://github.com/home-assistant/core/pull/189484): ESPHome RF receiver in HA.
- [home-assistant/home-assistant.io#49508](https://github.com/home-assistant/home-assistant.io/pull/49508) and [#50031](https://github.com/home-assistant/home-assistant.io/pull/50031): the matching user docs.
- [hardcoreerik/esp-rtl-sdr#26](https://github.com/hardcoreerik/esp-rtl-sdr/pull/26) (closed unmerged), then [#49](https://github.com/hardcoreerik/esp-rtl-sdr/pull/49) and [#50](https://github.com/hardcoreerik/esp-rtl-sdr/pull/50) (merged).
- [davidcoulson/esp-rtl-sdr](https://github.com/davidcoulson/esp-rtl-sdr), tag `v0.9.4-dc.1`: the driver fork to pin.
- [rtl-433-hass/rtl_433#296](https://github.com/rtl-433-hass/rtl_433/issues/296): the esphome-rtl433 status thread.
- [m5stack/M5Unit-PoE-P4-UserDemo](https://github.com/m5stack/M5Unit-PoE-P4-UserDemo): vendor firmware used for the pin check.
