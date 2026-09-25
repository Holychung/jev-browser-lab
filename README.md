<img src="docs/banner.svg" alt="Jev Ultrafast · Browser Use × TypeSafe" width="100%" />

# Jev Ultrafast ⚡

> [!IMPORTANT]
> **The Browser Use Cloud waitlist is open.** Get early access to ultrafast browser agents in the cloud.
> **[Join the waitlist →](https://browser-use.com/ultrafast?utm_source=github&utm_medium=readme&utm_campaign=jev-ultrafast)**

**A browser agent with a dynamic, indexed action space.**

Give it one goal. [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation and an element. A small LLM writes text only when the operation is `TYPE_TEXT`.

**Signed out → LinkedIn → three Broadway lottery entries in 17.5 seconds.** One run on the real Telecharge Lottery + Rush site, nothing typed, and every entry checked again on a fresh page load.

<a href="docs/telecharge-demo.mp4"><img src="docs/telecharge-demo.gif" alt="A real Telecharge run at 1× speed: sign in with LinkedIn, open Lottery, and enter three shows, with Jev's decisions beside the page" width="100%" /></a>

| Time | What happens |
| --- | --- |
| 1.8 s | Jev clicks **Sign In** on the signed-out page |
| 3.1 s | Clicks **Connect with LinkedIn** in the login dialog |
| 4.5–12.5 s | The LinkedIn window signs in with the saved session (a separate window, captioned in the video) |
| 14.4 s | The page shows the signed-in state |
| 15.2 s | Clicks **Lottery** |
| 16.0 s · 16.8 s · 17.5 s | Clicks **Enter** on Maybe Happy Ending, Oh, Mary! and Operation Mincemeat; each card confirms its entry within 15 ms |

6 actions, 6 Jev calls, no refused choices, $0.00175.

[Watch the MP4](docs/telecharge-demo.mp4) · [Run trace](docs/telecharge-measurement.json) · [Flights demo](docs/demo.mp4) · [Measurements](docs/performance.md) · [Read the loop](jev_ultrafast/agent.py)

## The action space

Every observation produces a new element table:

```text
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
[4] textbox   Departure          · empty
...
```

The operations are `CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `DONE`, and `BLOCKED`. Only supported operations and targets are offered.

```text
                      one TypeSafe request
                     ┌───────────────────────────┐
page → element table → operation                 │
                     │ click_target              │
                     │ type_text_target          │
                     │ select_target, if present │
                     └─────────────┬─────────────┘
                         use the matching target
                                   │
                    CLICK [7] ─────┤──→ browser
                TYPE_TEXT [3] ─────┘
                          ↓
                   small LLM → text → browser
```

Target questions are speculative. If the operation is `CLICK`, only `click_target` can execute. Two decisions, **one network round trip**. Each target head contains only compatible elements. Native dropdown choices carry an observed element/option index.

There are no site-specific action scripts or prepared field strings in the policy. The Flights example supplies a goal and independently verifies the outcome. The screenshot renderer adds labels afterward; it does not drive the browser.

## Try it

```bash
git clone https://github.com/browser-use/jev-ultrafast.git
cd jev-ultrafast
uv sync
cp .env.example .env
# Add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY.
uv run jev
```

Open **http://127.0.0.1:8766** and click **Start demo → Run automatically**. The inspector shows numbered elements, operation probabilities, target probabilities, and executed actions. **Choose next** pauses before execution.

Chrome connects through [Browser Harness](https://github.com/browser-use/browser-harness), installed by `uv sync`. Run `uv run browser-harness --doctor` if it needs connecting. Allow remote debugging in Chrome when prompted.

`TEXT_MODEL_API_KEY` is an OpenRouter key in the example configuration. The current demo uses `inception/mercury-2.5` with reasoning disabled. Gemini, GLM, and DeepSeek can also use the OpenAI-compatible text helper; configure the appropriate model, endpoint, and reasoning setting.

## Use the library

```python
from jev_ultrafast import Agent

with Agent(
    "https://www.google.com/travel/flights?hl=en",
    "Find one-way flights from Zurich to London on September 20, 2026, "
    "for one adult in economy. Stop when matching flight options are visible.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

Run with `uv run --env-file .env python your_script.py`. The same policy can run a different task:

```bash
uv run --env-file .env python examples/run.py \
  --url https://en.wikipedia.org/wiki/Main_Page \
  --goal 'Find and open the Wikipedia article about Gödel’s incompleteness theorems.'
```

`uv run --env-file .env python examples/flights.py --keep-open` performs the flight search, checks the actual route/date/results, and saves its trace. It does not select or book a flight.

`uv run --env-file .env python examples/telecharge.py --show 'The Great Gatsby' --when 8:00PM` enters Telecharge Lottery + Rush drawings; repeat `--show` to enter several in one run. It starts on the framed SocialToaster page, signs in only through a LinkedIn session already saved in the automation Chrome profile, and types nothing. A 4800 px viewport puts the whole list in one observation. Each Enter click waits for an approve file unless `--auto-approve` is given; code still refuses an Enter on any other card, never clicks one twice, and reads the result again from a fresh page load.

The same loop drives Android apps. [android.py](jev_ultrafast/android.py) reads the accessibility tree through uiautomator2, which does not wait for the screen to go idle, and taps observed nodes with adb; install it with `uv sync --extra android`. `uv run --env-file .env --extra android python examples/hamilton.py --all --dry-run` walks every open Broadway performance in the official Hamilton app on an emulator: it opens each entry page, keeps two tickets, ticks both confirmation boxes, and stops before Submit Your Entry, then restarts the app and reads the list again. Without `--dry-run`, each submit waits for an approve file unless `--auto-approve` is given. The app must already be signed in; nothing is typed, and the entrant's name is kept from the model. The app's Official Rules (Broadway Direct) say an entry may be disqualified for automated or programmed methods, so read them before submitting.

## Why it moves

- **One request per decision cycle.** Operation and target heads share the same observed state.
- **No screenshots in the default agent loop.** Jev consumes structured state. The inspector opts into screenshots; the video uses a separate continuous screencast.
- **One browser call per snapshot.** Read visible controls, their names, values, and text atomically. Keep references to the actual DOM nodes.
- **Validate the selected target.** Clicks check the document, form values, target, and nearby context. Animation alone does not force another prediction. Resolve current geometry and reject covered controls before input.
- **Wait for useful state.** After typing into a combobox, wait for visible suggestions, capped at 200 ms. Other interactions get at most two animation frames or 50 ms. These reads happen after execution is logged.
- **Keep hidden tabs rendering.** Focus emulation prevents background animation throttling without switching Chrome's visible tab.
- **Send visible text.** Offscreen article bodies and footers do not fill the model context.
- **Reuse an interrupted text request.** A generated value survives a stale-page retry only if the entire text-helper input is unchanged.

Every executed target is resolved from an observed node. The executor rechecks page freshness and click occlusion. Model output never becomes selectors, coordinates, shell commands, or executable JavaScript. Text-helper output must parse as a small JSON object before typing.

## Small enough to read

| File | Job |
| --- | --- |
| [agent.py](jev_ultrafast/agent.py) | The complete loop and text-helper handoff |
| [snapshot.js](jev_ultrafast/snapshot.js) | Atomic DOM snapshot, indexed controls, freshness guards |
| [browser.py](jev_ultrafast/browser.py) | Browser connection, current geometry, execution |
| [android.py](jev_ultrafast/android.py) | The same page shape from an Android app's accessibility tree |
| [model.py](jev_ultrafast/model.py) | Dynamic operation/target heads and text generation |
| [questions.py](jev_ultrafast/questions.py) | Model instructions |
| [demo.py](jev_ultrafast/demo.py) | Local inspector |

## Evidence and limits

The Telecharge video covers **17,459 ms**, from the first recorded frame to the third confirmed entry, at 1×. It started with no Telecharge session; the automation Chrome profile already held LinkedIn and Google sessions. The LinkedIn window accounts for 7.9 s and cannot appear in the screencast of the page's own tab. Each entry was confirmed on its card, and a fresh page load then reported all three entered. For the last 0.6 s Chrome sent frames of the page top instead of the full 4800 px view, so the final Enter click is not visible. This is one run, not a reliability benchmark; its trace is [telecharge-measurement.json](docs/telecharge-measurement.json).

**Zürich → London on Google Flights in 7.1 seconds.** The [Flights video](docs/demo.mp4) is a **7,073 ms** run. Timing starts after initial page observation and includes model calls, generated text, browser work, stale decisions, and loading waits. A fresh independent check verifies the one-way setting, Zürich, London, September 20, 2026, and visible flight options. The video plays at 1×, with no opening hold and a 0.5-second final hold.

In six alternating runs with identical models and settings, both versions passed **3/3**. Median task time went from **9.450 s → 7.092 s**, a **25% reduction**; median browser protocol calls went from **1,092 → 101**. This is three repeats of one task on one browser profile, not a general reliability benchmark.

The same policy opened the requested Wikipedia article in **2.798 s** and passed a local hotel search/filter task in **1.896 s**. Runs, failures, source hashes, and measurement boundaries are in [performance.md](docs/performance.md).

A `DONE` choice still requires independent outcome verification. The DOM reader handles common HTML and ARIA controls, not the full accessible-name specification. Shadow roots, frames, canvas, uploads, pop-up tabs, nested scrolling, and arbitrary keyboard widgets remain outside this MVP. Owned tabs share the existing Chrome profile. On Android the tree holds only what is on screen and no scroll offsets, so the end of a list is known once a scroll there changed nothing; typing and WebView content are not supported yet.

## Development

```bash
uv run ruff check .
uv run pytest
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
uv build
```

Tests are offline. `uv run python scripts/check_guards.py` checks real controls in a local browser without model calls. Live examples and recording scripts make paid API calls. `scripts/record_flights.py <new-folder>` captures original browser timestamps; `scripts/render_demo.py <recording-folder>` renders that verified run at 1× and crops out the Google account strip. `examples/telecharge.py --record` and `scripts/render_telecharge.py <recording-folder>` do the same for a lottery entry: the camera pans over the tall view, and the account name, e-mail and phone are blurred at capture. `examples/hamilton.py --record` saves the emulator's own screen, and `scripts/render_hamilton.py <run> --speed 2` renders it with the entrant's name blurred, each tap and drag marked, and a caption bar that names each step and prints the speed beside the real elapsed time. Credentials and raw traces stay ignored.

## Disclaimer

This is a personal project, shared for learning and reference. It is not affiliated with, endorsed by, or supported by Telecharge, Lucky Seat, SocialToaster, Hamilton, Broadway Direct, LinkedIn, TypeSafe, or Browser Use.

The lottery examples act on real websites with your own accounts. Check each site's terms before you run them; some sites do not allow automated entries. You are responsible for how you use this code, including any suspended account or lost entry. The software is provided as is, without warranty, under the [MIT License](LICENSE).

---

[Browser Use](https://github.com/browser-use/browser-use) · [Browser Harness](https://github.com/browser-use/browser-harness) · [TypeSafe speculative fan-out](https://docs.typesafe.ai/patterns/fan-out)
