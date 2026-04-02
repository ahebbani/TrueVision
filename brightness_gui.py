#!/usr/bin/env python3
import math
import subprocess
import tkinter as tk
from tkinter import ttk

def get_connected_displays():
    """Return (displays, primary) from xrandr.

    - displays: list of connected output names (e.g., ["DP-0", "HDMI-1"]) in xrandr order
    - primary: the primary output name or None
    """
    try:
        output = subprocess.check_output(["xrandr", "-q"], text=True)
    except Exception:
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
    subprocess.run(
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
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


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
        return

    brightness = float(brightness_slider.get())

    if night_shift_var.get():
        r, g, b = kelvin_to_rgb_gains(float(temp_slider.get()))
    else:
        r = g = b = 1.0

    for out in outputs:
        apply_xrandr_settings(out, brightness, r, g, b)


def on_night_shift_toggle() -> None:
    state = "normal" if night_shift_var.get() else "disabled"
    temp_slider.configure(state=state)
    apply_settings()

# --- GUI setup ---
root = tk.Tk()
root.title("Display Control")
root.geometry("380x240")

tk.Label(root, text="Screen Brightness", font=("Arial", 12)).pack(pady=(8, 4))

ALL_DISPLAYS_LABEL = "All displays"

connected_displays, primary = get_connected_displays()

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

# Apply once on startup (safe no-op if xrandr isn't available)
root.after(100, apply_settings)

root.mainloop()
