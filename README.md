# SlowPics Offsets Plugin for VSPreview

Compare multiple video sources with per-frame, per-source offsets.
<p align="center">
  <img width="528" height="674" alt="preview" src="https://github.com/user-attachments/assets/c4af5de9-7a6e-427c-aa31-1cee4b8867aa" style="width: 40%; height: auto;">
</p>

## Features

- **Per-frame offsets** - Set different offsets for each frame, per source
- **SlowPics Comp support** - Generate frames using SlowPics Comp settings
- **Frame management** - Add, edit, remove frames manually
- **Offset-aware upload** - Upload to slow.pics with offsets applied per source
- **State persistence** - Save and load frame selections with offsets for future comps

## Installation

Copy the `slowpics-offsets` folder to your VSPreview plugins directory:
- Windows: `%APPDATA%\vspreview\plugins\`
- Linux/Mac: `~/.config/vspreview/plugins/`

## Usage

### 1. Generate Frames

First go the the "SlowPics Offsets" tab in VSPreview and fill out the frame generation settings.    
Click **Generate Frames (using SlowPics settings)** to use the SlowPics plugin's configuration (random frames, picture types, etc.). This creates your initial frame selection.

Alternatively, manually add frames using the **Add Frame** button.

### 2. Set Offsets

For each frame in your selection:
1. Select the frame from the list
2. Adjust the offset spinboxes for each source
3. The preview updates automatically to show the offset frame
4. Navigate between frames to verify alignment

Each frame can have different offsets per source. For example, if a transition appears on frame 5000 in one source, you can set a +10 offset just for that frame.

### 3. Upload or Save

- **Upload** - Uploads to slow.pics with offset-adjusted frames per source
- **Save Offsets** - Saves frame selection and offsets to JSON for later use
- **Load Offsets** - Restores a previously saved configuration
- **Send to SlowPics Comps Tab** - Sends frame numbers to SlowPics Comps without offsets

## Keyboard Shortcuts

- **Ctrl+[** - Previous generated frame
- **Ctrl+]** - Next generated frame

## JSON Format

State files this format:

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

Only frames with non-zero offsets are stored. Offsets are keyed by source name. Changes to source names between sessions requires manual remapping.

## Warning
This project heavily depends on SlowPics Comp plugin internals. Updates to that plugin may break functionality. I did not want to duplicate all the frame generation logic, so this plugin hooks into SlowPics Comp directly. Use at your own risk.
