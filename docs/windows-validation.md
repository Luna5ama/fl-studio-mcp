# Windows validation record — 2026-10-30

This records implementation and scoped acceptance through 2026-10-30. A registered
MCP tool, a passing simulation, a native input event and a completed operation are
separate evidence. The tables below identify which paths ran against real FL Studio.

Native workflows require Windows 10/11, an English FL Studio UI, Windows English
OCR, scanned plugins where applicable, and visible unobstructed controls. No Computer
Use tool was used or is required: the program may foreground FL and use local OCR,
mouse and keyboard input. Foreground-loss guards remain enabled. Only one agent or
program may own native FL input at a time.

## Installation and real controller reload

`uv run python scripts/install_controller.py` installs the main controller, all five
`mcp_*.py` helpers, and the Piano Roll script. It bakes the same relocated FL user-data
root into both embedded entry points; the tested root was
`I:\MusicProduction\FL User Data\FL Studio`.

After installation, select the loopMIDI input in **Options > MIDI Settings**. Change
**Controller type** to a generic controller, then back to **FL Studio MCP Controller**.
This actually reloads the Python entry point and helpers. **Refresh device list**
only refreshes devices and did not reload these modules on the tested host. Restart
the MCP server after source updates, then verify the versioned handshake, all five
addons, and no addon errors with `fl_check_environment`.

The complete native operation, MIDI mailbox and Piano Roll request/trigger cycle
share one process lock. Cooperative cancellation and cached active-operation reads
remain available while the native runner owns that lock.

## Automated checks

The final repository-wide run passed **512 tests** and Ruff. Offline wheel/sdist
content checks, Python 3.10 parsing and PowerShell installer parsing are also required
for the final packaged artifact; their evidence is recorded with the local manifest.

Coverage includes public tool registration, controller dispatch, native ownership,
post-drop acknowledgements, cancellation races, uncertain outcomes, reload cleanup,
MIDI request correlation, route rollback, target identity, relocated paths, full FST
records and completed WAV/FLP structures. Simulated windows/API checks establish
these boundaries; they do not establish real GUI success.

## Real host and accepted cases

Host: FL Studio Producer Edition v26.1.7 build 5654, MIDI API 45, controller protocol
2, English FL Studio UI, Chinese Windows file dialogs, and loopMIDI. The tested main
window was 2560 × 1392 pixels. All five installed helpers loaded without addon
errors after an actual controller-type reload.

### Plugins and native FST

Indices in this table are zero-based.

| Case | Observed result |
| --- | --- |
| Speldosa VST3 loading | New channel 6, explicitly routed to Mixer track 6 |
| Correlometer VST3 loading | Loaded on Mixer track 6, slot 0 |
| Effect wet | Requested 0.37; read back 0.3700000047683716 |
| Instrument enabled state | Channel 6 disabled and enabled; host mute state read back |
| Effect bypass/enable | LED state independently read; bounded observation after one click resolved the initial premature readback |
| Same-track effect move | Track 6, slot 0 → 2; all slot fingerprints matched the native adjacent swaps |
| Effect deletion | Selected track 6, slot 2; exact FL 26 confirmation; public tool returned `deleted` |
| Instrument deletion | Public VST2 Speldosa add to appended global channel 7, then public delete; original channels 0–6 identities/routes and Room 0 preserved |
| Native FST save | Speldosa fixtures saved through the native wrapper menu; 9,061-byte and 9,089-byte files |
| Public instrument FST load | Room was saved at 0, deliberately changed to 1, then restored to 0 through `fl_load_plugin_preset`; operation `555842234000000` returned `loaded` |
| Named instrument state verification | All 150 named published parameters matched; the manifest retained all 4,240 names/value records |
| Effect FST save/load | Correlometer saved to a complete 1,007-byte FST; Bypass 0 → 1 → 0 through public load, all 145 named parameters matched |
| Cross-track copy | 6:0 → 7:2, source retained, wet 0.37 and host effect-enabled False (bypassed) preserved |
| Cross-track move | 7:4 → 8:3, destination FST verified before source deletion; automation links explicitly reported as not preserved |
| Replacement | Correlometer at 8:3 replaced by Fruity Balance; independent plugin-identity change witness |
| Live cancellation before activation | Requested during Browser navigation; request returned in 47 ms, terminal `cancelled` with runner stopped, target 7:8 remained empty |
| Live cancellation after activation | After real native mouse release and controller drop acknowledgement, public request returned in 31 ms; terminal `loaded`, no error, runner stopped; completed operation rejected another cancellation; actual effect cleaned |

The FST result proves the public save/load path and named published state on this
instrument. Unnamed values and hidden/private VST state were **not independently
accepted**. An earlier strict comparison returned `unknown` when an unnamed value
differed even though Room was restored; that evidence was retained, with no arbitrary
parameter-index exclusion. The accepted contract compares the full named schema and
values and reports unnamed differences.

FST-backed FX operations initially exposed the private staging basename as the
slot display name. The final fix places uniqueness in an exclusively created private
directory and retains the normal display-name leaf. Fresh generator operation
`560606625000000` and effect operation `560801218000000` both completed with normal
Speldosa/Correlometer names, the full named-state comparison, post-release drop
acknowledgement, and staged file/directory fields cleared after cleanup.
Windows-illegal display labels are rejected before navigation or activation.

The plugin-stage checkpoint retained original channels 0–5 and the new enabled
Speldosa on channel 6, routed to Mixer 6 with Room 0. Effect tracks 6/7/8 were empty.
That stage saved no FLP; the later file stage saved the seven-channel scene. The
fixture contained an earlier private channel label and the stage restored `Speldosa`
explicitly. A later fresh save/load under the final directory scheme independently
confirmed Room 0 and `channel_name_preserved: true` with the normal Speldosa name.
The target instrument must be visible in the current Channel Rack filter: the Audio
filter hid global channel 6 and returned local form ID -1, so the guard refused it.
Switching the test fixture to All made the strict local-to-global proof valid.
No -1-to-target fallback was introduced.

### Files and independent audio evidence

| Case | Observed result |
| --- | --- |
| Sample import | Complete single-call public Audio Clip and Sampler imports passed; final Sampler E2E is global channel 7/type 0/Mixer 1; prior channels including event IDs preserved and source SHA unchanged |
| FLP Save As | Final `MCP Windows Validated 20261030.flp`: 62,509 bytes, header channel count 8, PPQ 96; FL reported `changed: 0`. Earlier export checkpoint: 61,561 bytes |
| Full-song WAV | Public `fl_export_wav`, 778,050 frames / 17.642857142857142 s |
| Selected-range WAV | Public `fl_export_wav`, 75,600 frames / 1.7142857142857142 s |
| Actual WAV format | Both outputs: stereo PCM, 44,100 Hz, 16-bit |
| Independent full-song analysis | Peak 0.061004638671875, RMS 0.005310337027197139; non-silent |
| Independent selection analysis | Peak 0.023773193359375, RMS 0.010442931111816484; non-silent |

Save acceptance covers the requested output path, completed FLP structure and clean
project flag. It does not independently prove that every plugin's private state
would survive reopening the project. WAV acceptance includes actual decoded audio
measurements; headers alone were not treated as proof of audible output. Extra
selected formats/stems remained in private staging, and mode-restoration results
were recorded. Full-song rendering clears the timeline selection and reports that
side effect; playback remains stopped.

### Pattern timing, tempo and Playlist

On this Windows API 45 host, Pattern length is returned in sixteenth-note steps.
The production conversion read 16 steps as 4 quarter-note beats; an earlier raw
4-step test pattern is 1 quarter-note beat, not 4 beats.

Public `bpm` and `fl_set_tempo` input use the value displayed in FL's toolbar.
`quarter_bpm` remains a separate API reading, with `ui_to_quarter_ratio` relating
it to the UI. Project-summary `beats_per_bar` uses displayed beats and is tagged
`beats_per_bar_unit: displayed_beats`; `quarter_beats_per_bar` retains its explicit
quarter-note unit. Real getter acceptance passed at displayed 140 / quarter 140 in 4/4,
and displayed 140 / quarter 70 / ratio 2 in 6/8. A toolbar number must be independently
identified by its native Tempo hint; an API value alone is not a UI readback.

The complete public cycle passed: 4/4 displayed 140 / quarter 140; change to 6/8
and read displayed 140 / quarter 70; set displayed 160 and independently confirm
quarter 80 plus toolbar 160; set displayed 140 and confirm quarter 70; restore
4/4 displayed 140 / quarter 140. The project summary also confirmed 6 displayed
beats versus 3 quarter-note beats per bar. The native setter consumes displayed
BPM while its getter reports quarter-note BPM, so the controller receives both
units explicitly and independently verifies the expected quarter-note result.

The initial 160 toolbar OCR produced letter mixtures instead of digits and was
rejected. Re-reading the same bounded pixels at double scale produced the exact
numeric word; the final live cycle passed with the same native control, geometric,
ratio and stable Tempo-hint guards. No letter-to-number substitution was used.

Final public Playlist cases passed:

| Case | Observed result |
| --- | --- |
| Pattern placement/read/delete | Beat at Track 7/bar 2, exact 384..768 ticks; fresh final-source single calls all returned success |
| Pattern cross-track move | Track 2/bar 1 → Track 3/bar 2; exact 384..768-tick readback, then verified deletion |
| Audio placement/read/delete | Audio channel 5 at Track 5/bar 2, exact 384..496 ticks |
| Audio cross-track move | Track 5/bar 2 → Track 6/bar 3, exact 768..880 ticks; independent readback and deletion passed |
| Source selection | Audio/Pattern Picker tabs identified by two native hints; explicit exact source-row selection, including the complete first row |

The half-second Audio fixture initially occupied about 14 pixels at 48 px/bar,
truncating its title. Verification proceeded only after preparing a readable view
near 150 px/bar. A single native input was never repeated merely because its
readback was uncertain. Native-ruler recovery scans a bounded strip only after the
first full-Playlist ruler calibration fails; the no-track clear-selection path uses
the same recovery. An OCR word mapped onto an exactly uniform RGB area is discarded
because no visible glyph exists there; real multiple-location conflicts still fail.

The row-top drag-offset fix preserved the grab offset between unequal track heights.
Reversible local selection-pixel witnesses, visible endpoint guards and three stable
absent-label/changed-pixel deletion checks remained enabled. Temporary unique track
names were restored, test occurrences were removed, source Pattern data retained its
four steps, and the final independent checkpoint was clean. Default numeric labels
can still be unreadable to OCR; use unique fully readable names. Grouping must be
disabled, both move endpoints must be visible, and hidden/stacked clips remain outside
the inventory boundary.

### Earlier retained routing/loading evidence

Earlier live MCP checks verified send 1 → 2 at 0.4; sidechain 1 → 2 at level 0 while
preserving the Master route; rejection of feedback 2 → 1; and exclusive sources
3/4 → bus 5 at 0.6. Original routes/levels for sources 1–5 were restored and verified.
Earlier Speldosa VST2 loading and Correlometer loading into multiple requested slots
remain historical loading evidence, not acceptance of the new lifecycle/FST paths.

## Follow-up completion and coverage boundaries

All three previously pending cases were completed after the user dismissed the
Windows Update notification. FL had exited; the saved seven-channel project was
reopened after its hash was checked, the missing virtual MIDI port was restored,
and communication was independently verified. Browser had a zero-width dock; its
native divider restored a visible layout before loading. No OCR matching guard
was relaxed to bypass hidden controls.

The first instrument deletion stopped at FL's channel-specific confirmation.
The fix accepts the exact channel prompt for generators and the exact plugin
prompt for effects, rejecting cross-kind dialogs. After safely resolving that
pending test object, a fresh public add/delete cycle passed.

Post-drop cancellation exposed a loaded result retaining an informational error.
The controller now reports cancellation_detail during observation, clears the
temporary inspection requirement after verified completion, and retains the
request audit. The updated controller was installed and truly reloaded before
the final live replay. An observation hook requested public cancellation only
after the real mouse release and successful acknowledgeDrop; it did not replace
native input or controller results. This establishes post-drop completion, not
every possible late-cancellation failure or timeout.

Sampler import used exactly one public call. Its uniquely named Sampler E2E
fixture is intentionally retained; existing channels were unchanged. The tool
proved the loaded source label before renaming, channel type and route, while
an independent snapshot checked prior event IDs and the unchanged source hash.
Hidden sample content/path and private plugin state remain outside independent
acceptance. The final eight-channel FLP was saved clean; earlier WAVs were
rendered from the separate seven-channel export checkpoint.

A timeout, cancelled input or foreground interruption does not undo an activation.
Retain staged artifacts for uncertain outcomes, inspect the destination, and never
automatically repeat native plugin activation. Cases interrupted before verification
are recorded as unaccepted, not passed.

## Local evidence

The following files are local validation artifacts, not repository contents:

- `I:\MusicProduction\mcp-validation\20261030\final-state.json`: final saved FLP,
  environment handshake, 8 channel names/routes, Room 0, no effect tracks and clean state.
- `I:\MusicProduction\mcp-validation\20261030\arrangement\root-public-final.json`
  and `root-pattern-read-cleanup.json`: public Audio and final Pattern paths.
- `I:\MusicProduction\mcp-validation\20261030\plugins\acceptance.md` and `acceptance.json`:
  public plugin operations, FST application witness, parameter coverage and snapshot identities.
- The same directory's `final-staging-acceptance.json`: final normal-name Gen/FX loads,
  post-release acknowledgement, named state, independent readbacks and cleanup.
- The same directory's `generator-delete-final.json` and `post-activation-cancel-final.json`:
  completed follow-up public deletion and post-drop cancellation, including cleanup.
- `I:\MusicProduction\mcp-validation\20261030\project-files\sampler-public-final.json`:
  single-call Sampler import, existing-channel identities, final save and state capture.
- The same directory's `roundtrip-readback.json`: full before/after parameter records,
  including the earlier strict unnamed-state mismatch.
- `I:\MusicProduction\mcp-validation\20261030\project-files\final-export-stage.json`:
  final file headers, channel count, timing and checkpoint state.
- The same directory's `full-song-audio-analysis.json` and `selection-audio-analysis.json`:
  independent PCM frame/peak/RMS measurements.
- `I:\MusicProduction\mcp-validation\20261030\tempo\public-cycle-final.json`:
  complete public displayed-BPM cycle and restored host timing.
- `.tmp/runtime-plugins-20261030.json`, `.tmp/runtime-files-20261030.json`,
  `.tmp/runtime-arrangement-20261030.json`, `.tmp/runtime-tempo-root-20261030.json`
  and `.tmp/tempo-native-setter-probe.json`: raw local operation results, including
  failed and restored cases. These logs are ignored by Git.
