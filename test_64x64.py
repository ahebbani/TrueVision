from PIL import Image, ImageDraw, ImageFont
import textwrap

# --- Config ---
img_size = (64, 64)
bg_color = "white"
text_color = "black"
# Try longer text to test density
text = "Hello! Zobabe is a fat monkey and shawshank redemption gets no bitches!!! Heb gets too many bitches. Wow!!!"
# Optional: set to a .ttf for smoother scaling (e.g., "/System/Library/Fonts/SFNS.ttf")
font_path = None
min_font_size = 6
max_font_size = 32
line_spacing = 1  # additional pixels between lines


def load_font(size):
    if font_path:
        return ImageFont.truetype(font_path, size)
    # Fallback bitmap font (fixed size). If default font can't scale, try truetype.
    try:
        return ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", size)
    except Exception:
        return ImageFont.load_default()


def wrap_text_to_width(draw, font, text, max_width):
    # Estimate max characters per line using font.getlength if available
    sample_char = "A"
    try:
        avg_char_px = font.getlength(sample_char)
    except Exception:
        # Approximate using textbbox on a couple of characters
        bbox = draw.textbbox((0, 0), sample_char * 2, font=font)
        avg_char_px = (bbox[2] - bbox[0]) / 2 if bbox else 6
    max_chars = max(1, int(max_width / max(avg_char_px, 1)))
    lines = []
    for paragraph in text.split("\n"):
        # First wrap by character count
        rough_lines = textwrap.wrap(paragraph, width=max_chars) if paragraph else [""]
        # Refine: ensure each line fits by shrinking if needed
        for line in rough_lines:
            if not line:
                lines.append("")
                continue
            # If the line is still too wide, reduce by words until it fits
            words = line.split()
            cur = []
            for w in words:
                trial = (" ".join(cur + [w])).strip()
                bbox = draw.textbbox((0, 0), trial, font=font)
                if bbox and (bbox[2] - bbox[0]) <= max_width:
                    cur.append(w)
                else:
                    if cur:
                        lines.append(" ".join(cur))
                    cur = [w]
            if cur:
                lines.append(" ".join(cur))
    return lines


def text_bbox_multiline(draw, font, lines):
    # Measure each line, accumulate height. Use the max width across lines.
    max_w = 0
    total_h = 0
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        w = (bbox[2] - bbox[0]) if bbox else 0
        h = (bbox[3] - bbox[1]) if bbox else 0
        max_w = max(max_w, w)
        total_h += h
        if i < len(lines) - 1:
            total_h += line_spacing
    return max_w, total_h


def find_best_fit(draw, text, box_w, box_h):
    # Try sizes from max down to min; first that fits is selected
    for size in range(max_font_size, min_font_size - 1, -1):
        font = load_font(size)
        lines = wrap_text_to_width(draw, font, text, box_w)
        w, h = text_bbox_multiline(draw, font, lines)
        if w <= box_w and h <= box_h:
            return font, lines
    # Fallback to min size
    font = load_font(min_font_size)
    lines = wrap_text_to_width(draw, font, text, box_w)
    return font, lines


# --- Create image and draw ---
img = Image.new("RGB", img_size, bg_color)
draw = ImageDraw.Draw(img)

font, lines = find_best_fit(draw, text, img_size[0], img_size[1])
w, h = text_bbox_multiline(draw, font, lines)

# Center block of text
start_x = (img_size[0] - w) // 2
start_y = (img_size[1] - h) // 2

# Draw each line
cur_y = start_y
for i, line in enumerate(lines):
    bbox = draw.textbbox((0, 0), line, font=font)
    line_w = (bbox[2] - bbox[0]) if bbox else 0
    x = start_x + (w - line_w) // 2  # center each line
    draw.text((x, cur_y), line, fill=text_color, font=font)
    if bbox:
        cur_y += (bbox[3] - bbox[1])
    if i < len(lines) - 1:
        cur_y += line_spacing

# Save
img.save("text_64x64.png")

print("Saved text_64x64.png")
