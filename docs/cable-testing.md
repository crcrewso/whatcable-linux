# Testing a cable with WhatCable

WhatCable reads what the kernel and firmware already know about a connection. It
never drives traffic through a cable or draws power through it. This page says
what that still lets you find out, how to make a cable show its faults while
WhatCable is watching, and where the limits are.

The procedures were written against the code; the throughput and dropout
recipes have not been exercised on every hardware combination. Treat the
numbers below as what to expect, not as a guarantee.

## What one report tells you

For a Type-C port with a cable and a partner attached, the normal report
(`whatcable-linux`, or `--json --raw` for everything) shows:

- **The cable's own claims**, decoded from its e-marker: vendor, rated speed,
  rated current and voltage, active or passive. The kernel reads these during
  USB Power Delivery discovery. A cable without an e-marker is a USB 2.0 cable
  or a 3 A USB 3 cable; the report cannot tell which until a device is attached.
- **The negotiated power contract** from the kernel's Power Delivery class, or on
  Framework laptops from the embedded controller.
- **A compatibility verdict** for each alternate mode the partner advertises
  (DisplayPort, Thunderbolt): Yes, No or Unknown, using the same rule the Linux
  kernel applies before it lets the mode start. The verdict is about the
  e-marker; it does not try the mode.
- **The negotiated USB speed** of every attached USB device (480 Mbps, 5, 10 or
  20 Gbps) and the **negotiated Thunderbolt or USB4 link** (speed and lanes) when
  one came up.

That last pair is the closest a single report gets to a functional test: a
cable rated for 40 Gbps whose device negotiated 20 Gbps, or a 5 A cable through
which the charger agreed to 3 A, has told you something.

## Testing a cable with a device on the far end

A directory is a place on a filesystem, not on a port. Copying between two
directories on the internal disk never touches a cable, whatever is plugged in.
To exercise a cable the far end must be a device the kernel can reach only
through it: a USB 3 or USB4 SSD is the simplest.

1. Plug the SSD in through the cable under test and mount it.
2. Make a payload that compression cannot flatter, and record its checksum:

   ```sh
   mkdir -p /tmp/cabletest
   head -c 4G /dev/urandom > /tmp/cabletest/payload.bin
   sha256sum /tmp/cabletest/payload.bin
   ```

3. Copy it across, flush, and check it on the far side:

   ```sh
   time cp /tmp/cabletest/payload.bin /run/media/$USER/<ssd>/payload.bin && sync
   sha256sum /run/media/$USER/<ssd>/payload.bin
   ```

4. Note the wall time, unmount, move the cable to the other port, repeat. Then
   repeat once more with a cable you trust.

The checksum must match. The time tells you the class of cable: a USB 2.0 cable
enumerates the SSD at 480 Mbps and the copy takes minutes instead of seconds,
and `whatcable-linux` shows the device's negotiated speed either way. A device
that disappears mid-copy, or an I/O error, is a fault signal; see the next
section for catching the intermittent kind.

## Hunting an intermittent cable

A cable that reports fine and drops out now and then is invisible to a single
report, because a drop that lasts half a second is over before you look. Two
things catch it: the kernel announces every detach and re-attach as it happens,
and a USB device that re-enumerates comes back with a **new device number**, so
even a drop you missed leaves a fingerprint.

### Watch mode

```sh
whatcable-linux --watch
```

polls every two seconds (`--interval 0.5` for finer) and prints one timestamped
line per change until Ctrl+C, then a summary. Lines marked `[dropout]` are a
Type-C partner detaching, a USB device vanishing or coming back with a new
device number, a Thunderbolt device disappearing, or a Framework port going to
Disconnected. Lines marked `[degraded]` are a USB device or Thunderbolt link
that came back slower than before.

```text
2026-11-06T16:00:00+00:00  watching 2 Type-C ports, 1 USB devices, 0 Thunderbolt/USB4 entries, 4 EC ports. Polling every 2s; Ctrl+C stops and prints a summary.
2026-11-06T16:04:12+00:00  usb-reenumerated 3-7: External SSD device number 5 → 7 [dropout]
2026-11-06T16:04:14+00:00  usb-speed        3-7: External SSD 5 Gbps → 480 Mbps [degraded]
Watched for 0:10:23 across 311 polls: 1 dropouts, 1 degradations, 2 events.
  dropouts: 3-7 ×1
  degradations: 3-7 ×1
```

`--watch --json` emits the same as newline-delimited JSON for scripts. Watch
mode only compares reports; a drop that begins and ends between two polls shows
only through the device-number change, which is why the interval does not need
to be short.

Inside the Flatpak the same command is
`flatpak run com.nedrichards.WhatCable.Devel --watch`.

### The kernel's own log

The kernel records every event at the moment it happens, with no polling gap.
Leave one of these running beside watch mode:

```sh
udevadm monitor --kernel --subsystem-match=usb --subsystem-match=typec --subsystem-match=power_supply
```

```sh
journalctl -kf -o short-precise | grep -iE 'usb|typec|ucsi|thunderbolt'
```

A good cable prints its attach once and then nothing. A flapping one prints
remove and add pairs, `USB disconnect, device number N`, or
`reset SuperSpeed USB device`, each with a time you can correlate with what was
happening at the desk.

### Keep the cable busy

Faults often appear only under load. The easiest continuous load is a USB-C
Ethernet adapter on the end of the cable and a fast ping through it, which
prints a line for every missed reply:

```sh
ping -i 0.2 -O <a host on that link>
```

Without an adapter, loop a read from the SSD every second and log failures with
the time. While either runs, flex the cable near each plug. Intermittent cables
usually fail at the strain relief, and a drop you can provoke by hand is a drop
you can be sure of.

## A USB4 port with a cable that is not USB4

A USB4 port decides what to run from the cable's e-marker before any data link
comes up. If the cable does not identify as USB4-capable the port never tries
USB4 and settles on the best mode the cable allows, silently. From the USB4 and
Type-C specifications:

| Cable | What a USB4 port runs through it |
|---|---|
| USB4 or Thunderbolt 3 40 Gbps, e-marked | USB4 at 40 Gbps, Thunderbolt tunnelling, DisplayPort, up to the marked current |
| USB 3.2 Gen 2 (10 Gbps) passive, e-marked | USB4 at 20 Gbps, or USB 3 at 10 Gbps for a non-USB4 device; DisplayPort works |
| USB 3.2 Gen 1 (5 Gbps), or unmarked with SuperSpeed wires | No USB4. USB 3 at 5 Gbps; DisplayPort usually works |
| USB 2.0 or charge-only | USB 2 at 480 Mbps and power only. No display, no Thunderbolt device appears |
| Any of the above without a 5 A marking | Charging capped at 3 A (60 W at 20 V), however strong the charger |

To see which happened, look at the report:

- A device under **Advanced sources** from `/sys/bus/thunderbolt/devices` with
  `tx_speed` and `rx_lanes` means USB4 or Thunderbolt came up, at 20 or 40 Gbps.
- No such device, with the same hardware listed under **USB devices** instead,
  means the port fell back to USB 3 or USB 2; the negotiated speed says which.
- Where the kernel exposes no Type-C ports (see the Framework note below) you
  cannot read the e-marker directly. Infer the cable from the speed the same
  device reaches through a cable you trust.

This matters for the dropout hunt. A cable that honestly is not USB4 is the
quiet case: the port downgrades once and stays there. The cable that flaps is
the one that *claims* more than it reliably carries, through a damaged
high-speed pair or an optimistic e-marker. The port trains a 40 Gbps link
because the marker says it may, the link takes errors under load, and the kernel
retrains or disconnects, which watch mode reports as a `link-change` or a device
re-enumerating at a lower speed.

## Framework 13 note

On the Framework 13 firmware documented in
[the investigation](framework-13-usb-c-investigation.md), the kernel exposes no
Type-C ports, so cable identity, speed and alt-mode verdicts are unavailable.
The per-port view comes from the embedded controller and needs read access to
`/dev/cros_ec` (see the README): role, charging type, negotiated voltage and
current, maximum power. Port 2 is **Left front**, port 3 is **Left back**. What
that tests about a cable is whether a charger or device negotiates through it
and at what contract; watch mode still reports the port going to Disconnected.

## Why a loopback does not test a cable

One cable between two ports of the same machine proves little. Over plain USB it
does nothing, because USB is host-to-device and both ports are hosts. Over
Thunderbolt or USB4, Linux can bring up a host-to-host network link, but only
with a USB4 or Thunderbolt cable, so a lesser cable looks exactly like a dead
one, and even then copying between two local directories never crosses the
link. Use a device on the far end instead.
