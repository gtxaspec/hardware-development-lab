#!/usr/bin/env python3
"""MCP server for the Relay Control Panel."""

import os

import httpx
from mcp.server.mcpserver import MCPServer

# Where the panel is reachable. Set RELAY_PANEL_URL in the MCP server's env if
# it runs on a different machine to the panel, which is the usual case.
BASE_URL = os.environ.get("RELAY_PANEL_URL", "http://localhost:8080")

mcp = MCPServer(
    "relay-panel",
    instructions=(
        "Control I2C relay boards and direct GPIO pins on a hardware test bench. "
        "Relays are grouped on I2C boards (board index + relay number). "
        "GPIOs are individual wiringPi-numbered pins used for direct HI/LOW control "
        "(e.g. driving a USB switch from pin 40 / wPi 27). "
        "Both can be toggled, set on/off, listed, added, removed, and renamed. "
        "State is always 'on' or 'off'. "
        "PWM channels are separate: a PCA9685 drives dimmable 12 V LED strips "
        "through MOSFET modules for camera day/night testing. Those are "
        "continuous, addressed by chip index + channel, and set with a level "
        "(0-4095) or a percent (0-100), not on/off. Channels can be collected "
        "into named groups driven by one master level. "
        "Connected Ingenic USB devices (bootrom / DFU gadget) are also exposed "
        "read-only, showing which bench devices are in a flashable state. "
        "Backpacks are ESP32 units that each own one camera: its power, boot pin, "
        "USB port and console. list_backpacks shows them; backpack_action powers, "
        "power-cycles or puts the camera into its bootrom, and the unit's dfu "
        "address is what thingino-dfu --host flashes it through. "
        "Adding, removing and re-addressing boards and PWM chips is deliberately "
        "not exposed here - that is physical setup, done in the web UI."
    ),
)


def api(method: str, path: str, body: dict | None = None, timeout: float = 10) -> dict:
    with httpx.Client(base_url=BASE_URL, timeout=timeout) as client:
        if method == "GET":
            r = client.get(path)
        elif method == "POST":
            r = client.post(path, json=body)
        elif method == "PUT":
            r = client.put(path, json=body)
        elif method == "DELETE":
            r = client.delete(path)
        else:
            return {"error": f"Unknown method {method}"}
        return r.json()


@mcp.tool()
def list_relays() -> dict:
    """List all relay boards and configured GPIOs with their current states.

    Returns all configured I2C boards and direct GPIO pins. Each board has an
    index, name, I2C address/bus, and a list of relays (num, name, state).
    Each GPIO has a wiringPi number, optional physical pin number, name, and state.
    State is always 'on' or 'off'.
    """
    return api("GET", "/api/state?fresh=1")


@mcp.tool()
def toggle_relay(board: int, relay: int) -> dict:
    """Toggle a single relay and return its new state.

    Args:
        board: Board index (0-based)
        relay: Relay number (0-15 for a 16ch board)

    Returns the relay number, name, and new state ('on' or 'off').
    """
    return api("POST", "/api/relay/toggle", {"board": board, "relay": relay})


@mcp.tool()
def set_relay(board: int, relay: int, state: str) -> dict:
    """Set a relay to a specific state.

    Args:
        board: Board index (0-based)
        relay: Relay number (0-15 for a 16ch board)
        state: 'on' or 'off'

    Returns the relay number, name, and confirmed state.
    """
    return api("POST", "/api/relay/set", {
        "board": board,
        "relay": relay,
        "state": state == "on",
    })


@mcp.tool()
def all_on(board: int) -> dict:
    """Turn all relays on a board ON.

    Args:
        board: Board index (0-based)
    """
    return api("POST", "/api/board/all", {"board": board, "state": True})


@mcp.tool()
def all_off(board: int) -> dict:
    """Turn all relays on a board OFF.

    Args:
        board: Board index (0-based)
    """
    return api("POST", "/api/board/all", {"board": board, "state": False})


@mcp.tool()
def rename_relay(board: int, relay: int, name: str) -> dict:
    """Rename a relay.

    Args:
        board: Board index (0-based)
        relay: Relay number
        name: New display name for the relay
    """
    return api("PUT", "/api/relay/name", {
        "board": board,
        "relay": relay,
        "name": name,
    })


@mcp.tool()
def rename_board(board: int, name: str) -> dict:
    """Rename a board.

    Args:
        board: Board index (0-based)
        name: New display name for the board
    """
    return api("PUT", "/api/board/name", {"board": board, "name": name})


@mcp.tool()
def reset_board(board: int) -> dict:
    """Reset a board: re-initialize I2C direction registers and set all relays OFF.

    Use this if I2C communication seems stuck or relays are in an unknown state.

    Args:
        board: Board index (0-based)
    """
    return api("POST", "/api/board/reset", {"board": board})


@mcp.tool()
def list_gpios() -> dict:
    """List all configured GPIO pins with their current states.

    Returns each GPIO's wiringPi number (wpi), optional physical pin number,
    name, and state ('on' or 'off'). If the hardware read fails, 'error' is set.
    """
    return api("GET", "/api/gpios")


@mcp.tool()
def add_gpio(wpi: int, name: str = "", pin: int | None = None) -> dict:
    """Configure a new GPIO pin for HI/LOW control.

    Sets the pin to output mode and starts it OFF. Use the wiringPi number
    shown by `gpio readall` (the wPi column).

    Args:
        wpi: wiringPi pin number (from `gpio readall`)
        name: Optional display name (default: 'GPIO {wpi}')
        pin: Optional physical header pin number (display only)
    """
    body: dict = {"wpi": wpi}
    if name:
        body["name"] = name
    if pin is not None:
        body["pin"] = pin
    return api("POST", "/api/gpios", body)


@mcp.tool()
def remove_gpio(wpi: int) -> dict:
    """Unconfigure a GPIO pin. Does not change the pin state on the hardware.

    Args:
        wpi: wiringPi pin number
    """
    return api("DELETE", f"/api/gpios/{wpi}")


@mcp.tool()
def set_gpio(wpi: int, state: str) -> dict:
    """Set a GPIO pin HIGH ('on') or LOW ('off').

    Args:
        wpi: wiringPi pin number of a configured GPIO
        state: 'on' or 'off'
    """
    return api("POST", "/api/gpio/set", {
        "wpi": wpi,
        "state": state == "on",
    })


@mcp.tool()
def toggle_gpio(wpi: int) -> dict:
    """Toggle a GPIO pin and return its new state.

    Args:
        wpi: wiringPi pin number of a configured GPIO
    """
    return api("POST", "/api/gpio/toggle", {"wpi": wpi})


@mcp.tool()
def rename_gpio(wpi: int, name: str) -> dict:
    """Rename a configured GPIO pin.

    Args:
        wpi: wiringPi pin number of a configured GPIO
        name: New display name
    """
    return api("PUT", "/api/gpio/name", {"wpi": wpi, "name": name})


@mcp.tool()
def list_leds() -> dict:
    """List PWM (LED dimming) chips and their channels with live levels.

    Each chip has an index, I2C address/bus, the PWM frequency it is actually
    producing, and its PRE_SCALE divider. Each channel has a number, name,
    stagger phase, level in counts (0-4095) and percent. Levels are read back
    from the chip, not cached.
    """
    return api("GET", "/api/pwm")


@mcp.tool()
def set_led(chip: int, channel: int, level: int | None = None,
            percent: float | None = None) -> dict:
    """Set one PWM channel's brightness.

    Give either level or percent. Level is the raw 12-bit duty and is what to
    use for repeatable test steps; percent is a convenience that rounds to the
    nearest count.

    Args:
        chip: PWM chip index (0-based)
        channel: Channel number on that chip
        level: Duty in counts, 0-4095
        percent: Duty as a percentage, 0-100
    """
    body: dict = {"chip": chip, "channel": channel}
    if level is not None:
        body["level"] = level
    elif percent is not None:
        body["percent"] = percent
    else:
        return {"error": "level or percent is required"}
    return api("POST", "/api/pwm/set", body)


@mcp.tool()
def set_all_leds(chip: int, level: int | None = None,
                 percent: float | None = None) -> dict:
    """Set every channel on a PWM chip to the same brightness.

    Args:
        chip: PWM chip index (0-based)
        level: Duty in counts, 0-4095
        percent: Duty as a percentage, 0-100
    """
    body: dict = {"chip": chip}
    if level is not None:
        body["level"] = level
    elif percent is not None:
        body["percent"] = percent
    else:
        return {"error": "level or percent is required"}
    return api("POST", "/api/pwm/all", body)


@mcp.tool()
def leds_off(chip: int | None = None) -> dict:
    """Force PWM channels dark in a single I2C write.

    Args:
        chip: PWM chip index, or omit to blank every configured chip
    """
    return api("POST", "/api/pwm/off", {} if chip is None else {"chip": chip})


@mcp.tool()
def add_led_channel(chip: int, channel: int, name: str = "") -> dict:
    """Bring one of the chip's 16 channels under management.

    Channels need not be contiguous - a rig can manage 0-3 for two strips and
    then 7 for an IR bank. A newly added channel starts dark.

    Args:
        chip: PWM chip index (0-based)
        channel: Channel number on the chip, 0-15
        name: Optional display name, e.g. "IR 850nm"
    """
    body: dict = {"chip": chip, "channel": channel}
    if name:
        body["name"] = name
    return api("POST", "/api/pwm/channels", body)


@mcp.tool()
def remove_led_channel(chip: int, channel: int) -> dict:
    """Stop managing a channel. It is blanked and dropped from any group.

    A group left with no members is removed too; the response lists any that
    went in `groups_dropped`.

    Args:
        chip: PWM chip index (0-based)
        channel: Channel number to remove
    """
    return api("DELETE", f"/api/pwm/channels/{chip}/{channel}")


@mcp.tool()
def remove_led_group(chip: int, group: int) -> dict:
    """Delete a channel group. Its member channels keep their current levels.

    Args:
        chip: PWM chip index (0-based)
        group: Group index within that chip
    """
    return api("DELETE", f"/api/pwm/groups/{chip}/{group}")


@mcp.tool()
def set_led_group(chip: int, group: int, level: int | None = None,
                  percent: float | None = None) -> dict:
    """Set a whole group of PWM channels from one master level.

    A group is a named set of channels on a chip - e.g. the R, G and B legs of
    one RGB strip driven together as a single light. Members default to equal
    weights, so the master simply sets them all the same. Give a member an
    unequal weight and the master scales them proportionally instead, which
    holds a calibrated white point while dimming.

    Args:
        chip: PWM chip index (0-based)
        group: Group index within that chip (0-based)
        level: Master duty in counts, 0-4095
        percent: Master duty as a percentage, 0-100
    """
    body: dict = {"chip": chip, "group": group}
    if level is not None:
        body["level"] = level
    elif percent is not None:
        body["percent"] = percent
    else:
        return {"error": "level or percent is required"}
    return api("POST", "/api/pwm/group", body)


@mcp.tool()
def add_led_group(chip: int, name: str, channels: list[int]) -> dict:
    """Create a named group of PWM channels driven by one master level.

    Args:
        chip: PWM chip index (0-based)
        name: Display name, e.g. "RGB Strip"
        channels: Channel numbers to drive together, e.g. [1, 2, 3]
    """
    return api("POST", "/api/pwm/groups", {
        "chip": chip,
        "name": name,
        "channels": channels,
    })


@mcp.tool()
def edit_led_group(chip: int, group: int, name: str = "",
                   channels: list[int] | None = None,
                   weights: dict | None = None) -> dict:
    """Rename a group, change which channels it drives, or set its weights.

    Any subset of the three may be given. Weights are 0-4095 and are the level
    each member sits at when the master is full; equal weights (the default)
    mean the master sets every member the same, unequal weights hold a colour
    balance while dimming. Weight keys are channel numbers as strings.

    Args:
        chip: PWM chip index (0-based)
        group: Group index within that chip
        name: New display name
        channels: New membership, e.g. [1, 2, 3]
        weights: e.g. {"1": 4095, "2": 3200, "3": 2600}
    """
    body: dict = {"chip": chip, "group": group}
    if name:
        body["name"] = name
    if channels is not None:
        body["channels"] = channels
    if weights is not None:
        body["weights"] = weights
    if len(body) == 2:
        return {"error": "give at least one of name, channels or weights"}
    return api("PUT", "/api/pwm/group", body)


@mcp.tool()
def set_led_phase(chip: int, channel: int, phase: int) -> dict:
    """Set a channel's stagger phase: the count in the 4096-step cycle at which
    it switches on.

    Channels lit together default to a quarter-period stagger (0, 1024, 2048,
    3072) so their on-times spread across the cycle instead of stacking at
    count 0, which lowers both peak current draw and combined optical ripple.
    Three channels driven as one white light spread best at exact thirds
    (0, 1365, 2731). Re-applies at the channel's current duty.

    Args:
        chip: PWM chip index (0-based)
        channel: Channel number
        phase: Turn-on count, 0-4095
    """
    return api("PUT", "/api/pwm/phase", {
        "chip": chip,
        "channel": channel,
        "phase": phase,
    })


@mcp.tool()
def rename_led_chip(chip: int, name: str) -> dict:
    """Rename a PWM chip. Use rename_led_channel for a single channel.

    Args:
        chip: PWM chip index (0-based)
        name: New display name, e.g. "LED Strips"
    """
    return api("PUT", "/api/pwm/name", {"chip": chip, "name": name})


@mcp.tool()
def reset_led_chip(chip: int) -> dict:
    """Re-initialize a PWM chip: wake it, re-enable auto-increment, re-apply the
    configured frequency, and force every channel dark.

    Use this if the chip is in an unknown state or stopped responding. The
    relay-board equivalent is reset_board.

    Args:
        chip: PWM chip index (0-based)
    """
    return api("POST", "/api/pwm/reset", {"chip": chip})


@mcp.tool()
def list_usb() -> dict:
    """List connected Ingenic USB devices (vendor a108), read fresh from sysfs.

    These are the mask-ROM bootrom (a108:c309) and U-Boot's DFU gadget
    (a108:4d44), so this shows which bench devices are currently sitting in a
    flashable state. `port` is the sysfs bus-port path, stable per physical
    socket. Read-only. The same list is included in list_relays.
    """
    return api("GET", "/api/usb")


@mcp.tool()
def set_led_freq(chip: int, freq_hz: float) -> dict:
    """Set a PWM chip's frequency. Applies to all 16 channels.

    The PCA9685 divides a 25 MHz oscillator by an integer, so the achievable
    range is 24 Hz to 1526 Hz and the steps are uneven - the response reports
    the frequency actually produced, which is what a test log should record.
    Low frequencies are useful on purpose: they make rolling-shutter banding
    appear so a rig can prove it can produce the artifact, not just avoid it.

    Args:
        chip: PWM chip index (0-based)
        freq_hz: Requested frequency in Hz
    """
    return api("PUT", "/api/pwm/freq", {"chip": chip, "freq_hz": freq_hz})


@mcp.tool()
def rename_led_channel(chip: int, channel: int, name: str) -> dict:
    """Rename a PWM channel.

    Args:
        chip: PWM chip index (0-based)
        channel: Channel number
        name: New display name
    """
    return api("PUT", "/api/pwm/name", {
        "chip": chip,
        "channel": channel,
        "name": name,
    })



def _backpack_id(backpack: str | int) -> int | dict:
    """Accept a backpack's id, its name or its mDNS hostname."""
    units = api("GET", "/api/backpacks").get("backpacks", [])
    for unit in units:
        if str(backpack) in (str(unit["id"]), unit["name"], unit.get("mdns") or "", unit["host"]):
            return unit["id"]
    return {"error": f"no backpack {backpack!r}; known: {[u['name'] for u in units]}"}


@mcp.tool()
def list_backpacks() -> dict:
    """List the thingino backpacks: ESP32 units that each control one camera.

    Each has an id, name, host, online/stale flags, firmware version, the
    camera's power ('on'/'off') and boot pin ('held'/'released'), the USB
    devices enumerated on its port as VID:PID (a108:c309 = bootrom, a108:4d44 =
    DFU gadget), `dfu` (the host:port to pass to thingino-dfu --host) and
    `uart` (its console's index in the uart list).
    """
    return api("GET", "/api/backpacks")


@mcp.tool()
def backpack_action(backpack: str, action: str) -> dict:
    """Operate a backpack's camera.

    Actions: power-on, power-off, power-cycle, boot-hold, boot-release, and
    bootrom: hold the boot pin, power-cycle, and release the pin as soon as the
    bootrom enumerates (fails after ~10 s if nothing does). After bootrom,
    flash with `thingino-dfu --host <dfu> -w image.bin` using list_backpacks'
    `dfu` address; no USB wait is needed, the unit already saw the bootrom.

    Args:
        backpack: The backpack's id, name or mDNS hostname
        action: One of the actions above
    """
    uid = _backpack_id(backpack)
    if isinstance(uid, dict):
        return uid
    return api("POST", "/api/backpack/action", {"id": uid, "action": action}, timeout=40)


@mcp.tool()
def backpack_setting(backpack: str, setting: str, value: str) -> dict:
    """Change a backpack setting, saved on the unit.

    bootsel = low | high: how the SoC's BOOTSEL line asks for USB boot.
    usb_port = on | off: off takes the backpack off the camera's USB lines
    (for cameras whose Wi-Fi module shares them); bootrom is refused while off.

    Args:
        backpack: The backpack's id, name or mDNS hostname
        setting: bootsel or usb_port
        value: The new value
    """
    uid = _backpack_id(backpack)
    if isinstance(uid, dict):
        return uid
    return api("POST", "/api/backpack/setting", {"id": uid, setting: value}, timeout=20)


if __name__ == "__main__":
    mcp.run()
