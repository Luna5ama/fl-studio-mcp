"""MIDI-based connection to FL Studio.

This module provides communication with FL Studio via MIDI messages and JSON files.
The approach is:
1. Write command data to a JSON file
2. Send a MIDI trigger note to FL Studio
3. FL Studio's MIDI controller script executes the command
4. Read the response from a JSON file

This is similar to how the piano_roll module works, but uses MIDI for triggering
instead of keystrokes.
"""

from __future__ import annotations

import json
import os
import platform
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fl_studio_mcp.utils.fl_paths import get_user_data_dir


def _get_fl_hardware_dir() -> Path:
    """Get the FL Studio Hardware scripts directory."""
    system = platform.system()

    if system in ("Darwin", "Windows"):
        base = get_user_data_dir() / "Settings"
    else:
        # Linux fallback
        base = Path.home() / ".fl-studio" / "Settings"

    hardware_dir = base / "Hardware" / "FLStudioMCP"
    hardware_dir.mkdir(parents=True, exist_ok=True)
    return hardware_dir


_mailbox_lock = threading.RLock()


class MIDIConnection:
    """MIDI-based connection to FL Studio.

    Communicates with FL Studio via:
    - JSON files for command/response data
    - MIDI trigger note to execute commands
    """

    # MIDI trigger note (same as in FL Studio controller script)
    TRIGGER_NOTE = 127

    def __init__(self) -> None:
        self._port = None
        self._port_name: str | None = None
        self._connected = False
        self._error: str | None = None
        self._command_lock = _mailbox_lock

        # File paths for JSON communication
        self._hardware_dir = _get_fl_hardware_dir()
        self._command_file = self._hardware_dir / "mcp_command.json"
        self._response_file = self._hardware_dir / "mcp_response.json"

    @property
    def is_connected(self) -> bool:
        """Check if connected to FL Studio."""
        return self._connected and self._port is not None

    @property
    def connection_error(self) -> str | None:
        """Get the last connection error, if any."""
        return self._error

    def connect(self) -> bool:
        """Attempt to connect to FL Studio via MIDI.

        Returns True if connection successful, False otherwise.
        """
        with self._command_lock:
            if self._connected and self._port is not None:
                return True

            try:
                import mido
            except ImportError:
                self._error = (
                    "mido library not installed. Install with: pip install mido python-rtmidi"
                )
                return False

            # Find available MIDI output ports
            try:
                output_ports = mido.get_output_names()
            except Exception as e:
                self._error = f"Failed to get MIDI ports: {e}"
                return False

            if not output_ports:
                self._error = (
                    "No MIDI output ports found. On Mac, enable IAC Driver in Audio MIDI Setup."
                )
                return False

            configured = os.environ.get("FL_STUDIO_MIDI_PORT")
            if configured:
                target_port = configured if configured in output_ports else None
            else:
                candidates = [
                    name
                    for name in output_ports
                    if any(token in name.upper() for token in ("IAC", "LOOPMIDI", "FL"))
                ]
                target_port = candidates[0] if len(candidates) == 1 else None
            if target_port is None:
                self._error = (
                    "No unambiguous FL virtual MIDI output. Start loopMIDI and enable its input "
                    "in FL, or set FL_STUDIO_MIDI_PORT to the exact output port name. "
                    f"Available ports: {output_ports}"
                )
                return False

            try:
                self._port = mido.open_output(target_port)
                self._port_name = target_port
                self._connected = True
                self._error = None
                return True
            except Exception as e:
                self._error = f"Failed to open MIDI port '{target_port}': {e}"
                return False

    def disconnect(self) -> None:
        """Close the MIDI connection."""
        with self._command_lock:
            if self._port is not None:
                try:
                    self._port.close()
                except Exception:
                    pass
                self._port = None
            self._connected = False
            self._port_name = None

    def ensure_connected(self) -> None:
        """Ensure connection to FL Studio is active. Raises RuntimeError if not."""
        if not self.is_connected:
            if not self.connect():
                raise RuntimeError(self._error or "Failed to connect to FL Studio via MIDI")

    @contextmanager
    def transaction(self):
        """Serialize a complete multi-command/UI operation within this server."""
        with self._command_lock:
            yield self

    def send_command(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        """Send a command to FL Studio and wait for response.

        Args:
            action: The command action (e.g., "transport.start", "mixer.setTrackVolume")
            params: Optional parameters for the command
            timeout: Maximum time to wait for response in seconds

        Returns:
            Response dictionary from FL Studio

        Raises:
            RuntimeError: If not connected or command fails
        """
        # The controller has one command/response mailbox, shared by all MCP tools.
        with self._command_lock:
            return self._send_command(action, params, timeout)

    def _send_command(
        self, action: str, params: dict[str, Any] | None, timeout: float
    ) -> dict[str, Any]:
        self.ensure_connected()

        # Prepare command
        command = {
            "action": action,
            "params": params or {},
            "request_id": uuid.uuid4().hex,
        }

        # Write command to file
        try:
            temporary = self._command_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(command, indent=2), encoding="utf-8")
            temporary.replace(self._command_file)
        except Exception as e:
            return {"success": False, "error": f"Failed to write command file: {e}"}

        # Clear old response file
        if self._response_file.exists():
            try:
                self._response_file.unlink()
            except Exception:
                pass

        # Send MIDI trigger
        try:
            import mido

            trigger_msg = mido.Message("note_on", note=self.TRIGGER_NOTE, velocity=127)
            self._port.send(trigger_msg)
        except Exception as e:
            return {"success": False, "error": f"Failed to send MIDI trigger: {e}"}

        # Wait for response
        return self._wait_for_response(timeout, command["request_id"])

    def _wait_for_response(self, timeout: float, request_id: str | None = None) -> dict[str, Any]:
        """Wait for response file to appear and read it.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            Response dictionary or error dict if timeout
        """
        start_time = time.monotonic()
        poll_interval = 0.02  # 20ms between checks

        while time.monotonic() - start_time < timeout:
            if self._response_file.exists():
                try:
                    response_text = self._response_file.read_text(encoding="utf-8")
                    response = json.loads(response_text)

                    # Missing ids remain compatible with older controller versions.
                    if (
                        request_id is not None
                        and response.get("request_id") is not None
                        and response["request_id"] != request_id
                    ):
                        self._response_file.unlink(missing_ok=True)
                        continue

                    # Clean up response file
                    try:
                        self._response_file.unlink()
                    except Exception:
                        pass

                    return response
                except json.JSONDecodeError:
                    # FL's sandbox requires in-place writes; wait out partial JSON.
                    pass
                except Exception as e:
                    return {"success": False, "error": f"Failed to read response: {e}"}

            time.sleep(poll_interval)

        return {
            "success": False,
            "error": (
                f"Timeout waiting for FL Studio response after {timeout}s. "
                "Make sure FL Studio is running and the MCP controller is enabled "
                "in MIDI Settings."
            ),
        }

    def get_status(self) -> dict[str, Any]:
        """Get connection status information."""
        try:
            import mido

            output_ports = mido.get_output_names()
        except Exception:
            output_ports = []

        return {
            "connected": self.is_connected,
            "port_name": self._port_name,
            "available_ports": output_ports,
            "command_file": str(self._command_file),
            "response_file": str(self._response_file),
            "error": self._error,
        }


# Global connection instance
_connection: MIDIConnection | None = None


def get_connection() -> MIDIConnection:
    """Get the global MIDI connection instance."""
    global _connection
    if _connection is None:
        _connection = MIDIConnection()
    return _connection


def reset_connection() -> None:
    """Reset the connection state to allow reconnection attempts."""
    global _connection
    with _mailbox_lock:
        if _connection is not None:
            _connection.disconnect()
            _connection = None
