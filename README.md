# FL Studio MCP Server

**Control FL Studio with plain language.** This fork connects MCP clients to FL Studio through a MIDI controller, Piano Roll scripts, and Windows native UI automation. Compose notes, manage plugins and patterns, route Mixer tracks, import samples, save projects, and render WAV files. It does not require a Computer Use tool or plugin; native operations can foreground FL Studio and use local OCR, mouse clicks, and keystrokes.

[![Listed on mcpservers.org](https://mcpservers.org/badge.svg)](https://mcpservers.org/servers/karl-andres/fl-studio-mcp)
[![Listed on awesome-mcp-servers](https://img.shields.io/badge/awesome--mcp--servers-listed-blue)](https://github.com/punkpeye/awesome-mcp-servers)
[![GitHub stars](https://img.shields.io/github/stars/karl-andres/fl-studio-mcp?style=flat)](https://github.com/karl-andres/fl-studio-mcp/stargazers)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org)

---

## Table of Contents

- [Demo](#demo)
- [Features](#features)
- [Limitations](#limitations)
- [Requirements](#requirements)
- [Quick Installation](#quick-installation)
- [Manual Installation](#manual-installation)
- [Which AI Clients Work With This?](#which-ai-clients-work-with-this)
- [Usage](#usage)
- [Available Tools](#available-tools)
- [Example Workflows](#example-workflows)
- [Troubleshooting](#troubleshooting)
- [Architecture](#architecture)
- [Development](#development)
- [Credits](#credits)

---

## Demo

Ask Claude to analyze your project and compose a melody — notes appear in the Piano Roll in real time:

![Claude composing a dark bell melody in FL Studio via MCP](assets/demo.gif)

dont mind the scuffed audio, i had to clip with my mic bc apple wouldnt let me record desktop audio lol

https://github.com/user-attachments/assets/d4fc668f-9fe5-4ab4-9f18-76cd661029c6

Hear the original audio if you'd like

https://github.com/user-attachments/assets/c2b1a5e7-1640-41fa-82bc-18ca7cbae9e8

---

## Features

### Transport Control

- Play, pause, stop playback
- Toggle recording
- Set playback position
- Get song length and position
- Control loop mode (pattern/song)
- Adjust playback speed

### Mixer Control

- Get/set track volume and pan
- Mute/solo tracks
- Arm tracks for recording
- Set track names and colors
- Stereo separation control
- Inspect and edit sends, buses, and sidechain-only routes with host readback

### Channel Rack Control

- List all channels
- Get/set channel properties (volume, pan, name, color)
- Mute/solo channels
- Route channels to mixer tracks
- Trigger MIDI notes in real-time
- Step sequencer control (get/set grid bits)

### Plugin Control

- Discover scanned plugins, including distinct VST/VST3 variants
- Add an instrument in a new Channel Rack channel or an effect on a specified Mixer track
- Verify the resulting channel or effect slot asynchronously
- List plugin parameters
- Get/set parameter values
- Navigate presets (next/previous)
- Query plugin info and inventory the project
- Enable/bypass, replace, delete, copy, or move explicitly identified instances
- Save/load complete native FST presets; published host state is checked afterward

### Piano Roll Control

- **Add notes** to the piano roll with precise timing
- **Add chords** with a single command
- **Delete specific notes** by MIDI number and time
- **Clear all notes** from the piano roll
- **Read piano roll state** to see all existing notes
- Auto-triggering via keystroke (Cmd+Opt+Y on macOS, Ctrl+Alt+Y on Windows)

### Patterns and Playlist

- Create, select, rename, clone, and delete patterns
- Read/set the BPM displayed by FL Studio; report quarter-note BPM separately
- Read/set global time signature, with feature-specific MIDI API requirements
- Inspect Playlist tracks and readable visible clip labels
- Place pattern or Audio Clip sources, move clips by whole bars/tracks, and remove occurrences
- Verify selected clip start/end PPQ ticks after native edits

### Project Files and Diagnostics

- Import audio into a new Sampler or Audio Clip channel, with optional name and Mixer route
- Save As to an explicit absolute `.flp` path and verify the completed file and clean project flag
- Render WAV to a private staging directory, verify complete audio frames and render completion, then publish the requested path
- Check dependencies, controller installation, MIDI handshake, capabilities, and project summary
- Inspect loading operations and request cancellation before activation

---

## Limitations

**Windows native operations.** Plugin loading and lifecycle operations, native FST files,
Playlist clip edits, sample import, project saving, and WAV rendering require Windows
10/11, an English FL Studio UI, Windows English OCR, and a connected controller.
No Computer Use tool is required. The server itself foregrounds FL Studio, reads local
screenshots, and sends mouse/keyboard input. Keep FL untouched while an operation runs,
keep relevant controls visible and unobstructed, and run one server per FL instance.
Screenshots used for OCR are not uploaded or saved. Original MIDI/Piano Roll tools
retain their existing macOS path; the new Windows UI workflows have no macOS implementation.

**Plugin targets and state.** `fl_add_plugin` uses already scanned Browser entries:
instruments open in a new channel; effects go to an empty slot on the requested Mixer
track. Keep Browser search clear, the Browser readable, and the Mixer inspector visible.
Plugin loading requires MIDI API 34+. Installation and scanning remain manual.
Existing instances can be replaced with
`fl_replace_plugin` or explicitly authorized FST loading. Refresh inventory and supply
`expected_plugin` before destructive edits. Native FST files include private plugin state,
but independent verification covers plugin identity and named published parameters;
unnamed parameter differences are reported and private state is not independently
verified. FST staging preserves the normal display-name leaf inside an isolated
directory; names must be legal complete Windows basenames. Invalid names are rejected
before navigation or activation. Same-track effect moves reorder natively.
Cross-track moves recreate the instance from FST and require `allow_recreate=True`: existing automation links are not preserved. Copies retain
the source and do not clone its automation links.

**Playlist coverage.** MIDI does not enumerate ordinary Playlist clips. Native inspection
covers readable labels in the visible viewport; hidden, stacked, truncated, or offscreen
clips may be omitted. Use unique fully readable track/source names and at least
three readable bar numbers. Default numeric track names can need renaming if OCR
is ambiguous. FL offers quick horizontal zoom presets with Shift+1/2/3 in the
[official Playlist manual](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/playlist.htm).
Disable clip grouping for individual edits. Placement and movement use whole-bar
positions, set local snap to Bar, and change clip/time selections. Overlap checks cover
visible labels only. Channel/Mixer/slot indices are zero-based; pattern, Playlist track,
and bar indices are one-based. Explicit pattern length needs API 39+, explicit clone
needs API 43+, tempo/time-signature setting API 38+, routing API 36+, and effect mix API 37+.
Time-signature numerator/denominator have no independent public getters and are
reported as unverified. Pattern lengths use quarter-note beats: on the tested Windows
API 45 host, 16 native steps equal 4 quarter-note beats.

**Tempo units.** Public `bpm` values and `fl_set_tempo` inputs use the BPM displayed in
FL's toolbar. `quarter_bpm` reports the independent MIDI API reading;
`ui_to_quarter_ratio` relates the two. For example, a displayed 140 BPM in 6/8 read
back as `quarter_bpm: 70` and ratio 2. The getter checks a visible toolbar value and
its native Tempo hint; unreadable or obstructed controls and foreground loss cause
an explicit failure. The complete displayed-BPM read/write cycle is recorded in
the validation record.

**Project files.** Saving requires an explicit absolute `.flp` filename and changes the
current project filename; no pathless save claims knowledge of the current disk path.
Existing outputs require `overwrite=True`. Import creates a new channel and returns its
actual index/name/type/route; an uncertain failure can leave that channel behind. A source
stem equal to an empty channel name (`Sampler` or `Audio Clip`) must be renamed so a label
change can prove loading. Import verification cannot independently read a plugin's private
source-file path.

**WAV export.** Audio is rendered inside a unique `.mcp-render-*` directory under the
output parent, so additional selected formats or split stems cannot overwrite sibling
user files. Only the exact verified primary WAV is published. If FL produces no primary
WAV, the tool reports failure and keeps staging for inspection. Actual sample rate,
bit depth, channels, and frame count come from the WAV header; global Audio Settings are
not changed to force a sample rate. `full_song` clears the Playlist time selection and
reports before/after bounds; it does not restore that selection. Song/Pattern mode is
restored on success and failure, with the restoration result reported. Playback remains
stopped. Standard RIFF/WAVE is supported; RF64/Wave64 outputs are not accepted.

**Uncertain results.** A timeout or cancellation does not undo an activation already
issued. Inspect the project and query operation status before retrying. A queued request,
key press, or newly appearing file alone is never reported as completed work.

---

## Requirements

- **FL Studio 20.7+** for the original MIDI tools; newer APIs are required for fork extensions (see limitations and `fl_check_environment`)
- **Python 3.10+**
- **Windows 10/11** for native workflows; **macOS** for original MIDI/Piano Roll tools
  - macOS: IAC Driver (built-in, needs to be enabled)
  - Windows: [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html)

---

## Quick Installation

### macOS / Linux

```bash
git clone https://github.com/Luna5ama/fl-studio-mcp.git
cd fl-studio-mcp
./install.sh
```

### Windows

```powershell
git clone https://github.com/Luna5ama/fl-studio-mcp.git
cd fl-studio-mcp

# Allow running local scripts for this user (one-time)
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force

.\install.ps1
```

`install.ps1` is the Windows counterpart to `install.sh` — same steps, PowerShell instead of bash. It installs Python 3.12 specifically (required for prebuilt `python-rtmidi` wheels on Windows).

Both installers will:

1. Install [uv](https://github.com/astral-sh/uv) if not present
2. Install Python dependencies
3. Guide you through enabling virtual MIDI ports (IAC Driver on Mac, loopMIDI on Windows)
4. Install the FL Studio MIDI controller script
5. Install the Piano Roll script (ComposeWithLLM)
6. Configure Claude Desktop or Claude Code automatically (`scripts/install_mcp_for_claude.sh` / `scripts/install_mcp_for_claude.ps1`)

> **Windows piano-roll note:** The Piano Roll script is launched by sending FL Studio a keystroke (`Ctrl+Alt+Y`). On Windows this briefly foregrounds the FL Studio window, so **FL Studio will pop to the front for a moment** each time a tool like `fl_send_notes` runs. This is expected. See [Piano Roll script not triggering](#piano-roll-script-not-triggering) if it doesn't fire at all.

---

## Manual Installation

### 1. Install Python Dependencies

```bash
# Using uv (recommended)
uv sync

# Or using pip
pip install -e .
```

### 2. Enable Virtual MIDI Ports

#### macOS (IAC Driver)

1. Open **Audio MIDI Setup** (search in Spotlight)
2. Press **Cmd+2** or go to **Window > Show MIDI Studio**
3. Double-click on **IAC Driver**
4. Check **"Device is online"**
5. Click **Apply**

#### Windows (loopMIDI)

1. Download and install [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html)
2. Create a virtual port with a recognizable name such as `FLStudioMCP`
3. Keep loopMIDI running while using FL Studio

Without configuration, the server selects a MIDI output only when exactly one name
contains `FL`, `IAC`, or `loopMIDI` (case-insensitive). If your port has another name or
multiple candidates exist, set `FL_STUDIO_MIDI_PORT` in the MCP server environment to
the exact output-port name reported by `fl_connection_status` / `fl_check_environment`.
Opening a MIDI output does not verify that FL received a command; check the controller
handshake with `fl_check_environment`.

### 3. Install FL Studio Scripts

Install the controller with automatic user-data-directory detection:

```bash
uv run python scripts/install_controller.py
```

This detects relocated Windows user data from Image-Line's registry settings and
bakes that path into both the controller and Piano Roll script, since FL's embedded Python restricts registry
access. Existing controllers are backed up before replacement. For another location,
set `FL_STUDIO_USER_DATA_DIR` to the **FL Studio folder itself** (containing
`Settings` and `Presets`) in the MCP server's environment, and run the installer
with `--user-data-dir /path/to/FL\ Studio`. After updating, select the MIDI input in
FL's MIDI Settings, change **Controller type** to a generic controller, then select
**FL Studio MCP Controller** again. **Refresh device list** does not reload its Python
modules. Restart the MCP server too. The same folder must be used by the server and controller.

The following manual-copy commands also work for the default user-data location:

Copy the main controller **and every `mcp_*.py` companion module** into the same
Hardware folder. Copying only `device_FLStudioMCP.py` omits the fork extensions.
The automatic installer above is preferred for relocated user-data folders.

macOS (default location):

```bash
mkdir -p ~/Documents/Image-Line/FL\ Studio/Settings/Hardware/FLStudioMCP
cp fl_controller/device_FLStudioMCP.py fl_controller/mcp_*.py ~/Documents/Image-Line/FL\ Studio/Settings/Hardware/FLStudioMCP/
cp scripts/ComposeWithLLM.pyscript ~/Documents/Image-Line/FL\ Studio/Settings/Piano\ roll\ scripts/
```

Windows PowerShell (default location):

```powershell
$flHardware = Join-Path $env:USERPROFILE 'Documents\Image-Line\FL Studio\Settings\Hardware\FLStudioMCP'
New-Item -ItemType Directory -Force -Path $flHardware
Copy-Item fl_controller\device_FLStudioMCP.py, fl_controller\mcp_*.py -Destination $flHardware
$flPianoRoll = Join-Path $env:USERPROFILE 'Documents\Image-Line\FL Studio\Settings\Piano roll scripts'
New-Item -ItemType Directory -Force -Path $flPianoRoll
Copy-Item scripts\ComposeWithLLM.pyscript -Destination $flPianoRoll
```

### 4. Configure FL Studio

1. **Restart FL Studio** (if it's running)
2. Go to **Options > MIDI Settings**
3. Under **Input**, find your virtual MIDI port (e.g., "IAC Driver Bus 1")
4. Set the **Controller type** to **FL Studio MCP Controller** and enable the input
5. Assign an unused input Port number (for example, 10). Keep loopMIDI running on Windows.
   After updating scripts, switch **Controller type** to a generic controller and then
   back to **FL Studio MCP Controller**. **Refresh device list** does not reload scripts.

### 5. Configure Your MCP Client

Add to your Claude Desktop config:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "fl-studio": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/fl-studio-mcp", "fl-studio-mcp"]
    }
  }
}
```

Optional environment variables go in the server entry's `env` object: `FL_STUDIO_MIDI_PORT`
selects an exact MIDI output name; `FL_STUDIO_USER_DATA_DIR` selects the FL Studio
user-data folder (also used when installing the controller).

For Claude Code, add to your MCP settings (`~/.claude.json`, or run `claude mcp add`).

---

## Which AI Clients Work With This?

This is a standard [MCP](https://modelcontextprotocol.io) server that talks to clients over stdio — it isn't hardcoded to any one AI vendor. In principle, **any MCP-compatible client can connect**: Claude Desktop, Claude Code, Cursor, Windsurf, Gemini CLI, OpenAI's Codex CLI/Agents SDK, etc.

**What's actually tested and auto-configured:** only **Claude Desktop** and **Claude Code**, via `scripts/install_mcp_for_claude.sh` (macOS/Linux) and `scripts/install_mcp_for_claude.ps1` (Windows). Other clients have no installer support — you'd need to manually add the following MCP server entry to that client's own config:

```json
{
  "command": "uv",
  "args": ["run", "--directory", "/path/to/fl-studio-mcp", "fl-studio-mcp"]
}
```

Replace `/path/to/fl-studio-mcp` with your local clone path. If you try this with a non-Claude client, please open an issue with what worked or didn't — the protocol should support it, but it hasn't been verified here.

---

## Usage

### Running the Server Manually

```bash
# Using uv
uv run fl-studio-mcp

# Or after installation
fl-studio-mcp
```

### Piano Roll Workflow

1. Open FL Studio and select a channel
2. Open the Piano Roll (F7 or double-click the channel)
3. The first time, manually run the script: **Tools > Scripting > ComposeWithLLM**
4. After that, the MCP tools will auto-trigger the script

---

## Available Tools

### Connection

| Tool | Description |
|------|-------------|
| `fl_connect` | Connect/reconnect to FL Studio |
| `fl_connection_status` | Get connection status and available MIDI outputs |
| `fl_check_environment` | Check dependencies, installed controller, MIDI handshake, and feature capabilities |
| `fl_get_project_summary` | Read current project metadata, tempo, and object counts |
| `fl_get_load_operations` | List plugin-load operations from this server session |
| `fl_cancel_plugin_load` | Request cancellation; issued activations are not rolled back |

### Transport

| Tool | Description |
|------|-------------|
| `fl_play` | Start/pause playback |
| `fl_stop` | Stop playback |
| `fl_record` | Toggle recording |
| `fl_get_transport_status` | Get playback/recording state |
| `fl_set_song_position` | Set playback position |
| `fl_get_song_length` | Get song duration |
| `fl_set_loop_mode` | Switch between pattern/song mode |
| `fl_set_playback_speed` | Adjust playback speed (0.25x–4x) |

### Mixer

| Tool | Description |
|------|-------------|
| `fl_get_mixer_track_count` | Get number of mixer tracks |
| `fl_get_mixer_track_info` | Get track details |
| `fl_get_all_mixer_tracks` | List all tracks |
| `fl_set_track_volume` | Set track volume |
| `fl_set_track_pan` | Set track pan |
| `fl_mute_track` | Mute/unmute track |
| `fl_solo_track` | Solo/unsolo track |
| `fl_arm_track` | Arm track for recording |
| `fl_set_track_name` | Rename track |
| `fl_set_track_color` | Set track color |
| `fl_set_stereo_separation` | Adjust stereo width |

### Channels

| Tool | Description |
|------|-------------|
| `fl_get_channel_count` | Get number of channels |
| `fl_get_channel_info` | Get channel details |
| `fl_get_all_channels` | List all channels |
| `fl_get_selected_channel` | Get selected channel |
| `fl_select_channel` | Select/deselect channel |
| `fl_select_one_channel` | Select channel exclusively |
| `fl_trigger_note` | Trigger MIDI note (real-time) |
| `fl_set_channel_volume` | Set channel volume |
| `fl_set_channel_pan` | Set channel pan |
| `fl_mute_channel` | Mute/unmute channel |
| `fl_solo_channel` | Solo/unsolo channel |
| `fl_set_channel_name` | Rename channel |
| `fl_set_channel_color` | Set channel color |
| `fl_route_channel_to_mixer` | Route to mixer track |
| `fl_get_grid_bit` | Get step sequencer step |
| `fl_set_grid_bit` | Set step sequencer step |
| `fl_get_step_sequence` | Get full pattern |
| `fl_set_step_sequence` | Set full pattern |

### Plugins

| Tool | Description |
|------|-------------|
| `fl_list_available_plugins` | Search scanned plugins and return exact catalog IDs |
| `fl_add_plugin` | Add a new instrument channel or an effect on a specified Mixer track |
| `fl_get_plugin_load_status` | Inspect the most recent load after an uncertain timeout |
| `fl_is_plugin_valid` | Check if plugin exists |
| `fl_get_plugin_name` | Get plugin name |
| `fl_get_plugin_param_count` | Get parameter count |
| `fl_get_plugin_params` | List all parameters |
| `fl_get_plugin_param_value` | Get parameter value |
| `fl_set_plugin_param_value` | Set parameter value |
| `fl_get_preset_count` | Get preset count |
| `fl_next_preset` | Next preset |
| `fl_prev_preset` | Previous preset |
| `fl_get_plugin_color` | Get plugin color |

### Plugin Instances and FST Presets

| Tool | Description |
|------|-------------|
| `fl_get_project_plugins` | Inventory instrument channels and Mixer effect instances |
| `fl_get_plugin_instance` | Read an explicit instance and published parameter fingerprint |
| `fl_set_effect_mix` | Set and verify a slot's host dry/wet mix |
| `fl_set_plugin_enabled` | Mute/unmute an instrument or bypass/enable an effect |
| `fl_delete_plugin` | Delete an instance guarded by its exact `expected_plugin` identity |
| `fl_replace_plugin` | Replace a named instance using an exact scanned catalog entry |
| `fl_list_plugin_presets` | Find native `.fst` files in user preset folders |
| `fl_save_plugin_preset` | Save complete native FST state to an explicit path |
| `fl_load_plugin_preset` | Load FST into an explicit channel or effect slot; occupied targets require authorization |
| `fl_list_plugin_preset_operations` | List this session's FST load IDs and history |
| `fl_get_plugin_preset_status` | Inspect a pending/uncertain native FST load |
| `fl_cancel_plugin_preset_load` | Request cancellation before native FST activation |
| `fl_move_effect` | Reorder within a track or explicitly recreate on another track |
| `fl_copy_effect` | Copy complete FST state into an empty effect slot |

### Mixer Routing

| Tool | Description |
|------|-------------|
| `fl_get_mixer_routing` | Read enabled routes/send levels, or include disabled routes |
| `fl_set_mixer_route` | Enable/disable a route, optionally disconnecting other sends |
| `fl_set_mixer_send` | Enable a send and set its normalized level |
| `fl_sidechain_mixer_track` | Enable a route with zero audible send level |
| `fl_route_to_bus` | Route a batch of inserts to an existing bus track |

Routes preserve other sends unless `exclusive=True` is explicit. Feedback loops are
rejected. Sidechain routing does not select a plugin's internal sidechain detector.

### Patterns, Timing, and Playlist

| Tool | Description |
|------|-------------|
| `fl_get_patterns` | Read existing patterns and active pattern |
| `fl_create_pattern` | Create/name an unused pattern and select it |
| `fl_select_pattern` | Select the pattern for subsequent notes/steps |
| `fl_rename_pattern` | Rename a pattern with readback |
| `fl_clone_pattern` | Clone pattern data into an unused destination |
| `fl_delete_pattern` | Delete a source pattern and affected occurrences with identity guard and confirmation |
| `fl_get_timing` | Read displayed BPM, independent quarter-note BPM, PPQ, and global bar length |
| `fl_set_tempo` | Set the BPM shown by FL Studio and verify display/API readback |
| `fl_set_time_signature` | Set global time signature; denominator readback is unavailable |
| `fl_get_playlist` | Read Playlist track metadata and timeline selection |
| `fl_set_playlist_track` | Rename or mute a Playlist track |
| `fl_get_visible_playlist_clips` | Inspect readable visible labels and approximate bar positions |
| `fl_get_playlist_clip` | Select one named visible clip and read exact PPQ bounds |
| `fl_place_pattern_clip` | Place a pattern at a whole-bar boundary |
| `fl_place_audio_clip` | Place an existing Audio Clip channel at a whole-bar boundary |
| `fl_move_playlist_clip` | Move a visible clip by whole bars/tracks |
| `fl_delete_playlist_clip` | Remove one visible occurrence while retaining its source |
| `fl_clear_playlist_time_selection` | Clear the time range with native ruler action and MIDI readback |

### Sample Import and Project Files

| Tool | Description |
|------|-------------|
| `fl_import_sample` | Create a Sampler or Audio Clip channel from an existing absolute audio path |
| `fl_save_project` | Save As to an explicit absolute `.flp` path; overwrite is opt-in |
| `fl_export_wav` | Render full song, current time selection, or active pattern and verify actual WAV output |

### Piano Roll

| Tool | Description |
|------|-------------|
| `fl_send_notes` | Add notes to the piano roll |
| `fl_send_chord` | Add a chord (multiple notes at same time) |
| `fl_delete_notes` | Delete specific notes |
| `fl_clear_piano_roll` | Clear all notes |
| `fl_get_piano_roll_state` | Read current piano roll notes |
| `fl_trigger_script` | Manually trigger the FL Studio script |
| `fl_get_piano_roll_info` | Get piano roll system info |
| `fl_clear_request_queue` | Cancel pending queued changes |

---

## Example Workflows

### Adjusting a Mix

```text
"Set the volume of mixer track 1 to 80% and pan it slightly left"
```

### Creating a Drum Pattern

```text
"Create a basic kick pattern on channel 0 with kicks on steps 0, 4, 8, and 12"
```

### Adding a Melody to Piano Roll

```text
"Add a C major arpeggio starting at beat 0: C4, E4, G4, C5 - each note quarter duration"
```

### Adding Chords

```text
"Add a C major chord at beat 0, then F major at beat 2, then G major at beat 4"
```

### Automating Plugin Parameters

```text
"List the parameters of the plugin on channel 0 and set the filter cutoff to 50%"
```

### Adding a VST instrument or effect

First call `fl_list_available_plugins(query="Speldosa", kind="generator")`.
Use an exact returned ID when VST and VST3 variants share a name:

```json
{"plugin": "Installed/Generators/VST3/Speldosa.fst", "kind": "generator"}
```

For an effect, specify the destination track explicitly:

```json
{"plugin": "Installed/Effects/VST3/Correlometer.fst", "kind": "effect", "mixer_track": 1}
```

Pass these arguments to `fl_add_plugin`. A successful result has `status: "loaded"`
and the actual `channel_index`, or `mixer_track` and `slot_index`. A timeout is
uncertain: a slow plugin may finish opening later. Query `fl_get_plugin_load_status`
with the returned `operation_id` and inspect the project before retrying.

### Importing and arranging audio, then saving

1. Call `fl_import_sample` with an existing absolute audio path and
   `channel_type="audio_clip"`; use the returned `channel.index` as `source_channel`.
2. Keep that source visible in the Audio Clips Picker tab, then call
   `fl_place_audio_clip(source_channel=..., track=1, start_bar=1)`.
3. Call `fl_save_project` with an explicit absolute `.flp` path. Existing files
   require `overwrite=True`.
4. Call `fl_export_wav` with an explicit absolute `.wav` path and choose
   `full_song`, `current_selection`, or `pattern`. Inspect `sample_rate`,
   `bits_per_sample`, `channels`, `frames`, selection changes, and mode restoration.

A Sampler import is intended for note/step playback; import as `audio_clip` when
placing the audio file directly into Playlist. Refresh object indices after changes.

---

## Troubleshooting

### Extension tools unavailable or native UI actions fail

Run `fl_check_environment` and check `communication_verified`, controller modules,
MIDI API capabilities, and English OCR availability. Reinstall the main controller
and all `mcp_*.py` files, then switch the input's controller type to a generic
controller and back to **FL Studio MCP Controller**. Refreshing the device list alone
does not reload the installed modules. Restart the MCP server after source updates.
`fl_connect` verifies the versioned controller handshake; a MIDI output merely being
open is insufficient. For native workflows keep the English Browser/Mixer/Playlist
controls readable, close unrelated modal dialogs, and inspect any uncertain result
before another attempt.

### "Not connected to FL Studio"

1. Ensure FL Studio is running
2. Check that the FLStudioMCP controller is enabled in MIDI Settings
3. On Mac, verify IAC Driver is enabled in Audio MIDI Setup
4. On Windows, verify loopMIDI is running
5. Restart FL Studio after enabling the controller

### "Timeout waiting for FL Studio response"

1. Make sure FL Studio is in focus
2. Check the Script output window in FL Studio (**View > Script output**)
3. Verify the controller is receiving MIDI (look for activity in MIDI Settings)

### Piano Roll script not triggering

1. First time: manually run **Tools > Scripting > ComposeWithLLM** in FL Studio
2. On macOS: grant Accessibility permissions when prompted
3. On Windows: the MCP server foregrounds the FL Studio window automatically before sending the hotkey — if FL Studio isn't running or is minimized to the system tray, the trigger can't find it and will fall back to a warning telling you to press the hotkey manually
4. Try pressing Cmd+Opt+Y (macOS) or Ctrl+Alt+Y (Windows) manually to confirm the hotkey is bound to the script in FL Studio
5. If you just updated the server code (e.g. pulled a fix), **fully restart** your MCP client (Claude Desktop/Code) — reconnecting the MCP server alone does not respawn the underlying process

### No MIDI ports available

- **macOS**: Enable IAC Driver in Audio MIDI Setup
- **Windows**: Install and run loopMIDI

---

## Architecture

This MCP server uses a hybrid approach:

```
┌─────────────────┐     ┌─────────────────────────────────────────┐
│   MCP Client    │────▶│           FastMCP Server                │
│  (Claude, etc)  │     │                                         │
└─────────────────┘     │  ┌─────────────────┐  ┌──────────────┐  │
                        │  │ MIDI + JSON     │  │ Piano Roll   │  │
                        │  │ Tools           │  │ Tools (JSON) │  │
                        │  └────────┬────────┘  └──────┬───────┘  │
                        └───────────┼──────────────────┼──────────┘
                                    │                  │
                               MIDI + JSON        JSON Files +
                                    │              Keystroke
                                    ▼                  ▼
                        ┌─────────────────────────────────────────┐
                        │              FL Studio                   │
                        │  ┌──────────────┐  ┌──────────────────┐ │
                        │  │FLStudioMCP   │  │ Piano Roll Script│ │
                        │  │(MIDI Ctrl)   │  │ (ComposeWithLLM) │ │
                        │  └──────────────┘  └──────────────────┘ │
                        └─────────────────────────────────────────┘
```

### How It Works

**Transport / Mixer / Channels / Plugins:**
1. MCP server writes command to JSON file
2. Sends MIDI trigger note to FL Studio
3. FL Studio controller script reads JSON, executes API, writes response
4. MCP server reads response

**Piano Roll:**
1. MCP server writes note requests to JSON file
2. Sends keystroke (Cmd+Opt+Y on macOS, Ctrl+Alt+Y on Windows) to trigger FL Studio script — on Windows, the FL Studio window is foregrounded first so the keystroke actually reaches it
3. Piano Roll script reads JSON and modifies notes

---

## Development

### Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) (recommended)

### Setup

```bash
# Install all dependencies including dev extras
uv sync --dev

# Or with pip
pip install -e ".[dev]"
```

### Available Commands

| Command | Description |
|---------|-------------|
| `uv run fl-studio-mcp` | Run the MCP server |
| `uv run ruff check .` | Lint the codebase |
| `uv run ruff check --fix .` | Lint and auto-fix |
| `uv run pytest` | Run tests |

### Validation Status

Runtime acceptance on Windows is recorded separately from automated boundary tests.
The following cases passed through production MCP tools in FL Studio 26.1.7 build
5654, MIDI API 45, on 2026-10-30:

| Case | Verified result |
| --- | --- |
| VST3 loading | Speldosa channel 6 routed to Mixer 6; Correlometer on Mixer 6, slot 0 |
| Plugin lifecycle | Effect wet 0.37, instrument enable/disable, effect bypass/enable, native same-track move 0 → 2, and effect/instrument deletion |
| Instrument FST | Native save/load restored Speldosa Room 0 → 1 → 0; all 150 named published parameters matched, with all 4,240 parameter records retained |
| Effect FST | Correlometer Bypass 0 → 1 → 0; all 145 named parameters matched |
| Cross-track copy/move | Verified FST destination and wet/bypass; copy retained source, move deleted source only after verification; automation links are not preserved |
| Replacement/cancellation | Correlometer → Fruity Balance verified; cancellation before activation left its target empty; after acknowledged native drop it returned in 31 ms and observed the actual completed load |
| Sample import | Complete single-call Audio Clip and Sampler imports passed; names/types/routes and prior channel identities read back |
| Project save | Final `MCP Windows Validated 20261030.flp`: 62,509 bytes, header channel count 8, PPQ 96, clean project flag |
| WAV export | Full-song 17.642857 s and selected range 1.714286 s; independent audio analysis confirmed both were non-silent |
| Pattern/Audio Playlist | Public placement, exact tick readback, cross-track move and verified deletion; 16 native Pattern steps equal 4 quarter-note beats |
| Displayed BPM read/write | Full public 6/8 cycle: display 140 → 160 → 140 / quarter 70 → 80 → 70; restored 4/4 display/quarter 140 |

Normal display names through FST staging and both Picker source types passed.
The follow-up completed public instrument deletion, cancellation after actual
native drop, and a single-call Sampler import. The uniquely named Sampler E2E
fixture is retained in the final eight-channel test project. Cancellation is an
acknowledgement, not an undo; a completed operation rejects further cancellation.
Unnamed/private plugin state and hidden sample content/path have not been
independently accepted. See [the Windows validation record](docs/windows-validation.md).

The final full suite passed **512 tests**, with Ruff clean. Native workflows still
require readable English labels, a visible unobstructed window, and inspection of
any uncertain result before repeating an activation.

### Project Structure

```
fl-studio-mcp/
├── fl_controller/
│   ├── device_FLStudioMCP.py     # Main MIDI controller (runs inside FL Studio)
│   └── mcp_*.py                  # Required feature modules installed alongside it
├── scripts/
│   ├── install_controller.py    # Install controller/modules + baked Piano Roll script
│   ├── setup.sh                  # FL Studio script installer
│   ├── install_mcp_for_claude.sh # Claude config installer (macOS/Linux)
│   ├── install_mcp_for_claude.ps1# Claude config installer (Windows)
│   └── ComposeWithLLM.pyscript   # Piano Roll script (runs inside FL Studio)
├── src/fl_studio_mcp/
│   ├── server.py                 # FastMCP server entry point
│   ├── tools/                    # MCP tool implementations
│   │   ├── arrangement.py
│   │   ├── diagnostics.py
│   │   ├── plugin_management.py
│   │   ├── project_files.py
│   │   ├── routing.py
│   │   ├── channels.py
│   │   ├── mixer.py
│   │   ├── piano_roll.py
│   │   ├── plugins.py
│   │   └── transport.py
│   └── utils/
│       ├── connection.py         # FL Studio connection wrapper
│       ├── fl_trigger.py         # Piano roll keystroke trigger
│       └── midi_connection.py    # MIDI + JSON communication layer
├── install.sh                    # One-command installer (macOS/Linux)
└── install.ps1                   # One-command installer (Windows)
```

---

## Credits

- [FL Studio API Stubs](https://github.com/IL-Group/FL-Studio-API-Stubs) — API documentation
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server framework
- [mido](https://github.com/mido/mido) — MIDI library for Python
- [calvinw/fl-studio-mcp](https://github.com/calvinw/fl-studio-mcp) — Piano Roll integration approach
- [Image-Line](https://www.image-line.com/) — FL Studio

## Contributing

Contributions are welcome! Feel free to open an issue or a pull request.

This fork includes automated boundary tests and scoped Windows runtime evidence in
[the validation record](docs/windows-validation.md). Implemented entry points and
runtime-accepted cases are listed separately. Changes affecting FL Studio behavior
should also be tested in the application; the repository has no CI or formal review process yet.

## License

MIT
