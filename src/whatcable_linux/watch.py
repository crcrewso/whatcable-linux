"""Watch successive system reports for connection changes.

A single report cannot show a cable that drops out for half a second and
comes back. This module folds a sequence of reports into change events
(a Type-C partner detaching, a USB device being re-enumerated with a new
device number, a Thunderbolt link renegotiating at a lower speed, a Chrome EC
port going to Disconnected) and keeps a tally of dropouts and degradations
per subject. It is pure: no sysfs, no clock, no I/O, so the GTK application
and the command line can both drive it and the tests can replay a session as
a plain list of reports.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Iterable, TextIO

from .chrome_ec import is_chrome_ec_port
from .models import AdvancedDevice, SystemReport, TypeCPort, UsbDevice
from .usb_sysfs import is_root_hub, usb_device_name

LINK_SOURCES = frozenset({"Thunderbolt", "USB4"})
LINK_FIELDS = ("tx_speed", "rx_speed", "tx_lanes", "rx_lanes", "generation", "link")

_LEADING_NUMBER = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)")


@dataclass(frozen=True)
class WatchEvent:
    kind: str
    subject: str
    detail: str
    dropout: bool = False
    degradation: bool = False


@dataclass
class WatchTally:
    polls: int = 0
    events: int = 0
    dropouts: dict[str, int] = field(default_factory=dict)
    degradations: dict[str, int] = field(default_factory=dict)

    def record(self, events: Iterable[WatchEvent]) -> None:
        for event in events:
            self.events += 1
            if event.dropout:
                self.dropouts[event.subject] = self.dropouts.get(event.subject, 0) + 1
            if event.degradation:
                self.degradations[event.subject] = self.degradations.get(event.subject, 0) + 1

    @property
    def total_dropouts(self) -> int:
        return sum(self.dropouts.values())

    @property
    def total_degradations(self) -> int:
        return sum(self.degradations.values())


class CableWatch:
    """Feed reports in order; get the events each one implies and a running tally."""

    def __init__(self) -> None:
        self.previous: SystemReport | None = None
        self.tally = WatchTally()

    def observe(self, report: SystemReport) -> list[WatchEvent]:
        self.tally.polls += 1
        events = [] if self.previous is None else diff_reports(self.previous, report)
        self.previous = report
        self.tally.record(events)
        return events


def diff_reports(previous: SystemReport, current: SystemReport) -> list[WatchEvent]:
    events: list[WatchEvent] = []
    events.extend(_diff_typec(previous.typec_ports, current.typec_ports))
    events.extend(_diff_usb(previous.usb_devices, current.usb_devices))
    events.extend(_diff_links(previous.advanced_devices, current.advanced_devices))
    events.extend(_diff_ec_ports(previous.advanced_devices, current.advanced_devices))
    return events


def baseline_counts(report: SystemReport) -> dict[str, int]:
    return {
        "typec_ports": len(report.typec_ports),
        "usb_devices": sum(1 for device in report.usb_devices if not is_root_hub(device)),
        "link_devices": sum(1 for device in report.advanced_devices if device.source in LINK_SOURCES),
        "ec_ports": sum(1 for device in report.advanced_devices if is_chrome_ec_port(device)),
    }


def format_event(event: WatchEvent, when: str) -> str:
    marker = " [dropout]" if event.dropout else " [degraded]" if event.degradation else ""
    return f"{when}  {event.kind:<16} {event.subject}: {event.detail}{marker}"


def summary_lines(tally: WatchTally, elapsed_seconds: float) -> list[str]:
    minutes, seconds = divmod(int(elapsed_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    lines = [
        f"Watched for {hours}:{minutes:02d}:{seconds:02d} across {tally.polls} polls: "
        f"{tally.total_dropouts} dropouts, {tally.total_degradations} degradations, {tally.events} events."
    ]
    if tally.dropouts:
        lines.append("  dropouts: " + ", ".join(f"{subject} ×{count}" for subject, count in sorted(tally.dropouts.items())))
    if tally.degradations:
        lines.append(
            "  degradations: "
            + ", ".join(f"{subject} ×{count}" for subject, count in sorted(tally.degradations.items()))
        )
    if not tally.dropouts and not tally.degradations:
        lines.append("  No dropouts or degradations seen. A cable that fails only when flexed needs flexing.")
    return lines


def run_watch(
    scan: Callable[[], SystemReport],
    *,
    interval: float,
    as_json: bool,
    out: TextIO,
    now: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
    polls: int | None = None,
) -> int:
    """Poll ``scan`` every ``interval`` seconds and print what changed.

    Runs until Ctrl+C, or for ``polls`` iterations when given (used by tests).
    A failed scan is reported and watching continues, because devices that
    vanish mid-read are a normal runtime state for this tool.
    """
    watch = CableWatch()
    started = now()
    count = 0
    try:
        while polls is None or count < polls:
            count += 1
            stamp = _stamp(now())
            try:
                report = scan()
            except Exception as exc:  # a scan failure is data to report, not a crash
                _emit(out, as_json, stamp, {"kind": "scan-error", "detail": str(exc)}, f"{stamp}  scan-error: {exc}")
            else:
                events = watch.observe(report)
                if count == 1:
                    counts = baseline_counts(report)
                    _emit(
                        out,
                        as_json,
                        stamp,
                        {"kind": "baseline", **counts},
                        f"{stamp}  watching {counts['typec_ports']} Type-C ports, {counts['usb_devices']} USB devices, "
                        f"{counts['link_devices']} Thunderbolt/USB4 entries, {counts['ec_ports']} EC ports. "
                        f"Polling every {interval:g}s; Ctrl+C stops and prints a summary.",
                    )
                for event in events:
                    _emit(out, as_json, stamp, _event_payload(event), format_event(event, stamp))
            out.flush()
            if polls is None or count < polls:
                sleep(interval)
    except KeyboardInterrupt:
        pass
    elapsed = (now() - started).total_seconds()
    if as_json:
        summary = {
            "kind": "summary",
            "time": _stamp(now()),
            "elapsed_seconds": round(elapsed, 3),
            "polls": watch.tally.polls,
            "events": watch.tally.events,
            "dropouts": watch.tally.dropouts,
            "degradations": watch.tally.degradations,
        }
        print(json.dumps(summary, sort_keys=True), file=out)
    else:
        for line in summary_lines(watch.tally, elapsed):
            print(line, file=out)
    out.flush()
    return 0


def _event_payload(event: WatchEvent) -> dict:
    payload = {key: value for key, value in asdict(event).items() if key != "kind"}
    return {"kind": "event", "event": event.kind, **payload}


def _emit(out: TextIO, as_json: bool, stamp: str, payload: dict, text: str) -> None:
    if as_json:
        print(json.dumps({"time": stamp, **payload}, sort_keys=True), file=out)
    else:
        print(text, file=out)


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.isoformat(timespec="seconds")


def _diff_typec(previous: list[TypeCPort], current: list[TypeCPort]) -> list[WatchEvent]:
    before = {port.name: port for port in previous}
    after = {port.name: port for port in current}
    events: list[WatchEvent] = []
    for name in sorted(before.keys() - after.keys()):
        events.append(WatchEvent("typec-port-gone", name, "port no longer listed", dropout=True))
    for name in sorted(after.keys() - before.keys()):
        events.append(WatchEvent("typec-port-new", name, "port appeared"))
    for name in sorted(before.keys() & after.keys()):
        old, new = before[name], after[name]
        was_connected = old.partner is not None or old.cable is not None
        is_connected = new.partner is not None or new.cable is not None
        if was_connected and not is_connected:
            events.append(WatchEvent("typec-detach", name, _typec_detail(old) + " detached", dropout=True))
        elif is_connected and not was_connected:
            events.append(WatchEvent("typec-attach", name, _typec_detail(new) + " attached"))
        else:
            if (old.partner is None) != (new.partner is None):
                verb = "partner attached" if new.partner is not None else "partner detached"
                events.append(WatchEvent("typec-partner", name, verb, dropout=new.partner is None))
            if (old.cable is None) != (new.cable is None):
                verb = "cable attached" if new.cable is not None else "cable removed"
                events.append(WatchEvent("typec-cable", name, verb, dropout=new.cable is None))
        for label, before_value, after_value in (
            ("data role", old.data_role, new.data_role),
            ("power role", old.power_role, new.power_role),
        ):
            if before_value != after_value and is_connected and was_connected:
                events.append(WatchEvent("typec-role", name, f"{label} {before_value} → {after_value}"))
    return events


def _typec_detail(port: TypeCPort) -> str:
    bits = []
    if port.partner is not None:
        bits.append("partner")
    if port.cable is not None:
        bits.append("cable")
    return " and ".join(bits) or "connection"


def _diff_usb(previous: list[UsbDevice], current: list[UsbDevice]) -> list[WatchEvent]:
    before = {device.name: device for device in previous if not is_root_hub(device)}
    after = {device.name: device for device in current if not is_root_hub(device)}
    events: list[WatchEvent] = []
    for name in sorted(before.keys() - after.keys()):
        events.append(WatchEvent("usb-remove", name, f"{usb_device_name(before[name])} removed", dropout=True))
    for name in sorted(after.keys() - before.keys()):
        device = after[name]
        events.append(WatchEvent("usb-add", name, f"{usb_device_name(device)} at {_speed_text(device.speed)}"))
    for name in sorted(before.keys() & after.keys()):
        old, new = before[name], after[name]
        if old.devnum != new.devnum:
            events.append(
                WatchEvent(
                    "usb-reenumerated",
                    name,
                    f"{usb_device_name(new)} device number {old.devnum} → {new.devnum}",
                    dropout=True,
                )
            )
        if old.speed != new.speed:
            events.append(
                WatchEvent(
                    "usb-speed",
                    name,
                    f"{usb_device_name(new)} {_speed_text(old.speed)} → {_speed_text(new.speed)}",
                    degradation=_decreased(old.speed, new.speed),
                )
            )
    return events


def _diff_links(previous: list[AdvancedDevice], current: list[AdvancedDevice]) -> list[WatchEvent]:
    before = {(d.source, d.name): d for d in previous if d.source in LINK_SOURCES}
    after = {(d.source, d.name): d for d in current if d.source in LINK_SOURCES}
    events: list[WatchEvent] = []
    for key in sorted(before.keys() - after.keys()):
        device = before[key]
        events.append(WatchEvent("link-remove", _link_subject(device), f"{device.summary or device.name} gone", dropout=True))
    for key in sorted(after.keys() - before.keys()):
        device = after[key]
        events.append(WatchEvent("link-add", _link_subject(device), device.summary or device.name))
    for key in sorted(before.keys() & after.keys()):
        old, new = before[key], after[key]
        changes = []
        degraded = False
        for field_name in LINK_FIELDS:
            old_value, new_value = old.properties.get(field_name), new.properties.get(field_name)
            if old_value != new_value:
                changes.append(f"{field_name} {old_value} → {new_value}")
                degraded = degraded or _decreased(old_value, new_value)
        if changes:
            events.append(WatchEvent("link-change", _link_subject(new), ", ".join(changes), degradation=degraded))
    return events


def _link_subject(device: AdvancedDevice) -> str:
    return f"{device.source.lower()} {device.name}"


def _diff_ec_ports(previous: list[AdvancedDevice], current: list[AdvancedDevice]) -> list[WatchEvent]:
    before = {d.name: d for d in previous if is_chrome_ec_port(d)}
    after = {d.name: d for d in current if is_chrome_ec_port(d)}
    events: list[WatchEvent] = []
    for name in sorted(before.keys() & after.keys()):
        old, new = before[name], after[name]
        subject = f"ec {name} ({new.properties.get('location', name)})"
        old_role, new_role = old.properties.get("role"), new.properties.get("role")
        if old_role != new_role:
            events.append(
                WatchEvent("ec-role", subject, f"{old_role} → {new_role}", dropout=new_role == "Disconnected")
            )
        old_charging, new_charging = old.properties.get("charging_type"), new.properties.get("charging_type")
        if old_charging != new_charging:
            events.append(WatchEvent("ec-charging", subject, f"charging type {old_charging} → {new_charging}"))
    return events


def _speed_text(speed: str | None) -> str:
    if speed is None:
        return "unknown speed"
    number = _leading_number(speed)
    if number is None:
        return speed
    return f"{number / 1000:g} Gbps" if number >= 1000 else f"{number:g} Mbps"


def _decreased(before: str | None, after: str | None) -> bool:
    old_number, new_number = _leading_number(before), _leading_number(after)
    if old_number is None or new_number is None:
        return False
    return new_number < old_number


def _leading_number(value: str | None) -> float | None:
    if value is None:
        return None
    match = _LEADING_NUMBER.match(value)
    return float(match.group(1)) if match else None
