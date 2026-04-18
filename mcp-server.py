#!/usr/bin/env python3
"""MCP server for the Relay Control Panel."""

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = "http://your-pi:8080"

mcp = FastMCP(
    "relay-panel",
    instructions="Control I2C relay boards on your-pi. Relays can be toggled, set on/off, or controlled in bulk. Each relay has a number and a custom name. State is always 'on' or 'off'.",
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
    """List all relay boards with their relays, names, and current states (on/off).

    Returns all configured boards. Each board has an index, name, I2C address/bus,
    and a list of relays with their number, name, and state ('on' or 'off').
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


if __name__ == "__main__":
    mcp.run()
