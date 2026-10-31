"""Inspect and edit Mixer sends, buses and sidechains with verified readback."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fl_studio_mcp.utils.connection import get_connection

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _send(action: str, params: dict) -> dict:
    try:
        connection = get_connection()
        with connection.transaction():
            return connection.send_command(action, params, timeout=10.0)
    except (RuntimeError, OSError) as exc:
        return {"success": False, "error": str(exc)}


def register_routing_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def fl_get_mixer_routing(
        source_track: int | None = None,
        destination_track: int | None = None,
        include_disabled: bool = False,
    ) -> dict:
        """Read Mixer routes and send levels, including sidechain-only links.

        Indices are zero-based; Master is 0, Current is excluded. Omit source_track
        to inspect all inserts. Omit destination_track to inspect all destinations.
        Disabled routes are omitted unless include_disabled=True. API 36+ required.
        """
        return _send(
            "routing.get",
            {
                "source_track": source_track,
                "destination_track": destination_track,
                "include_disabled": include_disabled,
            },
        )

    @mcp.tool()
    def fl_set_mixer_route(
        source_track: int,
        destination_track: int,
        enabled: bool = True,
        exclusive: bool = False,
    ) -> dict:
        """Enable/disable an internal Mixer route, preserving its existing send level.

        A newly enabled route uses FL's initial level. Other sends remain intact.
        exclusive=True explicitly disconnects ALL other destinations of this source,
        including Master; it is only valid when enabling. Feedback loops are rejected.
        The returned status=verified confirms routing and unrelated-send readback.
        """
        return _send(
            "routing.set",
            {
                "source_track": source_track,
                "destination_track": destination_track,
                "enabled": enabled,
                "exclusive": exclusive,
            },
        )

    @mcp.tool()
    def fl_set_mixer_send(
        source_track: int,
        destination_track: int,
        level: float = 0.8,
        exclusive: bool = False,
    ) -> dict:
        """Enable a post-fader send and set its normalized level from 0 to 1.

        FL's default level is 0.8. A level of 0 keeps an enabled sidechain-only
        route; disabling the route instead requires fl_set_mixer_route(enabled=False).
        Other sends remain unchanged unless exclusive=True is explicitly requested.
        """
        return _send(
            "routing.set",
            {
                "source_track": source_track,
                "destination_track": destination_track,
                "enabled": True,
                "level": level,
                "exclusive": exclusive,
            },
        )

    @mcp.tool()
    def fl_sidechain_mixer_track(
        source_track: int,
        destination_track: int,
        exclusive: bool = False,
    ) -> dict:
        """Create an enabled sidechain-only send with zero audible send level.

        The destination effect still needs its own sidechain input/detector selected.
        This tool verifies the Mixer route, not the plugin's detector configuration.
        Master and other sends are preserved unless exclusive=True is explicit.
        """
        return _send(
            "routing.set",
            {
                "source_track": source_track,
                "destination_track": destination_track,
                "enabled": True,
                "level": 0.0,
                "exclusive": exclusive,
            },
        )

    @mcp.tool()
    def fl_route_to_bus(
        source_tracks: list[int],
        bus_track: int,
        level: float = 0.8,
        exclusive: bool = False,
    ) -> dict:
        """Route one or more inserts to an existing Mixer bus track as one batch.

        Choose/rename an existing insert as the bus; FL already provides the track
        pool. This does not insert an effect. With exclusive=True sources feed only
        this bus (their direct Master sends are removed). The bus's own routing is
        unchanged. Validation precedes all mutations; failures attempt rollback and
        report whether it completed. All indices are zero-based, Current excluded.
        """
        return _send(
            "routing.bus",
            {
                "source_tracks": source_tracks,
                "destination_track": bus_track,
                "enabled": True,
                "level": level,
                "exclusive": exclusive,
            },
        )
