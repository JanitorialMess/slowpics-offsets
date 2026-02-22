# SlowPics Offsets Plugin for VSPreview

Compare multiple video sources with per-frame, per-source offsets, then upload either as a new slow.pics comp or append to an existing comp.
<p align="center">
  <img width="528" height="674" alt="preview" src="https://github.com/user-attachments/assets/c4af5de9-7a6e-427c-aa31-1cee4b8867aa" style="width: 40%; height: auto;">
</p>

## Features

- **Per-frame offsets** - Set different offsets for each frame, per source
- **SlowPics Comp support** - Generate frames using SlowPics Comp settings
- **Frame management** - Add, edit, remove frames manually
- **Upload modes** - `New Comparison` and `Append to Existing`
- **Append by clone** - Loads an existing comp, clones it, and appends selected local sources
- **Manual frame mapping fallback** - Provide an explicit row-to-frame map when frame parsing fails
- **Offset-aware upload** - Upload to slow.pics with offsets applied per source
- **State persistence** - Save and load frame selections with offsets for future comps

## Installation

### Method 1: PyPI (Recommended)
1. Install via pip:
   ```bash
   pip install slowpics-offsets
   ```
2. Run the setup command to link it to VSPreview:
   ```bash
   spo-install
   ```
   *This detects your VSPreview plugins folder and installs a loader file.*

   To specify a custom path:
   ```bash
   spo-install --path /path/to/plugins
   ```

### Method 2: Local Source Install
1. Clone this repository:
   ```bash
   git clone https://github.com/JanitorialMess/slowpics-offsets.git
   cd slowpics-offsets
   ```
2. Install in editable mode:
   ```bash
   pip install -e .
   ```
3. Link it to VSPreview:
   ```bash
   spo-install
   ```

## Requirements

- VSPreview with the built-in `SlowPics Comp` plugin available

## Usage

### 1. Select Frames

Use **Generate Frames (using SlowPics settings)** to import frame selection from the built-in SlowPics Comp settings.

Or manage frames manually with:
- **Add Frame**
- **Edit Frame**
- **Remove Frame**
- **Prev / Next** navigation

### 2. Set Offsets

For each frame in your selection:
1. Select the frame from the list
2. Adjust the offset spinboxes for each source
3. The preview updates automatically to show the offset frame
4. Navigate between frames to verify alignment

Each frame can have different offsets per source. For example, if a transition appears on frame 5000 in one source, you can set a +10 offset just for that frame.

### 3. Choose Upload Mode

- **New Comparison**:
  - Uploads selected local sources as a new comp
  - Cookies are optional (anonymous upload can work, same as built-in behavior)
- **Append to Existing**:
  1. Paste a target key/URL (for example `https://slow.pics/c/abcd1234`)
  2. Click **Load**
  3. If frame numbers were not parsed from target rows, enter a manual frame map and click **Apply**
  4. Select local sources to append
  5. Click **Upload**
  - This mode may require cookies (clone permission can fail with 401/403)

#### Manual Frame Mapping (Append Mode)

Append mode must map target rows to local frame numbers 1:1 and in order.

- The plugin first tries to parse frame indices from target row names.
- If parsing fails, or your local frame list changes after loading target, you must provide the map manually.
- Format: comma-separated frame numbers (for example `100, 250, 500`).
- The number of entries must match target row count exactly.

Append mode uses the original slow.pics clone flow.

### 4. Save/Load and Interop

- **Save Offsets**: save selected frames + offsets to JSON
- **Load Offsets**: restore selected frames + offsets from JSON
- **Send Frame List to SlowPics Comps Tab**: send frame numbers to built-in SlowPics Comp without offsets

## Keyboard Shortcuts

- **Ctrl+[** - Previous generated frame
- **Ctrl+]** - Next generated frame

## JSON Format

State files use this format:

```json
{
  "version": 1,
  "selected_frames": [100, 500, 1000],
  "offsets": {
    "500": {
      "Source1": 10,
      "Source2": -5
    }
  }
}
```

Only frames with non-zero offsets are stored. Offsets are keyed by source name. If source names change between sessions, remapping is required.

## Warning
This plugin intentionally relies on internals from VSPreview's built-in SlowPics Comp plugin. Updates in VSPreview may change those internals and break behavior.
