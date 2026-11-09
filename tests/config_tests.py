#!/usr/bin/env python3
"""Config tests for the rtl_sdr ESPHome component. No hardware and no compiler needed.

Valid cases run `esphome compile --only-generate` and check what the component generated: the setter calls in
main.cpp, the sdkconfig options and the pinned IDF components. Invalid cases run `esphome config` and check the
error text. Use the pinned ESPHome (see docs/API-STATUS.md):

    pip install esphome==2026.10.2
    python3 tests/config_tests.py            # every case
    python3 tests/config_tests.py -k dwell   # cases whose name contains "dwell"
"""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
# The esphome next to this Python (a venv), else the one on PATH; ESPHOME=/path/to/esphome overrides both
_VENV_ESPHOME = pathlib.Path(sys.executable).parent / "esphome"
ESPHOME = os.environ.get("ESPHOME") or (str(_VENV_ESPHOME) if _VENV_ESPHOME.exists() else shutil.which("esphome") or "esphome")

DRIVER_SHA = "78d420d188971c941e211904e214678073e16d8d"

BASE = {
    "esphome": {"name": "rfpuck-cfgtest"},
    "esp32": {
        "variant": "esp32p4",
        "engineering_sample": True,
        "flash_size": "16MB",
        "framework": {"type": "esp-idf", "version": "6.1.0"},
    },
    "psram": {"mode": "hex", "speed": "200MHz"},
    "logger": None,
    "api": None,
    "ethernet": {
        "type": "IP101",
        "mdc_pin": "GPIO31",
        "mdio_pin": "GPIO52",
        "power_pin": "GPIO51",
        "clk": {"mode": "CLK_EXT_IN", "pin": "GPIO50"},
        "phy_addr": 1,
    },
    "external_components": [
        {"source": {"type": "local", "path": str(REPO / "components")}, "components": ["rtl_sdr"]}
    ],
    "rtl_sdr": {"bands": [{"frequency": "433.92MHz", "sample_rate": "250k"}]},
}

TWO_BANDS = [
    {"frequency": "433.92MHz", "sample_rate": "250k", "dwell": "30s"},
    {"frequency": "915MHz", "sample_rate": "2048k", "dwell": "30s"},
]

STATUS_LIGHT = {
    "output": [
        {"platform": "ledc", "id": "led_r", "pin": "GPIO17", "inverted": True},
        {"platform": "ledc", "id": "led_g", "pin": "GPIO15", "inverted": True},
        {"platform": "ledc", "id": "led_b", "pin": "GPIO16", "inverted": True},
    ],
    "light": [
        {"platform": "rgb", "id": "status_rgb", "internal": True, "red": "led_r", "green": "led_g", "blue": "led_b"}
    ],
}


def with_hub(**hub):
    """A mutator that replaces keys in the rtl_sdr block."""

    def apply(config):
        config["rtl_sdr"].update(hub)

    return apply


def with_config(**top):
    def apply(config):
        for key, value in top.items():
            if value is None and key in config:
                del config[key]
            else:
                config[key] = value

    return apply


def on_esp32s3(config):
    """Same hub config on a chip without a high-speed USB host."""
    config["esp32"] = {"variant": "esp32s3", "framework": {"type": "esp-idf"}}
    for key in ("ethernet", "psram", "api"):  # no network on this test chip: api would need one
        config.pop(key)


def chain(*mutators):
    def apply(config):
        for mutator in mutators:
            mutator(config)

    return apply


# (name, mutator, checks). Checks for valid cases: ("code", text in main.cpp), ("no_code", text),
# ("sdkconfig", line), ("no_sdkconfig", line), ("component", text in idf_component.yml),
# ("define", "USE_..."), ("config", text in `esphome config` output). For invalid cases: ("error", text).
CASES = [
    (
        "one band, defaults",
        with_hub(),
        [
            ("code", "add_band(433920000, 250000, 0)"),
            ("code", "set_gain_tenth_db(-1)"),
            ("code", "set_usb_buffer(1048576)"),
            ("code", "set_block_ms(32)"),
            ("code", "set_block_count(8)"),
            ("code", "set_reader_priority(6)"),
            ("code", "set_dsp_priority(5)"),
            ("no_code", "set_dsp_core("),
            ("sdkconfig", "CONFIG_ESP_RTL_SDR_ESP_R820T2_IF_TABLE=y"),
            ("sdkconfig", "CONFIG_USB_HOST_CONTROL_TRANSFER_MAX_SIZE=1024"),
            ("no_sdkconfig", "CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS=y"),
            ("component", DRIVER_SHA),
            ("component", "git: https://github.com/davidcoulson/esp-rtl-sdr.git"),
            ("component", "espressif/usb:\n    version: 1.6.0"),
        ],
    ),
    (
        "two bands with dwell",
        with_hub(bands=TWO_BANDS),
        [
            ("code", "add_band(433920000, 250000, 30000)"),
            ("code", "add_band(915000000, 2048000, 30000)"),
        ],
    ),
    ("manual gain", with_hub(gain=40), [("code", "set_gain_tenth_db(400)")]),
    ("fractional gain", with_hub(gain=28.7), [("code", "set_gain_tenth_db(287)")]),
    ("explicit auto gain", with_hub(gain="AUTO"), [("code", "set_gain_tenth_db(-1)")]),
    ("ppm error", with_hub(ppm_error=-12), [("code", "set_ppm_error(-12)")]),
    (
        "IF mode vendor_fixed",
        with_hub(r820t2_if_mode="vendor_fixed"),
        [
            ("sdkconfig", "CONFIG_ESP_RTL_SDR_ESP_R820T2_IF_VENDOR_FIXED=y"),
            ("no_sdkconfig", "CONFIG_ESP_RTL_SDR_ESP_R820T2_IF_TABLE=y"),
        ],
    ),
    ("IF mode librtlsdr", with_hub(r820t2_if_mode="librtlsdr"), [("sdkconfig", "CONFIG_ESP_RTL_SDR_ESP_R820T2_IF_LIBRTLSDR=y")]),
    ("usb buffer in KB", with_hub(usb_buffer="512KB"), [("code", "set_usb_buffer(524288)")]),
    ("usb buffer driver default", with_hub(usb_buffer=0), [("code", "set_usb_buffer(0)")]),
    (
        "block tuning and cores",
        with_hub(block_time="64ms", blocks=16, dsp_core=1, reader_priority=7, dsp_priority=4),
        [
            ("code", "set_block_ms(64)"),
            ("code", "set_block_count(16)"),
            ("code", "set_dsp_core(1)"),
            ("code", "set_reader_priority(7)"),
            ("code", "set_dsp_priority(4)"),
        ],
    ),
    (
        "rate edges",
        with_hub(
            bands=[
                {"frequency": "433.92MHz", "sample_rate": 225001, "dwell": "5s"},
                {"frequency": "868.3MHz", "sample_rate": "300k", "dwell": "5s"},
                {"frequency": "915MHz", "sample_rate": 900001, "dwell": "5s"},
                {"frequency": "315MHz", "sample_rate": "2560k", "dwell": "1h"},
            ]
        ),
        [
            ("code", "add_band(433920000, 225001, 5000)"),
            ("code", "add_band(868300000, 300000, 5000)"),
            ("code", "add_band(915000000, 900001, 5000)"),
            ("code", "add_band(315000000, 2560000, 3600000)"),
        ],
    ),
    (
        "status light",
        chain(with_config(**STATUS_LIGHT), with_hub(status_light="status_rgb")),
        [("code", "set_status_light(status_rgb)"), ("define", "USE_RTL_SDR_STATUS_LIGHT")],
    ),
    (
        "cpu load sensors",
        with_hub(cpu_load_core0={"name": "CPU 0"}, cpu_load_core1={"name": "CPU 1"}),
        [
            ("code", "set_cpu_load_core0_sensor("),
            ("code", "set_cpu_load_core1_sensor("),
            ("define", "USE_RTL_SDR_CPU_LOAD"),
            ("sdkconfig", "CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS=y"),
        ],
    ),
    (
        "diagnostics are diagnostic, noisy ones disabled by default",
        with_hub(
            dongle={"name": "Dongle"},
            dropped_samples={"name": "Dropped"},
            usb_overruns={"name": "Overruns"},
            stream_restarts={"name": "Restarts"},
            band={"name": "Band"},
            samples={"name": "Samples"},
            effective_sample_rate={"name": "Rate"},
            heap_free={"name": "Heap"},
            psram_free={"name": "PSRAM"},
            reader_stack_free={"name": "Reader stack"},
            dsp_stack_free={"name": "DSP stack"},
        ),
        [
            ("code", "set_dongle_binary_sensor("),
            ("code", "set_band_text_sensor("),
            ("code", "set_dropped_samples_sensor("),
            ("code", "set_usb_overruns_sensor("),
            ("code", "set_stream_restarts_sensor("),
            ("code", "set_samples_sensor("),
            ("code", "set_effective_sample_rate_sensor("),
            ("code", "set_heap_free_sensor("),
            ("code", "set_psram_free_sensor("),
            ("code", "set_reader_stack_free_sensor("),
            ("code", "set_dsp_stack_free_sensor("),
            ("diag", ("dongle", False)),
            ("diag", ("dropped_samples", False)),
            ("diag", ("usb_overruns", False)),
            ("diag", ("stream_restarts", False)),
            ("diag", ("band", True)),
            ("diag", ("samples", True)),
            ("diag", ("effective_sample_rate", True)),
            ("diag", ("heap_free", True)),
            ("diag", ("psram_free", True)),
            ("diag", ("reader_stack_free", True)),
            ("diag", ("dsp_stack_free", True)),
        ],
    ),
    (
        "user can enable a noisy sensor",
        with_hub(samples={"name": "Samples", "disabled_by_default": False}),
        [("diag", ("samples", False))],
    ),
    # Invalid configs
    ("no bands", lambda c: c["rtl_sdr"].pop("bands"), [("error", "'bands' is a required option")]),
    ("empty band list", with_hub(bands=[]), [("error", "at least 1")]),
    (
        "five bands",
        with_hub(
            bands=[
                {"frequency": f"{f}MHz", "sample_rate": "250k", "dwell": "10s"} for f in (315, 345, 433.92, 868.3, 915)
            ]
        ),
        [("error", "at most 4")],
    ),
    ("rate in the gap", with_hub(bands=[{"frequency": "433.92MHz", "sample_rate": "500k"}]), [("error", "sample_rate must be")]),
    ("rate exactly 900k", with_hub(bands=[{"frequency": "433.92MHz", "sample_rate": "900k"}]), [("error", "aliases to 300k")]),
    ("rate exactly 225k", with_hub(bands=[{"frequency": "433.92MHz", "sample_rate": "225k"}]), [("error", "sample_rate must be")]),
    ("rate above 2.56M", with_hub(bands=[{"frequency": "433.92MHz", "sample_rate": "3200k"}]), [("error", "sample_rate must be")]),
    ("frequency too low", with_hub(bands=[{"frequency": "13.56MHz", "sample_rate": "250k"}]), [("error", "24 MHz to 1766 MHz")]),
    ("frequency too high", with_hub(bands=[{"frequency": "2.4GHz", "sample_rate": "250k"}]), [("error", "24 MHz to 1766 MHz")]),
    ("missing sample rate", with_hub(bands=[{"frequency": "433.92MHz"}]), [("error", "'sample_rate' is a required option")]),
    (
        "two bands, one without dwell",
        with_hub(bands=[TWO_BANDS[0], {"frequency": "915MHz", "sample_rate": "2048k"}]),
        [("error", "every band needs a dwell")],
    ),
    ("duplicate band", with_hub(bands=[TWO_BANDS[0], dict(TWO_BANDS[0])]), [("error", "listed twice")]),
    ("dwell too short", with_hub(bands=[dict(TWO_BANDS[0], dwell="2s"), TWO_BANDS[1]]), [("error", "dwell")]),
    ("gain too high", with_hub(gain=60), [("error", "gain")]),
    ("gain not a number", with_hub(gain="high"), [("error", "gain")]),
    ("bias tee is not an option", with_hub(bias_tee=True), [("error", "bias_tee")]),
    ("bad IF mode", with_hub(r820t2_if_mode="wide"), [("error", "r820t2_if_mode")]),
    ("usb buffer too small", with_hub(usb_buffer="16KB"), [("error", "usb_buffer")]),
    ("usb buffer garbage", with_hub(usb_buffer="lots"), [("error", "usb_buffer")]),
    ("too few blocks", with_hub(blocks=2), [("error", "blocks")]),
    ("block_time too short", with_hub(block_time="4ms"), [("error", "block_time")]),
    (
        "block too large at 2.56M",
        with_hub(bands=[{"frequency": "915MHz", "sample_rate": "2560k"}], block_time="64ms"),
        [("error", "the most is 262144")],
    ),
    ("dsp core out of range", with_hub(dsp_core=2), [("error", "dsp_core")]),
    ("ppm out of range", with_hub(ppm_error=500), [("error", "ppm_error")]),
    ("status light that does not exist", with_hub(status_light="nope"), [("error", "nope")]),
    ("no psram", with_config(psram=None), [("error", "rtl_sdr needs PSRAM")]),
    ("web_server not allowed", with_config(web_server={"port": 80}), [("error", "does not support web_server")]),
    ("not an ESP32-P4", lambda c: on_esp32s3(c), [("error", "needs an ESP32-P4")]),
]

EXAMPLES = [REPO / "examples" / "m5-unit-poe-p4.yaml"]


def write_secrets(directory: pathlib.Path) -> None:
    key = base64.b64encode(os.urandom(32)).decode()
    (directory / "secrets.yaml").write_text(f'rf_puck_api_key: "{key}"\n')


def run(args, cwd):
    proc = subprocess.run([ESPHOME, *args], cwd=cwd, capture_output=True, text=True, timeout=600)
    return proc.returncode, proc.stdout + proc.stderr


def diag_flags(output: str, key: str):
    """(entity_category, disabled_by_default) of one diagnostic in the `esphome config` dump."""
    text = output[output.index("rtl_sdr:") :]
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{key}:")
    category, disabled = None, None
    for line in lines[start + 1 :]:
        if not line.startswith("    "):
            break
        stripped = line.strip()
        if stripped.startswith("entity_category:"):
            category = stripped.split(":", 1)[1].strip()
        if stripped.startswith("disabled_by_default:"):
            disabled = stripped.split(":", 1)[1].strip() == "true"
    return category, disabled


def check_case(name, mutator, checks):
    config = copy.deepcopy(BASE)
    mutator(config)
    invalid = any(kind == "error" for kind, _ in checks)
    with tempfile.TemporaryDirectory(prefix="rtlsdr-cfg-") as tmp:
        tmp = pathlib.Path(tmp)
        yaml_path = tmp / "case.yaml"
        yaml_path.write_text(yaml.safe_dump(config, sort_keys=False))
        failures = []
        if invalid:
            code, out = run(["config", str(yaml_path)], tmp)
            if code == 0:
                failures.append("expected validation to fail, it passed")
            for kind, want in checks:
                if kind == "error" and want not in out:
                    failures.append(f"error text missing: {want!r}")
            return name, failures, out
        code, out = run(["config", str(yaml_path)], tmp)
        if code != 0:
            return name, ["validation failed"], out
        config_out = out
        need_generate = any(kind in ("code", "no_code", "sdkconfig", "no_sdkconfig", "component", "define") for kind, _ in checks)
        main_cpp = sdkconfig = components = defines = ""
        if need_generate:
            code, out = run(["compile", "--only-generate", str(yaml_path)], tmp)
            if code != 0:
                return name, ["code generation failed"], out
            build = tmp / ".esphome" / "build" / BASE["esphome"]["name"]
            main_cpp = (build / "src" / "main.cpp").read_text()
            sdkconfig = next(build.glob("sdkconfig.*.esphomeinternal")).read_text()
            components = (build / "src" / "idf_component.yml").read_text()
            defines_path = build / "src" / "esphome" / "core" / "defines.h"
            defines = defines_path.read_text() if defines_path.exists() else ""
        for kind, want in checks:
            if kind == "code" and want not in main_cpp:
                failures.append(f"main.cpp lacks {want!r}")
            elif kind == "no_code" and want in main_cpp:
                failures.append(f"main.cpp has {want!r}")
            elif kind == "sdkconfig" and want not in sdkconfig:
                failures.append(f"sdkconfig lacks {want!r}")
            elif kind == "no_sdkconfig" and want in sdkconfig:
                failures.append(f"sdkconfig has {want!r}")
            elif kind == "component" and want not in components:
                failures.append(f"idf_component.yml lacks {want!r}")
            elif kind == "define" and f"#define {want}" not in defines:
                failures.append(f"defines.h lacks {want!r}")
            elif kind == "diag":
                key, want_disabled = want
                category, disabled = diag_flags(config_out, key)
                if category != "diagnostic":
                    failures.append(f"{key}: entity_category {category!r}, want diagnostic")
                if disabled is not want_disabled:
                    failures.append(f"{key}: disabled_by_default {disabled!r}, want {want_disabled}")
        return name, failures, out


def top_level_block(output: str, key: str) -> str:
    """The lines under one top-level key of the `esphome config` dump."""
    lines = output.splitlines()
    start = lines.index(f"{key}:")
    block = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break
        block.append(line)
    return "\n".join(block)


def check_example(path: pathlib.Path):
    with tempfile.TemporaryDirectory(prefix="rtlsdr-ex-") as tmp:
        tmp = pathlib.Path(tmp)
        # Same layout as the repo, so the example's relative path to components/ still works
        shutil.copytree(REPO / "components", tmp / "components")
        (tmp / "examples").mkdir()
        shutil.copy(path, tmp / "examples" / path.name)
        write_secrets(tmp / "examples")
        code, out = run(["config", str(tmp / "examples" / path.name)], tmp)
        if code != 0:
            return f"example {path.name}", ["validation failed"], out
        # OTA uploads are encrypted with the api key, and there is no OTA password
        ota_block = top_level_block(out, "ota")
        failures = []
        if "encryption:" not in ota_block:
            failures.append("ota is not encrypted")
        if "password:" in ota_block:
            failures.append("ota still has a password")
        return f"example {path.name}", failures, out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-k", help="only cases whose name contains this")
    parser.add_argument("-j", type=int, default=os.cpu_count() or 2, help="parallel jobs")
    parser.add_argument("-v", action="store_true", help="print ESPHome output for failures")
    args = parser.parse_args()

    cases = [c for c in CASES if not args.k or args.k in c[0]]
    jobs = []
    with ThreadPoolExecutor(max_workers=args.j) as pool:
        for case in cases:
            jobs.append(pool.submit(check_case, *case))
        if not args.k or "example" in args.k:
            for example in EXAMPLES:
                jobs.append(pool.submit(check_example, example))
        results = [job.result() for job in jobs]

    failed = 0
    for name, failures, out in results:
        if failures:
            failed += 1
            print(f"FAIL  {name}")
            for failure in failures:
                print(f"      {failure}")
            if args.v:
                print("      " + "\n      ".join(out.strip().splitlines()[-25:]))
        else:
            print(f"ok    {name}")
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
