"""Render a recorded Telecharge run at 1x: browser on the left, Jev's live decisions on the right.

uv run python scripts/render_telecharge.py artifacts/telecharge/<recorded-run>

The run uses a tall viewport, so each frame holds the whole page. The browser panel is a camera over
that frame: it pans to each element Jev chooses when the choice is made, and outlines it. Timing is
original; human-approval pauses are cut. Writes demo.mp4, demo.gif and poster.png into the
recording folder, then deletes the raw frames.
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
parser.add_argument("source", type=Path, help="Folder written by examples/telecharge.py --record")
parser.add_argument("--hold", type=int, default=2500, help="Milliseconds to hold the final frame")
parser.add_argument(
    "--keep-frames", action="store_true", help="Keep raw screencast frames (only listed fields are blurred)"
)
args = parser.parse_args()
source = args.source.resolve()
state = json.loads((source / "state.json").read_text())
summary = state["summary"]
rec = state["recording"]
assert not rec["errors"], rec["errors"]

FPS = 30
W, H = 1536, 1000
PANEL_W, PANEL_H = 1120, 732
PAN_MS = 350
ink, muted, green, amber, red = "#172a20", "#6a766c", "#2a743f", "#9a6a12", "#a2402f"
events = rec["events"]
steps = [e for e in events if e["kind"] == "step"]
targets = [e for e in events if e["kind"] == "target"]
stages = {e["index"]: e for e in events if e["kind"] == "stage"}
entries = [e for e in events if e["kind"] == "entry"]
pauses = sorted(rec["pauses"])
gated = rec["open_pause"] is not None
# The moment each browser input ran. Recordings without "acted" marks take it from the trace:
# history times count from the agent's clock, which the first step mark ties to the recording's.
acted = [e["t"] for e in events if e["kind"] == "acted"]
if not acted and steps and state["history"]:
    offset = steps[0]["t"] - state["history"][0]["elapsed_ms"]
    acted = [h["executed_ms"] + offset for h in state["history"]]
# The video ends when the entry is confirmed, so its length matches the run's own timing.
end_wall = (
    rec["open_pause"] if gated else entries[-1]["t"] if entries else max([e["t"] for e in events] + [0]) + 400
)
cut = sum(b - a for a, b in pauses if b <= end_wall)
video_end = end_wall - cut
frame_files = sorted((int(p.stem), p) for p in (source / "screencast").glob("*.jpg"))
viewport_w, viewport_h = summary["viewport"]
# Recordings made before several shows were allowed saved a single "show".
shows, when = summary.get("shows") or [summary["show"]], summary.get("when", "")
verified = summary.get("verified_after_reload") or []
confirmed = bool(verified) and all(p["entered"] for p in verified)
popup = [e for e in events if e["kind"] == "popup"]
signed_in_here = bool(popup)
stage_names = [
    "Sign in with LinkedIn" if signed_in_here else "Sign in (saved session)",
    "Open Lottery",
    f"{summary['tickets']} tickets",
    "Enter" if len(shows) == 1 else f"Enter {len(shows)} shows",
]


def popup_open(t):
    """True while the LinkedIn window is open (it is a separate window, so not in the screencast)."""
    opened = [e["t"] for e in popup if e["phase"] == "open" and e["t"] <= t]
    closed = [e["t"] for e in popup if e["phase"] == "closed" and e["t"] <= t]
    return bool(opened) and (not closed or closed[-1] < opened[-1])


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
    """The frame at wall time t, and whether it holds the whole tall view."""
    path = next((p for ms, p in reversed(frame_files) if ms <= t), frame_files[0][1])
    if path not in _cache:
        _cache.clear()
        shot = Image.open(path).convert("RGB")
        full = abs(shot.width / shot.height - viewport_w / viewport_h) < 0.01
        # Screencast frames can be downscaled; bring them back to viewport pixels. Chrome has also sent
        # short frames of the page top near the end of a run; those are shown at panel width.
        size = (viewport_w, viewport_h) if full else (PANEL_W, round(shot.height * PANEL_W / shot.width))
        _cache[path] = (shot.resize(size, Image.LANCZOS) if shot.size != size else shot, full)
    return _cache[path]


def top_for(target):
    """Camera top that centres a target's rect, clamped to the page."""
    y = target["rect"]["y"] + target["rect"]["h"] / 2 - PANEL_H / 2
    return max(0, min(viewport_h - PANEL_H, y))


def camera(t):
    """Camera top at wall time t: eases from the previous target to the latest one."""
    top = 0.0
    for target in targets:
        if target["t"] > t:
            break
        progress = min(1, (t - target["t"]) / PAN_MS)
        top += (top_for(target) - top) * (1 - (1 - progress) ** 3)
    return round(top)


def draw_frame(vt):
    t = wall(min(vt, video_end))
    final = vt >= video_end
    canvas = Image.new("RGB", (W, H), "#f3f4ec")
    d = ImageDraw.Draw(canvas)
    d.text((36, 26), "browser use", font=font(23, True), fill=ink)
    d.text((186, 27), "×  TypeSafe Jev", font=font(22), fill=muted)
    d.rounded_rectangle((1287, 24, 1499, 59), radius=17, fill="#dfebd9")
    d.text((1310, 32), "REAL WEB  ·  1× SPEED", font=font(14, True), fill=green)
    count = "1 lottery" if len(shows) == 1 else f"{len(shows)} lotteries"
    title = f"Signed in and entered {count} in" if signed_in_here else f"Telecharge: {count} entered in"
    d.text((36, 80), f"{title} {video_end / 1000:.1f} s", font=font(40, True), fill=ink)
    start = "Sign In → LinkedIn" if signed_in_here else "Signed in"
    d.text(
        (38, 136),
        f"{start} → Lottery → {', '.join(shows)}{' ' + when if when else ''} → Enter. One {viewport_h} px view.",
        font=font(19),
        fill=muted,
    )

    # Browser window: a camera over the full-height frame.
    d.rounded_rectangle((35, 181, 1157, 953), radius=14, fill="#202124")
    for j, c in enumerate(["#de8278", "#d6bd6e", "#8dbd8a"]):
        d.ellipse((54 + j * 19, 195, 63 + j * 19, 204), fill=c)
    d.text((145, 191), "rush.telecharge.com", font=mono(13), fill="#d4d6d5")
    shot, full = screenshot_at(t)
    top = camera(t) if full else 0
    view = Image.new("RGB", (PANEL_W, PANEL_H), "white")
    view.paste(shot.crop((0, top, PANEL_W, min(shot.height, top + PANEL_H))), (0, 0))
    canvas.paste(view, (36, 216))
    # Outline the chosen element until its input runs (or code refuses it); the page may reflow after.
    ends = sorted(acted + [e["t"] for e in events if e["kind"] == "refused"])
    recent = [e for e in targets if e["t"] <= t < next((a for a in ends if a >= e["t"]), e["t"] + 1200)]
    if recent and full:
        r = recent[-1]["rect"]
        box = (36 + r["x"] - 4, 216 + r["y"] - top - 4, 36 + r["x"] + r["w"] + 4, 216 + r["y"] - top + r["h"] + 4)
        if box[1] >= 216 and box[3] <= 216 + PANEL_H:
            d.rounded_rectangle(box, radius=8, outline=green, width=4)
    if popup_open(t):
        d.rounded_rectangle((316, 820, 876, 900), radius=14, fill="#202124")
        d.text((340, 834), "LinkedIn window signing in", font=font(21, True), fill="white")
        d.text((340, 866), "Saved session in the automation profile; nothing typed", font=font(15), fill="#d4d6d5")

    # Timer.
    x = 1189
    d.text((x + 3, 186), "JEV ULTRAFAST", font=font(16, True), fill=green)
    d.text((x, 214), f"{min(vt, video_end) / 1000:05.2f}", font=mono(52), fill=ink)
    d.text((x + 4, 278), "SECONDS ELAPSED", font=font(13, True), fill=muted)

    # Stage checklist.
    for j, name in enumerate(stage_names):
        # The last stage mark lands just after the confirmed entry that ends the video.
        done = j in stages and (stages[j]["t"] <= t or final)
        waiting = j == 3 and gated and final
        y = 326 + j * 44
        fill = green if done else amber if waiting else "#e0e4d9"
        d.ellipse((x + 4, y, x + 26, y + 22), fill=fill)
        if done:
            d.line([(x + 10, y + 11), (x + 14, y + 15), (x + 21, y + 7)], fill="white", width=2)
        d.text((x + 40, y), name, font=font(19, done), fill=ink if done else muted)

    # Current decision.
    seen = steps if final else [e for e in steps if e["t"] <= t]
    chosen = [e for e in targets if e["t"] <= t and "operation" in e]
    refused = [e for e in events if e["kind"] == "refused" and 0 <= t - e["t"] < 1200]
    entered = [e for e in entries if e["t"] <= t]
    y0 = 520
    d.rounded_rectangle((x, y0, 1499, y0 + 220), radius=14, fill="#e7e9df")
    if final and gated:
        d.rounded_rectangle((x, y0, 1499, y0 + 220), radius=14, fill="#f4ead3")
        d.text((x + 20, y0 + 20), "Paused before Enter", font=font(21, True), fill=amber)
        d.text((x + 20, y0 + 56), "A human decides whether to enter.", font=font(16), fill=muted)
    elif final and entered:
        d.rounded_rectangle((x, y0, 1499, y0 + 220), radius=14, fill="#dfebd9" if confirmed else "#f4ead3")
        d.text((x + 20, y0 + 20), "Lottery Entered!", font=font(24, True), fill=green if confirmed else amber)
        cards = [e["card"] or {} for e in entered][-4:]
        for k, card in enumerate(cards):
            y = y0 + 60 + k * 28
            d.line([(x + 22, y + 9), (x + 26, y + 13), (x + 33, y + 4)], fill=green, width=2)
            name = card.get("show") or ""
            d.text((x + 42, y), name if len(name) <= 30 else name[:29] + "…", font=font(16), fill=ink)
        verdict = "Verified on a fresh page load" if confirmed else "Not confirmed on a fresh page load"
        d.text((x + 20, y0 + 180), verdict, font=font(15, True), fill=green if confirmed else red)
    elif refused:
        d.text((x + 20, y0 + 20), "Choice refused by code", font=font(21, True), fill=red)
        for k, line in enumerate(textwrap.wrap(refused[-1]["reason"], 30)[:4]):
            d.text((x + 20, y0 + 58 + k * 24), line, font=font(16), fill=ink)
    elif seen or chosen:
        # A choice shows as soon as it is made; the step mark only lands after its result is observed.
        e = max(seen[-1:] + chosen[-1:], key=lambda e: e["t"])
        d.text((x + 20, y0 + 18), "JEV CHOSE", font=font(13, True), fill=muted)
        d.text((x + 20, y0 + 38), e["operation"], font=mono(24), fill=ink)
        label = "page" if e["operation"].startswith("SCROLL") else e["label"]
        for k, line in enumerate(textwrap.wrap(label, 28)[:3]):
            d.text((x + 20, y0 + 74 + k * 22), line, font=font(16), fill=ink)
        conf = e["confidence"]
        d.text((x + 20, y0 + 150), f"confidence {conf:.2f}", font=font(14), fill=muted)
        d.rounded_rectangle((x + 20, y0 + 174, x + 290, y0 + 186), radius=6, fill="#d3d9cc")
        d.rounded_rectangle((x + 20, y0 + 174, x + 20 + round(270 * conf), y0 + 186), radius=6, fill=green)
    else:
        d.text((x + 20, y0 + 20), "Choose. Act. Repeat.", font=font(21, True), fill=ink)

    latencies = [e["jev_ms"] for e in seen]
    d.text((x + 4, 790), f"{statistics.median(latencies):.0f} ms" if latencies else "—", font=mono(30), fill=ink)
    d.text((x + 4, 832), "median Jev decision", font=font(16), fill=muted)
    d.text((x + 4, 868), f"{sum(a <= t for a in acted)} actions executed", font=font(16), fill=muted)

    d.line((37, 966, 1498, 966), fill="#d3d9cc", width=2)
    d.line((37, 966, 37 + (1498 - 37) * min(vt, video_end) / max(video_end, 1), 966), fill=green, width=3)
    d.text(
        (37, 976),
        "Operation + element by Jev. Nothing typed on the site; name, e-mail and phone blurred. "
        "Camera pans over one full-height view. Original timing.",
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
if not args.keep_frames:
    # Raw frames show the whole page; only the rendered video is needed afterwards.
    shutil.rmtree(source / "screencast")
print(f"Rendered {len(frame_files)} screencast frames: {video_end} ms of run (cut {cut} ms of approval pauses)")
print("Wrote", source / "demo.mp4", source / "demo.gif", source / "poster.png")
