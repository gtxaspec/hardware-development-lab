#!/usr/bin/env python3
"""MCP server for the Relay Control Panel."""

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = "http://your-pi:8080"

mcp = FastMCP(
    "relay-panel",
    instructions=(
        "Control I2C relay boards and direct GPIO pins on your-pi. "
        "Relays are grouped on I2C boards (board index + relay number). "
        "GPIOs are individual wiringPi-numbered pins used for direct HI/LOW control "
        "(e.g. driving a USB switch from pin 40 / wPi 27). "
        "Both can be toggled, set on/off, listed, added, removed, and renamed. "
        "State is always 'on' or 'off'."
    ),
)


def api(method: str, path: str, body: dict | None = None) -> dict:
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
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
    return api("GET", "/api/state")


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


if __name__ == "__main__":
    mcp.run()
