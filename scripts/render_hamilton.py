"""Render a recorded Hamilton run: the device screen, the entrant's name blurred, and every tap shown.

uv run python scripts/render_hamilton.py artifacts/hamilton/<run> [--speed 1.5] [--gif]

examples/hamilton.py --record saves the device's own screen (screenrecord segments), the host time of
every accessibility read with where the hidden name node was, and every tap and drag. The tree can lag
the screen while it moves, so each blur covers the band between two consecutive reads, padded in space
and time. A caption bar under the screen names each step Jev executed and prints the playback speed
next to the real time since the first entry. --skip-middle keeps only the first and the last
performance and shows a card, with the real time left out, where the others are cut.
"""

import argparse
import json
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

PAD_SECONDS = 0.4
PAD_PIXELS = 60
RIPPLE_SECONDS = 0.6
CAPTION_LEAD = 0.3  # a step's caption shows this long before its input, while Jev decides
FOOTER = 92  # caption bar under the screen, in pixels at 540 wide
GOLD, INK, WHITE = (214, 170, 52), (23, 42, 32), (255, 255, 255)
PERFORMANCE = re.compile(r"PERFORMANCE TIME ([A-Z][a-z]+ \d{1,2}, \d{4} \d{1,2}:\d{2}[ap]m)")


def blur_windows(hidden, started, height):
    """[(start, end, top, bottom)] in seconds from `started`, covering the name between every two reads."""
    windows = []
    for (t0, rects0), (t1, rects1) in zip(hidden, hidden[1:]):
        rects = rects0 + rects1
        if not rects:
            continue
        top = max(0, min(r[1] for r in rects) - PAD_PIXELS)
        bottom = min(height, max(r[3] for r in rects) + PAD_PIXELS)
        start, end = t0 - started - PAD_SECONDS, t1 - started + PAD_SECONDS
        if windows and windows[-1][2:] == (top, bottom) and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], end, top, bottom)
        else:
            windows.append((start, end, top, bottom))
    return [(max(0.0, s), e, top, bottom) for s, e, top, bottom in windows if e > 0]


def probe(path):
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    stream = json.loads(output)["streams"][0]
    return stream["width"], stream["height"]


def start_delay(video, taps, started, latency=0.05):
    """How long after `started` the video's first frame was taken, measured from the video itself.

    screenrecord numbers frames from its first one, which comes a moment after the process starts. For
    each tap, find when the pixels around the tap point first change; that is the tap plus the app's
    reaction (`latency`). The median over the taps is the delay; the spread says how much to trust it.
    """
    side, fps, estimates = 60, 100, []
    for tap in taps[:16]:
        x, y = tap["points"][0]
        t = tap["started"] - started
        start = max(0.0, t - 1.0)
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", "2", "-i", str(video), "-vf",
             f"crop={side}:{side}:{max(0, x - side // 2)}:{max(0, y - side // 2)},fps={fps},format=gray",
             "-f", "rawvideo", "-"],
            capture_output=True, check=True,
        ).stdout
        size = side * side
        shots = [raw[i : i + size] for i in range(0, len(raw) - size + 1, size)]
        if not shots:
            continue
        differs = [sum(abs(a - b) for a, b in zip(shot, shots[0])) / size > 12 for shot in shots]
        changed = next((i for i, d in enumerate(differs) if d), None)
        if changed is not None:
            estimates.append(t + latency - (start + changed / fps))
    if not estimates:
        return 0.0, None
    estimates.sort()
    return estimates[len(estimates) // 2], estimates[-1] - estimates[0]


def frames(path, size, fps):
    """Decode a segment at a fixed frame rate, scaled to `size`, as RGB images."""
    width, height = size
    process = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"fps={fps},scale={width}:{height}:flags=lanczos",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE,
    )
    frame = width * height * 3
    while chunk := process.stdout.read(frame):
        if len(chunk) < frame:
            break
        yield Image.frombytes("RGB", size, chunk)
    process.wait()


def font(size, bold=True):
    return ImageFont.truetype(f"/System/Library/Fonts/Supplemental/Arial{' Bold' if bold else ''}.ttf", size)


def results(summary):
    """The end card's headline and rows, taken from the run's own summary."""
    targets = summary["targets"]
    # A later --verify-only read, when there is one, reads entered cards the run's own check missed.
    verified = summary.get("verified_after_restart_rescan") or summary.get("verified_after_restart") or {}
    if summary["dry_run"]:
        done = len(summary["dry_run_stops"])
        still_open = sum(1 for card in verified.values() if card and card["enter_now"])
        headline, detail = f"{done} / {len(targets)}", "reached Submit and stopped · dry run"
        check = ("Restarted app, still open", f"{still_open} / {len(targets)}")
    else:
        done = len(summary["submitted"])
        entered = sum(1 for p in summary["submitted"] if (verified.get(p) or {}).get("entered"))
        headline, detail = f"{done} / {len(targets)}", "entries submitted"
        check = ("Restarted app, marked entered", f"{entered} / {len(targets)}")
    rows = [
        ("Real time", f"{summary['entries_ms'] / 1000:.1f} s"),
        ("Jev decisions", str(summary["jev_calls"])),
        ("Model cost", f"US${summary['cost_usd']:.4f}"),
        check,
    ]
    return headline, detail, rows


def middle_cut(events, targets, inputs=()):
    """(from, to, skipped) in host time: after the first performance until the last one begins.

    The last performance begins with its first input's caption, 0.3 s before the input, so the cut runs
    to that moment, and never ends before the performance before it did.
    """
    ends = {}
    for e in events:
        if e.get("performance") in targets:
            ends[e["performance"]] = max(ends.get(e["performance"], e["t"]), e["t"])
    if len(targets) < 3 or any(p not in ends for p in (targets[0], targets[-2])):
        return None
    before_last = ends[targets[-2]]
    first_input = min((i["started"] for i in inputs if i["started"] > before_last), default=before_last)
    return ends[targets[0]] + 0.3, max(before_last, first_input - CAPTION_LEAD), len(targets) - 2


def skip_card(last, skipped, seconds, scale):
    """Shown where the middle performances are cut: how many, and how much real time the cut covers."""
    k = scale * 2
    card = last.filter(ImageFilter.GaussianBlur(12 * k))
    card = Image.blend(card, Image.new("RGB", card.size, INK), 0.72)
    d = ImageDraw.Draw(card)
    cx, cy = card.width // 2, round(card.height * 0.42)
    d.text((cx, cy - round(60 * k)), f"+{skipped}", font=font(round(88 * k)), fill=WHITE, anchor="mm")
    d.text((cx, cy + round(20 * k)), "more performances, entered the same way", font=font(round(19 * k), bold=False),
           fill=WHITE, anchor="mm")
    d.text((cx, cy + round(62 * k)), f"SKIPPED IN THIS CUT  ·  {seconds:.1f} s REAL TIME", font=font(round(14 * k)),
           fill=GOLD, anchor="mm")
    return card


def end_card(last, summary, scale):
    """The last frame, blurred and dimmed, under the run's result and a few numbers."""
    k = scale * 2  # 1.0 at 540 wide
    card = last.filter(ImageFilter.GaussianBlur(12 * k))
    card = Image.blend(card, Image.new("RGB", card.size, INK), 0.72)
    d = ImageDraw.Draw(card)
    headline, detail, rows = results(summary)
    cx, y = card.width // 2, round(card.height * 0.26)
    d.text((cx, y), "HAMILTON LOTTERY  ·  JEV ULTRAFAST", font=font(round(15 * k)), fill=GOLD, anchor="mm")
    d.text((cx, y + round(95 * k)), headline, font=font(round(88 * k)), fill=WHITE, anchor="mm")
    d.text((cx, y + round(170 * k)), detail, font=font(round(19 * k), bold=False), fill=WHITE, anchor="mm")
    left, right = round(64 * k), card.width - round(64 * k)
    top = y + round(225 * k)
    d.line((left, top, right, top), fill=GOLD, width=max(1, round(2 * k)))
    for i, (label, value) in enumerate(rows):
        row = top + round((44 + 46 * i) * k)
        d.text((left, row), label, font=font(round(18 * k), bold=False), fill=(214, 220, 214), anchor="lm")
        d.text((right, row), value, font=font(round(20 * k)), fill=WHITE, anchor="rm")
    return card


def ring(d, x, y, r, width, alpha):
    """A gold ring with a thin dark edge, so it reads on white cards and on photos alike."""
    d.ellipse((x - r - width, y - r - width, x + r + width, y + r + width), outline=(*INK, alpha // 2), width=2)
    d.ellipse((x - r, y - r, x + r, y + r), outline=(*GOLD, alpha), width=width)


def draw_inputs(image, t, inputs, scale):
    """At each tap a dot, then a ring that grows and fades; a dot that follows each drag along its path."""
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for event in inputs:
        age = t - event["started"]
        if event["kind"] == "tap" and 0 <= age <= RIPPLE_SECONDS:
            x, y = (c * scale for c in event["points"][0])
            progress = age / RIPPLE_SECONDS
            eased = 1 - (1 - progress) ** 3
            alpha = round(255 * (1 - progress) ** 0.6)
            ring(d, x, y, (30 + 70 * eased) * scale, max(3, round(9 * scale)), alpha)
            if progress < 0.5:
                dot = 26 * scale
                d.ellipse((x - dot, y - dot, x + dot, y + dot), fill=(*GOLD, round(alpha * 0.8)),
                          outline=(*WHITE, alpha), width=max(2, round(5 * scale)))
        elif event["kind"] == "drag" and 0 <= age <= event["ended"] - event["started"] + RIPPLE_SECONDS:
            (x0, y0), (x1, y1) = ([c * scale for c in p] for p in event["points"])
            span = max(0.001, event["ended"] - event["started"])
            progress = min(1.0, age / span)
            fade = 1.0 if age <= span else 1 - (age - span) / RIPPLE_SECONDS
            x, y = x0 + (x1 - x0) * progress, y0 + (y1 - y0) * progress
            d.line((x0, y0, x, y), fill=(*GOLD, round(130 * fade)), width=max(4, round(16 * scale)))
            dot = 28 * scale
            d.ellipse((x - dot, y - dot, x + dot, y + dot), fill=(*GOLD, round(230 * fade)),
                      outline=(*WHITE, round(240 * fade)), width=max(2, round(5 * scale)))
    image.paste(layer, (0, 0), layer)


def short(step):
    """A caption for one executed step: the operation and what it acted on, in a few words."""
    label, operation = step["label"], step["operation"]
    performance = PERFORMANCE.search(label)
    if performance and label.endswith("Enter Now"):
        label = f"Enter Now · {performance.group(1)}"
    elif label.startswith("I accept the Term of Service"):
        label = "Tick: accept the rules"
    elif label.startswith("I have reviewed my profile"):
        label = "Tick: profile reviewed"
    elif operation.startswith("SCROLL"):
        label = ""
    return f"{operation}  {label}".strip() if len(label) <= 44 else f"{operation}  {label[:43]}…"


def captions(inputs, events):
    """(from, text): each input's step from just before it, and the dry-run stops and submits by name."""
    steps = [e for e in events if e["kind"] == "step"]
    out = []
    for event in inputs:
        step = next((e for e in steps if e["t"] >= event["started"]), None)
        if step:
            out.append((event["started"] - CAPTION_LEAD, short(step)))
    for e in events:
        if e["kind"] == "dry_run_stop":
            out.append((e["t"], f"DRY RUN  Stopped before Submit · {e['performance']}"))
        elif e["kind"] == "submitted":
            out.append((e["t"], f"SUBMITTED  {e['performance']}"))
    return sorted(out)


def draw_footer(canvas, top, caption, status, scale):
    """Two lines under the screen: what Jev just did, then the speed and the real elapsed time."""
    d = ImageDraw.Draw(canvas)
    d.rectangle((0, top, canvas.width, canvas.height), fill=INK)
    k = scale * 2  # 1.0 at 540 wide
    x = round(22 * k)
    d.text((x, top + round(32 * k)), caption, font=font(round(18 * k)), fill=WHITE, anchor="lm")
    d.text((x, top + round(66 * k)), status, font=font(round(13 * k)), fill=GOLD, anchor="lm")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path, help="The --output folder of a recorded examples/hamilton.py run")
    parser.add_argument("--out", type=Path, help="Default: <run>/hamilton-demo.mp4")
    parser.add_argument("--speed", type=float, default=1.0, help="Playback speed; not 1 is shown on the video")
    parser.add_argument("--width", type=int, default=540, help="Output width in pixels")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--gif", action="store_true", help="Also write a GIF next to the MP4")
    parser.add_argument("--end-seconds", type=float, default=3.0, help="How long the result card holds")
    parser.add_argument(
        "--skip-middle", action="store_true", help="Keep the first and last performance; mark the cut in between"
    )
    args = parser.parse_args()
    recording = json.loads((args.run / "recording.json").read_text())
    inputs, hidden = recording.get("inputs", []), recording["hidden"]
    out = args.out or args.run / "hamilton-demo.mp4"
    first = args.run / "screen" / recording["segments"][0]["file"]
    source_w, source_h = probe(first)
    scale = args.width / source_w
    size = (args.width, round(source_h * scale / 2) * 2)
    canvas_size = (size[0], size[1] + round(FOOTER * args.width / 540 / 2) * 2)
    lines = captions(inputs, recording["events"])
    trace = args.run / "state.json"
    summary = json.loads(trace.read_text())["summary"] if trace.exists() else None
    cut = middle_cut(recording["events"], summary["targets"], inputs) if args.skip_middle and summary else None
    if args.skip_middle and cut is None:
        raise SystemExit("--skip-middle needs a run of three or more performances with its state.json")
    cut_shown, last, skip, fade_frames = False, None, None, round(0.25 * args.fps)
    encoder = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{canvas_size[0]}x{canvas_size[1]}", "-r", str(args.fps), "-i", "-", "-c:v", "libx264",
         "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
        stdin=subprocess.PIPE,
    )
    count = 0
    for n, segment in enumerate(recording["segments"]):
        source = args.run / "screen" / segment["file"]
        until = recording["segments"][n + 1]["started"] if n + 1 < len(recording["segments"]) else float("inf")
        taps = [i for i in inputs if i["kind"] == "tap" and segment["started"] <= i["started"] < until]
        delay, spread = start_delay(source, taps, segment["started"])
        spread = "no taps to measure" if spread is None else f"spread {spread:.2f} s"
        print(f"{segment['file']}: first frame {delay:.3f} s after start ({spread})")
        # Host times map onto this video from its first frame, not from when screenrecord was started.
        segment = {**segment, "started": segment["started"] + delay}
        windows = blur_windows(hidden, segment["started"], source_h)
        # Sampling the recording at fps / speed and writing at fps plays it `speed` times faster.
        for i, image in enumerate(frames(source, size, args.fps / args.speed)):
            t = i * args.speed / args.fps
            host = segment["started"] + t
            if cut and cut[0] < host < cut[1]:
                if not cut_shown and last is not None:
                    # One card where the middle performances are left out: fade in from the last kept
                    # frame, hold, and fade out into the first frame after the cut (below).
                    skip = skip_card(last, cut[2], cut[1] - cut[0], scale)
                    for j in range(round(1.15 * args.fps)):
                        encoder.stdin.write(Image.blend(last, skip, min(1.0, (j + 1) / fade_frames)).tobytes())
                        count += 1
                    cut_shown = True
                continue
            for start, end, top, bottom in windows:
                if start <= t <= end:
                    box = (0, int(top * scale), size[0], int(bottom * scale) + 1)
                    image.paste(image.crop(box).filter(ImageFilter.GaussianBlur(14 * scale * 2)), box[:2])
            draw_inputs(image, host, inputs, scale)
            canvas = Image.new("RGB", canvas_size, INK)
            canvas.paste(image, (0, 0))
            caption = next((text for start, text in reversed(lines) if start <= host), "Jev · Hamilton lottery")
            elapsed = max(0.0, host - recording["entries_started"])
            status = f"REAL APP  ·  {args.speed:g}× SPEED  ·  {elapsed:4.1f} s real time"
            draw_footer(canvas, size[1], caption, status, scale)
            if skip is not None:
                for j in range(fade_frames):
                    encoder.stdin.write(Image.blend(skip, canvas, (j + 1) / fade_frames).tobytes())
                    count += 1
                skip = None
            encoder.stdin.write(canvas.tobytes())
            last = canvas
            count += 1
        print(f"{segment['file']}: {len(windows)} blur windows")
    if args.end_seconds > 0 and summary:
        card = end_card(last, summary, scale)
        fade = round(0.4 * args.fps)
        for i in range(round(args.end_seconds * args.fps)):
            frame = Image.blend(last, card, min(1.0, (i + 1) / fade))
            encoder.stdin.write(frame.tobytes())
            count += 1
    encoder.stdin.close()
    encoder.wait()
    print(f"wrote {out} ({count / args.fps:.1f} s at {args.speed:g}x)")
    if args.gif:
        gif = out.with_suffix(".gif")
        palette = (
            "fps=12,scale=360:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
        )
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(out), "-vf", palette, str(gif)], check=True)
        print("wrote", gif)


if __name__ == "__main__":
    main()
