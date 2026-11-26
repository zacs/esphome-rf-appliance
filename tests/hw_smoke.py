#!/usr/bin/env python3
"""Hardware smoke test and soak monitor for the RF puck (Phase 1).

Connects to a running puck over the ESPHome native API, the same way Home Assistant does, and watches the
rtl_sdr hub's diagnostics. It never changes anything on the device.

Smoke test (default, 5 minutes):
    python3 tests/hw_smoke.py rf-puck.local --key <api key>

Phase 1 exit soak (24 hours, writes every reading to a CSV):
    python3 tests/hw_smoke.py rf-puck.local --key <api key> --duration 24h --csv soak.csv

The API key can also come from RF_PUCK_API_KEY. Bands default to the example's
(433.92 MHz at 250 kS/s, 915 MHz at 2048 kS/s); pass --band FREQ_MHZ:RATE once per band otherwise.

Checks:
  - every sensor, binary sensor and text sensor is a diagnostic (Phase 1 has no other entities)
  - the dongle is connected and the stream runs on every configured band
  - each sample-rate reading is within 2% of a configured band's rate
  - dropped samples, USB overruns and stream restarts do not move
  - the device does not reboot (uptime never goes back) and the connection does not drop
  - free internal heap stays flat (end within 2% of start; soak only)
Exit code 0 on pass, 1 on fail. Requires: pip install aioesphomeapi
"""

import argparse
import asyncio
import csv
import os
import re
import sys
import time

from aioesphomeapi import (
    APIClient,
    APIConnectionError,
    BinarySensorInfo,
    BinarySensorState,
    EntityCategory,
    SensorInfo,
    SensorState,
    TextSensorInfo,
    TextSensorState,
)

# object_id -> role. Names in examples/m5-unit-poe-p4.yaml.
ROLES = {
    "dongle": "dongle",
    "dropped_samples": "dropped",
    "usb_overruns": "overruns",
    "stream_restarts": "restarts",
    "band": "band",
    "samples": "samples",
    "effective_sample_rate": "rate",
    "heap_free": "heap",
    "psram_free": "psram",
    "reader_stack_free": "reader_stack",
    "dsp_stack_free": "dsp_stack",
    "cpu_load_core_0": "cpu0",
    "cpu_load_core_1": "cpu1",
    "chip_temperature": "temp",
    "uptime": "uptime",
}
REQUIRED = ["dongle", "dropped", "overruns", "restarts", "band", "rate", "heap"]


def parse_duration(text: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"bad duration {text!r}: use e.g. 300s, 5m, 24h, 3d")
    value, unit = float(match.group(1)), match.group(2) or "s"
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def parse_band(text: str):
    try:
        freq, rate = text.split(":")
        return float(freq), int(float(rate))
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"bad band {text!r}: use FREQ_MHZ:RATE, e.g. 433.92:250000") from err


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class Monitor:
    def __init__(self, args):
        self.args = args
        self.roles = {}  # key -> role
        self.first = {}  # role -> first value
        self.last = {}  # role -> last value
        self.min = {}
        self.max = {}
        self.bands_seen = set()
        self.rate_failures = []
        self.reboots = 0
        self.disconnects = 0
        self.problems = []
        self.csv_writer = None
        self.started = time.monotonic()

    def classify(self, entities):
        missing_category = []
        for entity in entities:
            if not isinstance(entity, (SensorInfo, BinarySensorInfo, TextSensorInfo)):
                continue
            object_id = entity.object_id or slug(entity.name)
            if entity.entity_category != EntityCategory.DIAGNOSTIC:
                missing_category.append(entity.name)
            role = ROLES.get(object_id) or ROLES.get(slug(entity.name))
            if role:
                self.roles[entity.key] = role
        if missing_category:
            self.problems.append(f"not diagnostic: {', '.join(missing_category)}")
        found = set(self.roles.values())
        absent = [role for role in REQUIRED if role not in found]
        if absent:
            self.problems.append(f"diagnostics missing from the device: {', '.join(absent)}")

    def on_state(self, state):
        role = self.roles.get(state.key)
        if role is None or getattr(state, "missing_state", False):
            return
        if isinstance(state, (SensorState, BinarySensorState, TextSensorState)):
            value = state.state
        else:
            return
        elapsed = time.monotonic() - self.started
        if self.csv_writer:
            self.csv_writer.writerow([f"{elapsed:.1f}", role, value])
        if role == "uptime" and isinstance(value, float) and role in self.last and value + 1 < self.last[role]:
            self.reboots += 1
            print(f"[{elapsed:8.0f} s] REBOOT: uptime went from {self.last[role]:.0f} s to {value:.0f} s")
        if role == "band":
            self.bands_seen.add(value)
        if role == "rate" and isinstance(value, float) and value > 0:
            if not any(abs(value - rate) <= 0.02 * rate for _, rate in self.args.band):
                self.rate_failures.append(value)
        if role in ("dropped", "overruns", "restarts") and role in self.last and value != self.last[role]:
            print(f"[{elapsed:8.0f} s] {role} moved: {self.last[role]} -> {value}")
        self.first.setdefault(role, value)
        self.last[role] = value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.min[role] = min(self.min.get(role, value), value)
            self.max[role] = max(self.max.get(role, value), value)

    def verdict(self, soak: bool):
        problems = list(self.problems)
        if self.last.get("dongle") is not True:
            problems.append("dongle not connected at the end")
        want_bands = {f"{freq:.3f} MHz" for freq, _ in self.args.band}
        if len(self.args.band) > 1 and not want_bands <= self.bands_seen:
            problems.append(f"bands seen {sorted(self.bands_seen)}, want {sorted(want_bands)}")
        if "rate" not in self.last:
            problems.append("no sample-rate reading")
        if self.rate_failures:
            problems.append(f"sample rate off every band by > 2%: {self.rate_failures[:5]}")
        for role, label in (("dropped", "dropped samples"), ("overruns", "USB overruns"), ("restarts", "stream restarts")):
            if role in self.last and self.last[role] != self.first[role]:
                problems.append(f"{label} moved: {self.first[role]:.0f} -> {self.last[role]:.0f}")
            if role in self.last and self.last[role] != 0:
                problems.append(f"{label} not zero: {self.last[role]:.0f}")
        if self.reboots:
            problems.append(f"{self.reboots} reboot(s)")
        if self.disconnects:
            problems.append(f"API connection dropped {self.disconnects} time(s)")
        if soak and "heap" in self.first and self.last["heap"] < 0.98 * self.first["heap"]:
            problems.append(f"internal heap fell: {self.first['heap']:.0f} -> {self.last['heap']:.0f} B")
        return problems

    def report(self):
        print("\nReadings (first / last / min / max):")
        for role in sorted(self.last):
            first, last = self.first[role], self.last[role]
            if isinstance(last, (int, float)) and not isinstance(last, bool):
                print(f"  {role:14} {first:>14.1f} {last:>14.1f} {self.min[role]:>14.1f} {self.max[role]:>14.1f}")
            else:
                print(f"  {role:14} {first!s:>14} {last!s:>14}")
        print(f"  bands seen: {', '.join(sorted(self.bands_seen)) or 'none'}")


async def run(args) -> int:
    monitor = Monitor(args)
    csv_file = None
    if args.csv:
        csv_file = open(args.csv, "w", newline="")
        monitor.csv_writer = csv.writer(csv_file)
        monitor.csv_writer.writerow(["elapsed_s", "role", "value"])

    deadline = time.monotonic() + args.duration
    stopped = asyncio.Event()

    async def on_stop(expected: bool) -> None:
        if not expected:
            monitor.disconnects += 1
            print(f"[{time.monotonic() - monitor.started:8.0f} s] API connection lost; reconnecting")
        stopped.set()

    client = APIClient(args.host, args.port, None, noise_psk=args.key, client_info="rf-puck-hw-smoke")
    classified = False
    try:
        while time.monotonic() < deadline:
            stopped.clear()
            try:
                await client.connect(on_stop=on_stop, login=True)
            except APIConnectionError as err:
                print(f"connect failed: {err}; retrying in 10 s")
                await asyncio.sleep(10)
                continue
            if not classified:
                info = await client.device_info()
                print(f"Connected: {info.name} ({info.model}), ESPHome {info.esphome_version}, compiled {info.compilation_time}")
                entities, _ = await client.list_entities_services()
                monitor.classify(entities)
                classified = True
            client.subscribe_states(monitor.on_state)
            try:
                await asyncio.wait_for(stopped.wait(), timeout=max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
        await client.disconnect()
    finally:
        if csv_file:
            csv_file.close()

    monitor.report()
    problems = monitor.verdict(soak=args.duration >= 3600)
    if problems:
        print("\nFAIL")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nPASS")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", help="device address, e.g. rf-puck.local or an IP")
    parser.add_argument("--port", type=int, default=6053)
    parser.add_argument("--key", default=os.environ.get("RF_PUCK_API_KEY"), help="API encryption key")
    parser.add_argument("--duration", type=parse_duration, default=parse_duration("5m"), help="e.g. 5m, 24h")
    parser.add_argument("--band", type=parse_band, action="append", help="FREQ_MHZ:RATE, once per band")
    parser.add_argument("--csv", help="write every reading here")
    args = parser.parse_args()
    if not args.key:
        parser.error("the API encryption key is required (--key or RF_PUCK_API_KEY)")
    if not args.band:
        args.band = [(433.92, 250000), (915.0, 2048000)]
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
