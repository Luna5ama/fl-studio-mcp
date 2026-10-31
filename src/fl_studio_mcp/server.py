"""FL Studio MCP Server - Control FL Studio via Model Context Protocol.

This MCP server provides tools to control FL Studio through two mechanisms:
1. MIDI + JSON - Real-time control of mixer, transport, channels, and plugins
2. Piano Roll Scripts - Persistent note placement via JSON + keystroke triggering

Requirements:
- FL Studio 20.7+ running
- FLStudioMCP controller script installed (run: ./scripts/setup_mac.sh)
- On Mac: IAC Driver enabled in Audio MIDI Setup
- On Windows: loopMIDI virtual MIDI ports configured
- For piano roll: ComposeWithLLM.pyscript installed in FL Studio

Limitations:
- Loading requires Windows, local English OCR, scanned plugins, English UI, and MIDI API 34+
- Native UI workflows require Windows and an unobstructed FL Studio window
"""

from __future__ import annotations

from fastmcp import FastMCP

from fl_studio_mcp.tools import (
    register_channel_tools,
    register_mixer_tools,
    register_piano_roll_tools,
    register_plugin_tools,
    register_transport_tools,
)
from fl_studio_mcp.tools.arrangement import register_arrangement_tools
from fl_studio_mcp.tools.diagnostics import register_diagnostic_tools
from fl_studio_mcp.tools.plugin_loading import register_plugin_loading_tools
from fl_studio_mcp.tools.plugin_management import register_plugin_management_tools
from fl_studio_mcp.tools.project_files import register_project_file_tools
from fl_studio_mcp.tools.routing import register_routing_tools
from fl_studio_mcp.utils.connection import get_connection, reset_connection

# Create the MCP server
mcp = FastMCP(
    name="fl-studio-mcp",
    instructions="""
FL Studio MCP Server - Control FL Studio from AI assistants.

This server provides tools to control FL Studio through its Python scripting API.
FL Studio must be running with the FLStudioMCP MIDI controller enabled.

Available tool categories:
- Transport: Play, stop, record, tempo, position control
- Mixer: Volume, pan, mute, solo, track management
- Channels: Channel info, note triggering, step sequencer
- Plugins: Discover/load instruments and effects, inventory, lifecycle, full FST presets
- Routing: Sends, buses, sidechain
- Arrangement: Patterns, timing, visible Playlist clips
- Files: Sample import, explicit project save, WAV export
- Diagnostics: Controller handshake, project summary, load history and cancellation

Important limitations:
1. Plugin loading uses FL's Browser and scanned Plugin database entries. Use
   fl_list_available_plugins followed by fl_add_plugin; only status=loaded confirms
   completion. Loading requires Windows/English UI and readable, unobstructed
   Browser and Mixer controls. Keep FL focused and untouched while loading. Never auto-retry
   an uncertain add operation; inspect fl_get_plugin_load_status and the project.
2. Pattern API indices and Playlist tracks/bars are one-based; channel, mixer and
   plugin slot indices are zero-based. Native clip tools operate on the visible Playlist.
   File save/export requires an explicit absolute output path; overwrites are opt-in.
3. Note triggering (fl_trigger_note) is real-time only - notes won't persist
   unless FL Studio is recording. Use step sequencer (fl_set_grid_bit) for
   persistent drum patterns.
""",
)


# Register connection status resource
@mcp.resource("fl://status")
def get_fl_status() -> str:
    """Get FL Studio connection status."""
    conn = get_connection()
    result = _connection_status(conn)
    if result["connected"]:
        return "FL Studio controller handshake verified via MIDI"
    return "Not connected: " + str(result.get("error"))


@mcp.resource("fl://project")
def get_project_info() -> dict:
    """Get current FL Studio project information."""
    conn = get_connection()

    if not conn.is_connected:
        return {"error": conn.connection_error}

    try:
        result = conn.send_command("transport.getStatus")
        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {
            "is_playing": result.get("is_playing", False),
            "is_recording": result.get("is_recording", False),
            "position": result.get("position", ""),
            "loop_mode": result.get("loop_mode", "pattern"),
        }
    except Exception as e:
        return {"error": str(e)}


# Connection management tools
@mcp.tool()
def fl_connect() -> str:
    """Connect or reconnect to FL Studio via MIDI.

    Use this tool to:
    - Check if FL Studio is connected
    - Retry connection after starting FL Studio
    - Reconnect if the connection was lost

    Returns the connection status.
    """
    # Reset connection state to force a fresh connection attempt
    reset_connection()

    conn = get_connection()
    try:
        conn.ensure_connected()
    except RuntimeError as e:
        return f"Connection failed: {e}"
    status = _connection_status(conn)
    if not status["connected"]:
        return "Connection failed: " + str(status["error"])
    return "FL Studio controller handshake verified via MIDI."


@mcp.tool()
def fl_connection_status() -> dict:
    """Get the current FL Studio connection status.

    Returns information about whether FL Studio is connected
    and any error messages if not.
    """
    conn = get_connection()
    return _connection_status(conn)


def _connection_status(conn) -> dict:
    try:
        response = conn.send_command("system.getCapabilities")
    except RuntimeError as exc:
        response = {"success": False, "error": str(exc)}
    verified = bool(
        response.get("success")
        and not response.get("error")
        and response.get("protocol_version") == 2
        and isinstance(response.get("midi_api_version"), int)
    )
    status = conn.get_status()
    return {
        "connected": verified,
        "midi_port_open": status.get("connected", False),
        "port_name": status.get("port_name"),
        "available_ports": status.get("available_ports", []),
        "controller": response,
        "error": None if verified else response.get("error", "Controller protocol mismatch"),
    }


# Register all tools
register_transport_tools(mcp)
register_mixer_tools(mcp)
register_channel_tools(mcp)
register_plugin_tools(mcp)
register_plugin_loading_tools(mcp)
register_diagnostic_tools(mcp)
register_routing_tools(mcp)
register_arrangement_tools(mcp)
register_plugin_management_tools(mcp)
register_project_file_tools(mcp)
register_piano_roll_tools(mcp)


def main():
    """Run the FL Studio MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
