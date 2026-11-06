import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from whatcable_linux.models import AdvancedDevice, SystemReport, TypeCCable, TypeCPartner, TypeCPort, UsbDevice
from whatcable_linux.watch import CableWatch, baseline_counts, diff_reports, run_watch, summary_lines


def port(name: str, *, partner: bool = False, cable: bool = False, data_role: str | None = None) -> TypeCPort:
    return TypeCPort(
        name=name,
        sysfs_path=f"/sys/class/typec/{name}",
        data_role=data_role,
        partner=TypeCPartner(name=f"{name}-partner", sysfs_path="") if partner else None,
        cable=TypeCCable(name=f"{name}-cable", sysfs_path="") if cable else None,
    )


def usb(name: str, *, devnum: str = "5", speed: str = "5000", product: str = "External SSD") -> UsbDevice:
    return UsbDevice(name=name, sysfs_path=f"/sys/bus/usb/devices/{name}", devnum=devnum, speed=speed, product=product)


def root_hub(name: str = "usb3") -> UsbDevice:
    return UsbDevice(name=name, sysfs_path="", id_vendor="1d6b", product="xHCI Host Controller", devnum="1")


def link(name: str, **properties: str) -> AdvancedDevice:
    return AdvancedDevice(source="Thunderbolt", name=name, summary=f"dock · {properties.get('tx_speed', '')}", properties=properties)


def ec_port(name: str, role: str, charging: str = "None", location: str = "Left front") -> AdvancedDevice:
    return AdvancedDevice(
        source="Framework EC",
        name=name,
        properties={"location": location, "role": role, "charging_type": charging},
    )


def test_identical_reports_produce_no_events() -> None:
    report = SystemReport(typec_ports=[port("port2", partner=True, cable=True)], usb_devices=[usb("3-7")])
    assert diff_reports(report, report) == []


def test_partner_detach_is_a_dropout_and_reattach_is_not() -> None:
    connected = SystemReport(typec_ports=[port("port2", partner=True, cable=True)])
    empty = SystemReport(typec_ports=[port("port2")])

    detach = diff_reports(connected, empty)
    assert [(e.kind, e.subject, e.dropout) for e in detach] == [("typec-detach", "port2", True)]
    assert "partner and cable detached" in detach[0].detail

    attach = diff_reports(empty, connected)
    assert [(e.kind, e.dropout) for e in attach] == [("typec-attach", False)]


def test_cable_only_removal_counts_as_dropout() -> None:
    before = SystemReport(typec_ports=[port("port3", partner=True, cable=True)])
    after = SystemReport(typec_ports=[port("port3", partner=True, cable=False)])
    events = diff_reports(before, after)
    assert [(e.kind, e.detail, e.dropout) for e in events] == [("typec-cable", "cable removed", True)]


def test_role_change_while_connected_is_reported_without_dropout() -> None:
    before = SystemReport(typec_ports=[port("port2", partner=True, data_role="[host] device")])
    after = SystemReport(typec_ports=[port("port2", partner=True, data_role="host [device]")])
    events = diff_reports(before, after)
    assert len(events) == 1
    assert events[0].kind == "typec-role"
    assert not events[0].dropout


def test_usb_reenumeration_is_a_dropout_even_when_the_device_never_looked_absent() -> None:
    before = SystemReport(usb_devices=[root_hub(), usb("3-7", devnum="5")])
    after = SystemReport(usb_devices=[root_hub(), usb("3-7", devnum="6")])
    events = diff_reports(before, after)
    assert [(e.kind, e.subject, e.dropout) for e in events] == [("usb-reenumerated", "3-7", True)]
    assert "device number 5 → 6" in events[0].detail


def test_usb_speed_drop_is_a_degradation_and_recovery_is_not() -> None:
    fast = SystemReport(usb_devices=[usb("3-7", speed="5000")])
    slow = SystemReport(usb_devices=[usb("3-7", speed="480")])
    down = diff_reports(fast, slow)
    assert [(e.kind, e.degradation) for e in down] == [("usb-speed", True)]
    assert "5 Gbps → 480 Mbps" in down[0].detail
    up = diff_reports(slow, fast)
    assert [(e.kind, e.degradation) for e in up] == [("usb-speed", False)]


def test_usb_remove_and_add_and_root_hubs_are_ignored() -> None:
    before = SystemReport(usb_devices=[root_hub("usb1"), usb("1-2")])
    after = SystemReport(usb_devices=[root_hub("usb2"), usb("1-3", speed="10000")])
    events = diff_reports(before, after)
    assert [(e.kind, e.subject, e.dropout) for e in events] == [
        ("usb-remove", "1-2", True),
        ("usb-add", "1-3", False),
    ]
    assert "10 Gbps" in events[1].detail


def test_thunderbolt_link_renegotiating_lower_is_a_degradation() -> None:
    before = SystemReport(advanced_devices=[link("0-1", tx_speed="40.0 Gb/s", rx_speed="40.0 Gb/s", tx_lanes="2")])
    after = SystemReport(advanced_devices=[link("0-1", tx_speed="20.0 Gb/s", rx_speed="40.0 Gb/s", tx_lanes="2")])
    events = diff_reports(before, after)
    assert [(e.kind, e.subject, e.degradation) for e in events] == [("link-change", "thunderbolt 0-1", True)]
    assert events[0].detail == "tx_speed 40.0 Gb/s → 20.0 Gb/s"


def test_thunderbolt_device_vanishing_is_a_dropout_and_debugfs_entries_are_ignored() -> None:
    debug = AdvancedDevice(source="USB debugfs", name="debug-usb-0")
    before = SystemReport(advanced_devices=[debug, link("0-1", tx_speed="40.0 Gb/s")])
    after = SystemReport(advanced_devices=[])
    events = diff_reports(before, after)
    assert [(e.kind, e.subject, e.dropout) for e in events] == [("link-remove", "thunderbolt 0-1", True)]


def test_ec_port_going_disconnected_is_a_dropout() -> None:
    before = SystemReport(advanced_devices=[ec_port("port2", "Sink", "USB PD")])
    after = SystemReport(advanced_devices=[ec_port("port2", "Disconnected", "None")])
    events = diff_reports(before, after)
    assert [(e.kind, e.dropout) for e in events] == [("ec-role", True), ("ec-charging", False)]
    assert events[0].subject == "ec port2 (Left front)"
    assert events[0].detail == "Sink → Disconnected"


def test_cable_watch_tallies_per_subject() -> None:
    watch = CableWatch()
    assert watch.observe(SystemReport(usb_devices=[usb("3-7", devnum="5")])) == []
    watch.observe(SystemReport(usb_devices=[usb("3-7", devnum="6")]))
    watch.observe(SystemReport(usb_devices=[usb("3-7", devnum="6", speed="480")]))
    watch.observe(SystemReport(usb_devices=[]))
    assert watch.tally.polls == 4
    assert watch.tally.events == 3
    assert watch.tally.dropouts == {"3-7": 2}
    assert watch.tally.degradations == {"3-7": 1}
    lines = summary_lines(watch.tally, 3723)
    assert lines[0].startswith("Watched for 1:02:03 across 4 polls: 2 dropouts, 1 degradations, 3 events.")
    assert lines[1] == "  dropouts: 3-7 ×2"


def test_baseline_counts_skip_root_hubs_and_non_link_sources() -> None:
    report = SystemReport(
        typec_ports=[port("port2"), port("port3")],
        usb_devices=[root_hub(), usb("3-7")],
        advanced_devices=[
            link("domain0"),
            AdvancedDevice(source="USB debugfs", name="debug-usb-0"),
            ec_port("port2", "Disconnected"),
            AdvancedDevice(source="Framework EC", name="controller", properties={"pd_power_info": "Available"}),
        ],
    )
    assert baseline_counts(report) == {"typec_ports": 2, "usb_devices": 1, "link_devices": 1, "ec_ports": 1}


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 6, 16, 0, 0, tzinfo=timezone.utc)
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def test_run_watch_prints_baseline_events_and_summary() -> None:
    reports = iter(
        [
            SystemReport(usb_devices=[usb("3-7", devnum="5")]),
            SystemReport(usb_devices=[usb("3-7", devnum="5")]),
            SystemReport(usb_devices=[usb("3-7", devnum="7")]),
        ]
    )
    clock = FakeClock()
    out = io.StringIO()

    code = run_watch(lambda: next(reports), interval=2.0, as_json=False, out=out, now=clock, sleep=clock.sleep, polls=3)

    assert code == 0
    assert clock.slept == [2.0, 2.0]
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("2026-11-06T16:00:00+00:00  watching 0 Type-C ports, 1 USB devices")
    assert lines[1] == "2026-11-06T16:00:04+00:00  usb-reenumerated 3-7: External SSD device number 5 → 7 [dropout]"
    assert lines[2].startswith("Watched for 0:00:04 across 3 polls: 1 dropouts, 0 degradations, 1 events.")
    assert lines[3] == "  dropouts: 3-7 ×1"


def test_run_watch_json_is_newline_delimited_and_ends_with_summary() -> None:
    reports = iter([SystemReport(), SystemReport(usb_devices=[usb("1-1")])])
    clock = FakeClock()
    out = io.StringIO()

    run_watch(lambda: next(reports), interval=0.5, as_json=True, out=out, now=clock, sleep=clock.sleep, polls=2)

    records = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [record["kind"] for record in records] == ["baseline", "event", "summary"]
    assert records[1]["event"] == "usb-add"
    assert records[1]["subject"] == "1-1"
    assert records[1]["dropout"] is False
    assert records[2]["polls"] == 2
    assert records[2]["elapsed_seconds"] == 0.5


def test_run_watch_reports_a_failed_scan_and_keeps_going() -> None:
    calls = {"n": 0}

    def flaky() -> SystemReport:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("device vanished mid-read")
        return SystemReport()

    clock = FakeClock()
    out = io.StringIO()
    run_watch(flaky, interval=1.0, as_json=False, out=out, now=clock, sleep=clock.sleep, polls=3)
    lines = out.getvalue().splitlines()
    assert any("scan-error: device vanished mid-read" in line for line in lines)
    assert lines[-1].startswith("  No dropouts or degradations seen.")


def test_run_watch_stops_cleanly_on_keyboard_interrupt() -> None:
    def interrupted() -> SystemReport:
        raise KeyboardInterrupt

    clock = FakeClock()
    out = io.StringIO()
    assert run_watch(interrupted, interval=1.0, as_json=False, out=out, now=clock, sleep=clock.sleep) == 0
    assert out.getvalue().splitlines()[0].startswith("Watched for 0:00:00 across 0 polls")


@pytest.mark.parametrize("argv", [["--watch", "--interval", "0"], ["--watch", "--interval", "-1"]])
def test_cli_rejects_non_positive_interval(argv: list[str]) -> None:
    from whatcable_linux.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2
