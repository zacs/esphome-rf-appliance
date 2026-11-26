"""rtl_sdr: an RTL-SDR dongle on the ESP32-P4 USB host port.

This is the hub. It owns the dongle through the esp_rtl_sdr driver, hops between the configured
bands, and hands blocks of CU8 samples from a USB reader task to a DSP task. Phase 1 counts the
samples. Later phases add the OOK demodulator and the radio_frequency receiver platform.
"""

import os

import esphome.codegen as cg
from esphome.components import binary_sensor, esp32, light, sensor, text_sensor
from esphome.components.esp32 import (
    VARIANT_ESP32P4,
    add_idf_component,
    add_idf_sdkconfig_option,
    idf_version,
)
import esphome.config_validation as cv
from esphome.const import (
    CONF_DISABLED_BY_DEFAULT,
    CONF_FREQUENCY,
    CONF_GAIN,
    CONF_ID,
    CONF_SAMPLE_RATE,
    DEVICE_CLASS_CONNECTIVITY,
    ENTITY_CATEGORY_DIAGNOSTIC,
    STATE_CLASS_MEASUREMENT,
    STATE_CLASS_TOTAL_INCREASING,
    UNIT_PERCENT,
    Framework,
)
from esphome.core import CORE
import esphome.final_validate as fv

CODEOWNERS = ["@zacs"]
AUTO_LOAD = ["sensor", "text_sensor", "binary_sensor"]
MULTI_CONF = False

# The driver. davidcoulson/esp-rtl-sdr v0.9.4-dc.1: upstream v0.9.4 plus unplug and fault recovery and the
# R820T2 IF-filter Kconfig choice. Pinned by commit, not tag. See docs/API-STATUS.md.
ESP_RTL_SDR_REPO = "https://github.com/davidcoulson/esp-rtl-sdr.git"
ESP_RTL_SDR_REF = "78d420d188971c941e211904e214678073e16d8d"
# esp_rtl_sdr's idf_component.yml asks for espressif/usb 1.6.0 on ESP-IDF 6 and later
USB_REF = "1.6.0"

# esp_rtl_sdr Kconfig choice: how R820T2 dongles (NooElec SMArt v5, Blog V3) set the IF filter and IF
# for the sample rate. "table" is what made 915 MHz work on the NooElec in esphome-rtl433.
R820T2_IF_MODES = {
    "vendor_fixed": "CONFIG_ESP_RTL_SDR_ESP_R820T2_IF_VENDOR_FIXED",
    "table": "CONFIG_ESP_RTL_SDR_ESP_R820T2_IF_TABLE",
    "librtlsdr": "CONFIG_ESP_RTL_SDR_ESP_R820T2_IF_LIBRTLSDR",
}

# Driver limits (include/esp_rtl_sdr.h at the pinned commit)
FREQ_MIN_HZ = 24_000_000  # R820T2 floor; the driver's own floor is lower but needs an upconverter
FREQ_MAX_HZ = 1_766_000_000
RATE_LOW_MIN, RATE_LOW_MAX = 225_001, 300_000
RATE_HIGH_MIN, RATE_HIGH_MAX = 900_001, 2_560_000  # 2.56 MS/s is the vendor's stable ceiling
MAX_BANDS = 4
MAX_BLOCK_BYTES = 256 * 1024

rtl_sdr_ns = cg.esphome_ns.namespace("rtl_sdr")
RtlSdrHub = rtl_sdr_ns.class_("RtlSdrHub", cg.PollingComponent)

CONF_BANDS = "bands"
CONF_DWELL = "dwell"
CONF_PPM_ERROR = "ppm_error"
CONF_R820T2_IF_MODE = "r820t2_if_mode"
CONF_USB_BUFFER = "usb_buffer"
CONF_BLOCK_TIME = "block_time"
CONF_BLOCKS = "blocks"
CONF_DSP_CORE = "dsp_core"
CONF_READER_PRIORITY = "reader_priority"
CONF_DSP_PRIORITY = "dsp_priority"
CONF_STATUS_LIGHT = "status_light"

CONF_DONGLE = "dongle"
CONF_BAND = "band"
CONF_SAMPLES = "samples"
CONF_EFFECTIVE_SAMPLE_RATE = "effective_sample_rate"
CONF_DROPPED_SAMPLES = "dropped_samples"
CONF_USB_OVERRUNS = "usb_overruns"
CONF_STREAM_RESTARTS = "stream_restarts"
CONF_HEAP_FREE = "heap_free"
CONF_PSRAM_FREE = "psram_free"
CONF_READER_STACK_FREE = "reader_stack_free"
CONF_DSP_STACK_FREE = "dsp_stack_free"
CONF_CPU_LOAD_CORE0 = "cpu_load_core0"
CONF_CPU_LOAD_CORE1 = "cpu_load_core1"


def _bytes(value):
    """1048576, "512KB", "1MB" (binary multiples, like the driver's docs)."""
    if isinstance(value, str):
        v = value.strip().upper().replace(" ", "")
        for suffix, mult in (("MB", 1 << 20), ("KB", 1 << 10), ("B", 1)):
            if v.endswith(suffix):
                try:
                    return int(float(v[: -len(suffix)]) * mult)
                except ValueError as err:
                    raise cv.Invalid(f"expected a size like 512KB or 1MB, got '{value}'") from err
    return cv.int_(value)


def _sample_rate(value):
    """250k, 2048k, 2.048MHz or 250000. Must be one the RTL2832U resampler can program."""
    rate = int(round(cv.frequency(value)))
    if not (RATE_LOW_MIN <= rate <= RATE_LOW_MAX or RATE_HIGH_MIN <= rate <= RATE_HIGH_MAX):
        raise cv.Invalid(
            "sample_rate must be 225.001k-300k or 900.001k-2560k S/s "
            "(the RTL2832U cannot do 300k-900k, and exactly 900k aliases to 300k)"
        )
    return rate


def _band_frequency(value):
    freq = int(round(cv.frequency(value)))
    if not FREQ_MIN_HZ <= freq <= FREQ_MAX_HZ:
        raise cv.Invalid("frequency must be 24 MHz to 1766 MHz (the R820T2 tuner's range)")
    return freq


def _gain(value):
    """auto, or a tuner gain in dB."""
    if isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    return cv.float_range(min=0.0, max=50.0)(cv.float_(value))


BAND_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_FREQUENCY): _band_frequency,
        cv.Required(CONF_SAMPLE_RATE): _sample_rate,
        # Time on this band before hopping to the next. Required when there is more than one band.
        cv.Optional(CONF_DWELL): cv.All(
            cv.positive_time_period_milliseconds,
            cv.Range(min=cv.TimePeriod(seconds=5), max=cv.TimePeriod(hours=1)),
        ),
    }
)


def _validate_bands(bands):
    seen = set()
    for band in bands:
        if band[CONF_FREQUENCY] in seen:
            raise cv.Invalid(f"band {band[CONF_FREQUENCY]} Hz is listed twice")
        seen.add(band[CONF_FREQUENCY])
    if len(bands) > 1:
        for index, band in enumerate(bands):
            if CONF_DWELL not in band:
                raise cv.Invalid(
                    "every band needs a dwell when there is more than one band",
                    path=[index, CONF_DWELL],
                )
    return bands


def _diag(*, enabled=True, **kwargs):
    """A diagnostic sensor schema. Noisy ones are disabled by default (they still reach the API)."""
    schema = sensor.sensor_schema(entity_category=ENTITY_CATEGORY_DIAGNOSTIC, **kwargs)
    if not enabled:
        schema = schema.extend({cv.Optional(CONF_DISABLED_BY_DEFAULT, default=True): cv.boolean})
    return schema


def _only_p4_idf(config):
    if esp32.get_esp32_variant() != VARIANT_ESP32P4:
        raise cv.Invalid("rtl_sdr needs an ESP32-P4: the dongle needs its high-speed USB host port")
    if CORE.target_framework != Framework.ESP_IDF:
        raise cv.Invalid("rtl_sdr needs the esp-idf framework")
    return config


def _validate_blocks(config):
    for index, band in enumerate(config[CONF_BANDS]):
        block_bytes = band[CONF_SAMPLE_RATE] * config[CONF_BLOCK_TIME].total_milliseconds // 1000 * 2
        if block_bytes > MAX_BLOCK_BYTES:
            raise cv.Invalid(
                f"block_time {config[CONF_BLOCK_TIME].total_milliseconds} ms at "
                f"{band[CONF_SAMPLE_RATE]} S/s is {block_bytes} bytes; the most is {MAX_BLOCK_BYTES}",
                path=[CONF_BLOCK_TIME],
            )
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(RtlSdrHub),
            cv.Required(CONF_BANDS): cv.All(
                cv.ensure_list(BAND_SCHEMA),
                cv.Length(min=1, max=MAX_BANDS),
                _validate_bands,
            ),
            cv.Optional(CONF_GAIN, default="auto"): _gain,
            cv.Optional(CONF_PPM_ERROR, default=0): cv.int_range(min=-200, max=200),
            cv.Optional(CONF_R820T2_IF_MODE, default="table"): cv.one_of(*R820T2_IF_MODES, lower=True),
            # The driver's pull ring between USB and the reader task, in PSRAM. 0 = driver default (192-512 KB).
            cv.Optional(CONF_USB_BUFFER, default="1MB"): cv.All(
                _bytes, cv.Any(cv.int_range(min=0, max=0), cv.int_range(min=64 << 10, max=8 << 20))
            ),
            # One hand-off from the reader task to the DSP task holds this much signal
            cv.Optional(CONF_BLOCK_TIME, default="32ms"): cv.All(
                cv.positive_time_period_milliseconds,
                cv.Range(min=cv.TimePeriod(milliseconds=8), max=cv.TimePeriod(milliseconds=128)),
            ),
            # Blocks in the pool between the two tasks (PSRAM)
            cv.Optional(CONF_BLOCKS, default=8): cv.int_range(min=3, max=32),
            # Core for the DSP task. Default: the core the ESPHome loop is not on.
            cv.Optional(CONF_DSP_CORE): cv.int_range(min=0, max=1),
            cv.Optional(CONF_READER_PRIORITY, default=6): cv.int_range(min=1, max=17),
            cv.Optional(CONF_DSP_PRIORITY, default=5): cv.int_range(min=1, max=17),
            # Optional RGB light that shows the radio's state (keep it internal: true)
            cv.Optional(CONF_STATUS_LIGHT): cv.use_id(light.LightState),
            # Diagnostics. All optional, published every update_interval.
            cv.Optional(CONF_DONGLE): binary_sensor.binary_sensor_schema(
                device_class=DEVICE_CLASS_CONNECTIVITY,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
            # Current band. Changes on every hop, so disabled by default.
            cv.Optional(CONF_BAND): text_sensor.text_sensor_schema(
                icon="mdi:radio-tower",
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ).extend({cv.Optional(CONF_DISABLED_BY_DEFAULT, default=True): cv.boolean}),
            cv.Optional(CONF_DROPPED_SAMPLES): _diag(
                icon="mdi:package-variant-remove",
                accuracy_decimals=0,
                state_class=STATE_CLASS_TOTAL_INCREASING,
            ),
            cv.Optional(CONF_USB_OVERRUNS): _diag(
                icon="mdi:usb",
                accuracy_decimals=0,
                state_class=STATE_CLASS_TOTAL_INCREASING,
            ),
            cv.Optional(CONF_STREAM_RESTARTS): _diag(
                icon="mdi:restart",
                accuracy_decimals=0,
                state_class=STATE_CLASS_TOTAL_INCREASING,
            ),
            cv.Optional(CONF_SAMPLES): _diag(
                enabled=False,
                unit_of_measurement="MS",
                icon="mdi:counter",
                accuracy_decimals=1,
                state_class=STATE_CLASS_TOTAL_INCREASING,
            ),
            cv.Optional(CONF_EFFECTIVE_SAMPLE_RATE): _diag(
                enabled=False,
                unit_of_measurement="S/s",
                icon="mdi:sine-wave",
                accuracy_decimals=0,
                state_class=STATE_CLASS_MEASUREMENT,
            ),
            cv.Optional(CONF_HEAP_FREE): _diag(
                enabled=False,
                unit_of_measurement="B",
                icon="mdi:memory",
                accuracy_decimals=0,
                state_class=STATE_CLASS_MEASUREMENT,
            ),
            cv.Optional(CONF_PSRAM_FREE): _diag(
                enabled=False,
                unit_of_measurement="B",
                icon="mdi:memory",
                accuracy_decimals=0,
                state_class=STATE_CLASS_MEASUREMENT,
            ),
            cv.Optional(CONF_READER_STACK_FREE): _diag(
                enabled=False,
                unit_of_measurement="B",
                icon="mdi:layers-outline",
                accuracy_decimals=0,
                state_class=STATE_CLASS_MEASUREMENT,
            ),
            cv.Optional(CONF_DSP_STACK_FREE): _diag(
                enabled=False,
                unit_of_measurement="B",
                icon="mdi:layers-outline",
                accuracy_decimals=0,
                state_class=STATE_CLASS_MEASUREMENT,
            ),
            cv.Optional(CONF_CPU_LOAD_CORE0): _diag(
                enabled=False,
                unit_of_measurement=UNIT_PERCENT,
                icon="mdi:cpu-64-bit",
                accuracy_decimals=1,
                state_class=STATE_CLASS_MEASUREMENT,
            ),
            cv.Optional(CONF_CPU_LOAD_CORE1): _diag(
                enabled=False,
                unit_of_measurement=UNIT_PERCENT,
                icon="mdi:cpu-64-bit",
                accuracy_decimals=1,
                state_class=STATE_CLASS_MEASUREMENT,
            ),
        }
    ).extend(cv.polling_component_schema("60s")),
    cv.only_on_esp32,
    _only_p4_idf,
    _validate_blocks,
)


def _final_validate(config):
    full = fv.full_config.get()
    if "psram" not in full:
        raise cv.Invalid("rtl_sdr needs PSRAM for its sample buffers: add psram: (mode: hex, speed: 200MHz on the P4)")
    if "web_server" in full:
        raise cv.Invalid("rtl_sdr does not support web_server on this device: it competes with the radio for CPU")
    return config


FINAL_VALIDATE_SCHEMA = _final_validate


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    for band in config[CONF_BANDS]:
        dwell_ms = band[CONF_DWELL].total_milliseconds if CONF_DWELL in band else 0
        cg.add(var.add_band(band[CONF_FREQUENCY], band[CONF_SAMPLE_RATE], dwell_ms))
    gain = config[CONF_GAIN]
    cg.add(var.set_gain_tenth_db(-1 if gain == "auto" else int(round(gain * 10))))
    cg.add(var.set_ppm_error(config[CONF_PPM_ERROR]))
    cg.add(var.set_usb_buffer(config[CONF_USB_BUFFER]))
    cg.add(var.set_block_ms(config[CONF_BLOCK_TIME].total_milliseconds))
    cg.add(var.set_block_count(config[CONF_BLOCKS]))
    if CONF_DSP_CORE in config:
        cg.add(var.set_dsp_core(config[CONF_DSP_CORE]))
    cg.add(var.set_reader_priority(config[CONF_READER_PRIORITY]))
    cg.add(var.set_dsp_priority(config[CONF_DSP_PRIORITY]))

    if CONF_STATUS_LIGHT in config:
        light_state = await cg.get_variable(config[CONF_STATUS_LIGHT])
        cg.add(var.set_status_light(light_state))
        cg.add_define("USE_RTL_SDR_STATUS_LIGHT")

    if conf := config.get(CONF_DONGLE):
        cg.add(var.set_dongle_binary_sensor(await binary_sensor.new_binary_sensor(conf)))
    if conf := config.get(CONF_BAND):
        cg.add(var.set_band_text_sensor(await text_sensor.new_text_sensor(conf)))
    for key in (
        CONF_DROPPED_SAMPLES,
        CONF_USB_OVERRUNS,
        CONF_STREAM_RESTARTS,
        CONF_SAMPLES,
        CONF_EFFECTIVE_SAMPLE_RATE,
        CONF_HEAP_FREE,
        CONF_PSRAM_FREE,
        CONF_READER_STACK_FREE,
        CONF_DSP_STACK_FREE,
        CONF_CPU_LOAD_CORE0,
        CONF_CPU_LOAD_CORE1,
    ):
        if conf := config.get(key):
            sens = await sensor.new_sensor(conf)
            cg.add(getattr(var, f"set_{key}_sensor")(sens))
    if CONF_CPU_LOAD_CORE0 in config or CONF_CPU_LOAD_CORE1 in config:
        # CPU load = 100 % minus each core's idle-task share of the run-time counter
        add_idf_sdkconfig_option("CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS", True)
        cg.add_define("USE_RTL_SDR_CPU_LOAD")

    # The driver, pinned. ESP_RTL_SDR_LOCAL_PATH=/path/to/esp_rtl_sdr builds a local checkout instead
    # (the directory must be named esp_rtl_sdr: ESP-IDF names components after their directory).
    if idf_version() >= cv.Version(6, 0, 0):
        add_idf_component(name="espressif/usb", ref=USB_REF)
    if local := os.environ.get("ESP_RTL_SDR_LOCAL_PATH"):
        add_idf_component(name="esp_rtl_sdr", path=local)
    else:
        add_idf_component(name="esp_rtl_sdr", repo=ESP_RTL_SDR_REPO, ref=ESP_RTL_SDR_REF)
    add_idf_sdkconfig_option(R820T2_IF_MODES[config[CONF_R820T2_IF_MODE]], True)
    # The driver's descriptor and tuner control transfers (its P4 reference config uses 1024)
    add_idf_sdkconfig_option("CONFIG_USB_HOST_CONTROL_TRANSFER_MAX_SIZE", 1024)
