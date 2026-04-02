#!/usr/bin/env python3
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tkinter as tk
from tkinter import ttk


ALL_DISPLAYS_LABEL = "All displays"
BACKLIGHT_SYSFS = Path("/sys/class/backlight")


def _set_status(message: str) -> None:
    if "status_var" in globals():
        status_var.set(message)


def _command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _is_wayland_session() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


def get_xrandr_displays():
    """Return (displays, primary) from xrandr.

    - displays: list of connected output names (e.g., ["DP-0", "HDMI-1"]) in xrandr order
    - primary: the primary output name or None
    """
    try:
        output = subprocess.check_output(["xrandr", "-q"], text=True)
    except Exception:
        _set_status("xrandr not available (or no X11 display)")
        return [], None

    displays: list[str] = []
    primary: str | None = None

    for line in output.splitlines():
        # Example:
        # "DP-0 connected primary 2560x1440+0+0 ..."
        # "HDMI-1 connected 1920x1080+0+0 ..."
        if " connected" not in line:
            continue
        name = line.split()[0]
        displays.append(name)
        if " primary " in f" {line} ":
            primary = name

    return displays, primary


def get_backlight_displays() -> list[str]:
    displays: list[str] = []
    if not BACKLIGHT_SYSFS.is_dir():
        return displays

    for device in sorted(BACKLIGHT_SYSFS.iterdir()):
        max_path = device / "max_brightness"
        brightness_path = device / "brightness"
        if not max_path.is_file() or not brightness_path.is_file():
            continue

        try:
            max_value = int(max_path.read_text().strip())
        except (OSError, ValueError):
            continue

        if max_value > 0:
            displays.append(device.name)

    return displays


def get_backlight_level(device_name: str) -> float:
    device_path = BACKLIGHT_SYSFS / device_name
    try:
        current = int((device_path / "brightness").read_text().strip())
        maximum = int((device_path / "max_brightness").read_text().strip())
    except (OSError, ValueError):
        return 1.0

    if maximum <= 0:
        return 1.0
    return clamp(current / maximum, 0.1, 1.0)


def parse_ddcutil_displays() -> list[dict[str, str]]:
    if not _command_exists("ddcutil"):
        return []

    proc = subprocess.run(
        ["ddcutil", "detect", "--brief"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []

    displays: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        match = re.match(r"^Display\s+(\d+)", line)
        if match:
            if current and "bus" in current:
                displays.append(current)
            current = {"label": f"Display {match.group(1)}"}
            continue

        if not current or not line:
            continue

        bus_match = re.search(r"/dev/i2c-(\d+)", line)
        if bus_match:
            current["bus"] = bus_match.group(1)
            continue

        if ":" not in line:
            continue

        key, value = [part.strip() for part in line.split(":", 1)]
        if key in {"Monitor", "Model"} and value:
            current["label"] = value
        elif key == "DRM connector" and value:
            current["connector"] = value

    if current and "bus" in current:
        displays.append(current)

    labels_seen: set[str] = set()
    for display in displays:
        label = display["label"]
        connector = display.get("connector")
        if connector and label in labels_seen:
            label = f"{label} ({connector})"
        elif label in labels_seen:
            label = f"{label} (bus {display['bus']})"
        labels_seen.add(label)
        display["label"] = label

    return displays


def detect_display_backend() -> tuple[str | None, list[str], str | None, str, dict[str, str]]:
    backlight_displays = get_backlight_displays()
    if backlight_displays:
        primary = backlight_displays[0]
        note = "Using Raspberry Pi backlight control"
        return "backlight", backlight_displays, primary, note, {}

    ddcutil_displays = parse_ddcutil_displays()
    if ddcutil_displays:
        labels = [display["label"] for display in ddcutil_displays]
        bus_map = {display["label"]: display["bus"] for display in ddcutil_displays}
        note = "Using DDC/CI monitor brightness control"
        return "ddcutil", labels, labels[0], note, bus_map

    xrandr_displays, primary = get_xrandr_displays()
    if xrandr_displays:
        note = "Using xrandr software brightness"
        if _is_wayland_session():
            note += " (may be ignored on Wayland)"
        return "xrandr", xrandr_displays, primary, note, {}

    if _is_wayland_session():
        note = "No supported brightness backend found. On Pi Wayland, use /sys/class/backlight or install ddcutil for HDMI monitors."
    else:
        note = "No connected displays found via backlight, ddcutil, or xrandr"
    return None, [], None, note, {}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def kelvin_to_rgb_gains(kelvin: float) -> tuple[float, float, float]:
    """Approximate color temperature (K) to RGB gains (0..1).

    Uses a common approximation that is smooth and fast enough for UI.
    """
    k = clamp(kelvin, 1000.0, 40000.0) / 100.0

    if k <= 66.0:
        red = 255.0
        green = 99.4708025861 * math.log(k) - 161.1195681661
        if k <= 19.0:
            blue = 0.0
        else:
            blue = 138.5177312231 * math.log(k - 10.0) - 305.0447927307
    else:
        red = 329.698727446 * ((k - 60.0) ** -0.1332047592)
        green = 288.1221695283 * ((k - 60.0) ** -0.0755148492)
        blue = 255.0

    red = clamp(red, 0.0, 255.0) / 255.0
    green = clamp(green, 0.0, 255.0) / 255.0
    blue = clamp(blue, 0.0, 255.0) / 255.0

    # Normalize so the strongest channel is 1.0
    max_c = max(red, green, blue, 1e-6)
    red, green, blue = red / max_c, green / max_c, blue / max_c

    # Keep values within xrandr-typical range
    return clamp(red, 0.1, 1.0), clamp(green, 0.1, 1.0), clamp(blue, 0.1, 1.0)


def apply_xrandr_settings(output_name: str, brightness: float, r: float, g: float, b: float) -> None:
    proc = subprocess.run(
        [
            "xrandr",
            "--output",
            output_name,
            "--brightness",
            f"{brightness:.2f}",
            "--gamma",
            f"{r:.3f}:{g:.3f}:{b:.3f}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if err:
            _set_status(f"xrandr failed: {err.splitlines()[-1]}")
        else:
            _set_status("xrandr failed (no error output)")


def apply_backlight_settings(device_name: str, brightness: float) -> None:
    device_path = BACKLIGHT_SYSFS / device_name
    try:
        maximum = int((device_path / "max_brightness").read_text().strip())
        target = max(1, int(round(clamp(brightness, 0.1, 1.0) * maximum)))
        (device_path / "brightness").write_text(f"{target}\n")
    except PermissionError:
        _set_status(f"Backlight write failed for {device_name}: permission denied")
    except (OSError, ValueError) as exc:
        _set_status(f"Backlight write failed for {device_name}: {exc}")


def apply_ddcutil_settings(display_label: str, brightness: float) -> None:
    bus = ddcutil_bus_map.get(display_label)
    if not bus:
        _set_status(f"No DDC/CI bus found for {display_label}")
        return

    value = int(round(clamp(brightness, 0.1, 1.0) * 100.0))
    proc = subprocess.run(
        ["ddcutil", "setvcp", "10", str(value), "--bus", bus],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if err:
            _set_status(f"ddcutil failed: {err.splitlines()[-1]}")
        else:
            _set_status("ddcutil failed (no error output)")


def get_selected_outputs() -> list[str]:
    selected = display_var.get() if "display_var" in globals() else ""
    if not connected_displays:
        return []
    if selected == ALL_DISPLAYS_LABEL or not selected:
        return list(connected_displays)
    if selected in connected_displays:
        return [selected]
    return list(connected_displays)


def apply_settings(_event=None) -> None:
    outputs = get_selected_outputs()
    if not outputs:
        _set_status(backend_note)
        return

    brightness = float(brightness_slider.get())

    if backend_kind == "xrandr" and night_shift_var.get():
        r, g, b = kelvin_to_rgb_gains(float(temp_slider.get()))
    else:
        r = g = b = 1.0

    for out in outputs:
        if backend_kind == "backlight":
            apply_backlight_settings(out, brightness)
        elif backend_kind == "ddcutil":
            apply_ddcutil_settings(out, brightness)
        else:
            apply_xrandr_settings(out, brightness, r, g, b)

    if "status_var" not in globals():
        return

    status = status_var.get()
    if status.startswith("xrandr failed") or status.startswith("ddcutil failed") or status.startswith("Backlight write failed"):
        return

    if backend_kind == "xrandr":
        _set_status(f"Applied xrandr settings to: {', '.join(outputs)}")
    elif backend_kind == "ddcutil":
        _set_status(f"Applied DDC/CI brightness to: {', '.join(outputs)}")
    else:
        _set_status(f"Applied backlight brightness to: {', '.join(outputs)}")


def on_night_shift_toggle() -> None:
    state = "normal" if night_shift_var.get() else "disabled"
    temp_slider.configure(state=state)
    apply_settings()


def configure_controls_for_backend() -> None:
    if backend_kind == "xrandr":
        night_shift_toggle.configure(state="normal")
        temp_slider.configure(state="normal" if night_shift_var.get() else "disabled")
        return

    night_shift_var.set(False)
    night_shift_toggle.configure(state="disabled")
    temp_slider.configure(state="disabled")

# --- GUI setup ---
root = tk.Tk()
root.title("Display Control")
root.geometry("420x280")

tk.Label(root, text="Screen Brightness", font=("Arial", 12)).pack(pady=(8, 4))

backend_kind, connected_displays, primary, backend_note, ddcutil_bus_map = detect_display_backend()

# Only show a dropdown if there is more than one connected output.
display_var = tk.StringVar()
if len(connected_displays) > 1:
    options = [ALL_DISPLAYS_LABEL] + connected_displays
    display_var.set(ALL_DISPLAYS_LABEL)

    picker_frame = tk.Frame(root)
    picker_frame.pack(pady=2)

    tk.Label(picker_frame, text="Display:").pack(side="left", padx=(0, 6))

    display_picker = ttk.Combobox(
        picker_frame,
        textvariable=display_var,
        values=options,
        state="readonly",
        width=22,
    )
    display_picker.pack(side="left")

    def on_display_change(_event=None):
        apply_settings()

    display_picker.bind("<<ComboboxSelected>>", on_display_change)
else:
    # Single (or zero) display: default to primary if present, else the first.
    if primary:
        display_var.set(primary)
    elif connected_displays:
        display_var.set(connected_displays[0])

# brightness slider (0.1 - 1.0)
brightness_slider = tk.Scale(
    root,
    from_=0.1,
    to=1.0,
    resolution=0.05,
    orient="horizontal",
    length=280,
    command=lambda _v: apply_settings(),
)
brightness_slider.set(1.0)
brightness_slider.pack(pady=(0, 6))

ttk.Separator(root, orient="horizontal").pack(fill="x", padx=10, pady=6)

night_shift_var = tk.BooleanVar(value=False)
night_shift_toggle = tk.Checkbutton(
    root,
    text="Night Shift",
    variable=night_shift_var,
    command=on_night_shift_toggle,
)
night_shift_toggle.pack(pady=(2, 4))

tk.Label(root, text="Color Temperature (K)").pack(pady=(2, 0))
temp_slider = tk.Scale(
    root,
    from_=1000,
    to=10000,
    resolution=100,
    orient="horizontal",
    length=280,
    command=lambda _v: apply_settings(),
)
temp_slider.set(4800)
temp_slider.configure(state="disabled")
temp_slider.pack(pady=(0, 8))

status_var = tk.StringVar(value="")
status_label = tk.Label(root, textvariable=status_var, fg="#555", wraplength=390, justify="left")
status_label.pack(padx=10, pady=(0, 8), anchor="w")

configure_controls_for_backend()
_set_status(backend_note)

# Match the slider to the current backlight level when that backend is active.
if backend_kind == "backlight" and connected_displays:
    brightness_slider.set(get_backlight_level(connected_displays[0]))

# Apply once on startup (safe no-op if xrandr isn't available)
root.after(100, apply_settings)

root.mainloop()
