"""Render a recorded Lucky Seat run at 1x: browser on the left, Jev's live decisions on the right.

uv run python scripts/render_luckyseat.py artifacts/luckyseat/<recorded-run>

Human-approval pauses are cut; everything else, including loading waits, keeps its original timing.
Writes demo.mp4, demo.gif and poster.png into the recording folder.
"""

import argparse
import json
import shutil
import statistics
import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", type=Path, help="Folder written by examples/luckyseat.py --record")
parser.add_argument("--hold", type=int, default=2500, help="Milliseconds to hold the final frame")
args = parser.parse_args()
source = args.source.resolve()
state = json.loads((source / "state.json").read_text())
rec = state["recording"]
assert not rec["errors"], rec["errors"]

FPS = 30
W, H = 1536, 1000
ink, muted, green, amber, red = "#172a20", "#6a766c", "#2a743f", "#9a6a12", "#a2402f"
events = rec["events"]
steps = [e for e in events if e["kind"] == "step"]
stages = {e["index"]: e for e in events if e["kind"] == "stage"}
pauses = sorted(rec["pauses"])
gated = rec["open_pause"] is not None
end_wall = rec["open_pause"] if gated else max([e["t"] for e in events] + [0]) + 400
cut = sum(b - a for a, b in pauses if b <= end_wall)
video_end = end_wall - cut
frame_files = sorted((int(p.stem), p) for p in (source / "screencast").glob("*.jpg"))
pending = source / "pending.json"
form = json.loads(pending.read_text())["form"] if pending.exists() else None
form_line = (
    f"{form['checked']}/{form['total']} performances · {', '.join(form['numbers'])} ticket(s)" if form else ""
)

# Recordings made before the show was saved in the summary were all Hadestown.
show = state["summary"].get("show", "Hadestown")
stage_names = ["Log in", f"New York → {show}", "Select all performances", "1 ticket", "Submit entry"]


def wall(vt):
    """Video time to wall time, skipping approval pauses."""
    for a, b in pauses:
        if vt >= a:
            vt += b - a
    return vt


def font(n, bold=False):
    return ImageFont.truetype(f"/System/Library/Fonts/Supplemental/Arial{' Bold' if bold else ''}.ttf", n)


def mono(n):
    return ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", n)


_cache = {}


def screenshot_at(t):
    path = next((p for ms, p in reversed(frame_files) if ms <= t), frame_files[0][1])
    if path not in _cache:
        _cache.clear()
        _cache[path] = Image.open(path).convert("RGB")
    return _cache[path]


def draw_frame(vt):
    t = wall(min(vt, video_end))
    final = vt >= video_end
    canvas = Image.new("RGB", (W, H), "#f3f4ec")
    d = ImageDraw.Draw(canvas)
    d.text((36, 26), "browser use", font=font(23, True), fill=ink)
    d.text((186, 27), "×  TypeSafe Jev", font=font(22), fill=muted)
    d.rounded_rectangle((1287, 24, 1499, 59), radius=17, fill="#dfebd9")
    d.text((1310, 32), "REAL WEB  ·  1× SPEED", font=font(14, True), fill=green)
    d.text((36, 80), f"Lucky Seat lottery form, filled in {video_end / 1000:.1f} s", font=font(40, True), fill=ink)
    d.text(
        (38, 136),
        f"Log in → New York → {show} → all performances → 1 ticket. Stops for human approval.",
        font=font(19),
        fill=muted,
    )

    # Browser window.
    d.rounded_rectangle((35, 181, 1157, 953), radius=14, fill="#202124")
    for j, c in enumerate(["#de8278", "#d6bd6e", "#8dbd8a"]):
        d.ellipse((54 + j * 19, 195, 63 + j * 19, 204), fill=c)
    d.text((145, 191), "luckyseat.com", font=mono(13), fill="#d4d6d5")
    shot = screenshot_at(t)
    scale = 732 / shot.height
    shot = shot.resize((round(shot.width * scale), 732), Image.LANCZOS)
    canvas.paste(shot, (36 + (1121 - shot.width) // 2, 216))

    # Timer.
    x = 1189
    d.text((x + 3, 186), "JEV ULTRAFAST", font=font(16, True), fill=green)
    d.text((x, 214), f"{min(vt, video_end) / 1000:05.2f}", font=mono(52), fill=ink)
    d.text((x + 4, 278), "SECONDS ELAPSED", font=font(13, True), fill=muted)

    # Stage checklist.
    for j, name in enumerate(stage_names):
        done = j in stages and stages[j]["t"] <= t
        waiting = j == 4 and gated and final
        y = 326 + j * 44
        fill = green if done else amber if waiting else "#e0e4d9"
        d.ellipse((x + 4, y, x + 26, y + 22), fill=fill)
        if done:
            d.line([(x + 10, y + 11), (x + 14, y + 15), (x + 21, y + 7)], fill="white", width=2)
        d.text((x + 40, y), name, font=font(19, done), fill=ink if done else muted)

    # Current decision.
    seen = [e for e in steps if e["t"] <= t]
    refused = [e for e in events if e["kind"] == "refused" and 0 <= t - e["t"] < 1200]
    d.rounded_rectangle((x, 560, 1499, 760), radius=14, fill="#e7e9df")
    if final and gated:
        d.rounded_rectangle((x, 560, 1499, 760), radius=14, fill="#f4ead3")
        d.text((x + 20, 580), "Paused before Submit Entry", font=font(21, True), fill=amber)
        d.text((x + 20, 616), form_line, font=font(16), fill=ink)
        d.text((x + 20, 644), "A human checks the CAPTCHA", font=font(16), fill=muted)
        d.text((x + 20, 668), "and decides whether to submit.", font=font(16), fill=muted)
    elif refused:
        d.text((x + 20, 580), "Choice refused by code", font=font(21, True), fill=red)
        for k, line in enumerate(textwrap.wrap(refused[-1]["reason"], 30)[:3]):
            d.text((x + 20, 618 + k * 24), line, font=font(16), fill=ink)
    elif seen:
        e = seen[-1]
        d.text((x + 20, 578), "JEV CHOSE", font=font(13, True), fill=muted)
        d.text((x + 20, 598), e["operation"], font=mono(24), fill=ink)
        label = e["label"]
        if e["operation"].startswith("SCROLL"):
            label = "page"
        elif " → " in label:  # Dropdowns: show the chosen option, not every option's text.
            label = "→ " + label.split(" → ")[-1]
        for k, line in enumerate(textwrap.wrap(label, 28)[:2]):
            d.text((x + 20, 634 + k * 22), line, font=font(16), fill=ink)
        conf = e["confidence"]
        d.text((x + 20, 690), f"confidence {conf:.2f}", font=font(14), fill=muted)
        d.rounded_rectangle((x + 20, 714, x + 290, 726), radius=6, fill="#d3d9cc")
        d.rounded_rectangle((x + 20, 714, x + 20 + round(270 * conf), 726), radius=6, fill=green)
    else:
        d.text((x + 20, 580), "Choose. Act. Repeat.", font=font(21, True), fill=ink)

    latencies = [e["jev_ms"] for e in seen]
    d.text((x + 4, 790), f"{statistics.median(latencies):.0f} ms" if latencies else "—", font=mono(30), fill=ink)
    d.text((x + 4, 832), "median Jev decision", font=font(16), fill=muted)
    d.text((x + 4, 868), f"{len(seen)} decisions executed", font=font(16), fill=muted)

    d.line((37, 966, 1498, 966), fill="#d3d9cc", width=2)
    d.line((37, 966, 37 + (1498 - 37) * min(vt, video_end) / max(video_end, 1), 966), fill=green, width=3)
    d.text(
        (37, 976),
        "Operation + element by Jev via OpenRouter. Credentials typed by code; email and name blurred. "
        "Original timing; approval pause cut.",
        font=font(13),
        fill=muted,
    )
    return canvas


out = source / "video-frames"
shutil.rmtree(out, ignore_errors=True)
out.mkdir()
total = round((video_end + args.hold) * FPS / 1000)
for i in range(total):
    frame = draw_frame(round(i * 1000 / FPS))
    frame.save(out / f"{i:05d}.png")
frame.save(source / "poster.png")
subprocess.run(
    ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(out / "%05d.png"),
     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-movflags", "+faststart", str(source / "demo.mp4")],
    check=True,
)
subprocess.run(
    ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source / "demo.mp4"), "-vf",
     "fps=12,scale=1152:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse", "-loop", "0",
     str(source / "demo.gif")],
    check=True,
)
shutil.rmtree(out)
print(f"Rendered {len(frame_files)} screencast frames: {video_end} ms of run (cut {cut} ms of approval pauses)")
print("Wrote", source / "demo.mp4", source / "demo.gif", source / "poster.png")
