# Automation

For anyone who wants an app on many Androids to do the same task without a person tapping, and who may not write code.

Automation works on any app. Nothing in the code knows a particular app: a task is recorded or written in terms of what is on the screen, and it runs on every Android you select, each on its own. A device that meets a screen nobody planned for stops and waits for a person while the others carry on.

There are two ways to make a task, and they run the same way:

- Record a task a few times on the live view in the panel and train the recordings into a program.
- Write a flow, a short YAML script, by hand.

For the AI Assistant and the Pilot, which can operate Androids and teach tasks for you, see [AI assistants](AI.md).

## How elements are found

Taps do not use fixed coordinates as the first choice. Before every step the farm reads the screen's view hierarchy, the tree of elements Android keeps for accessibility, and looks for the element the step means.

A trained program carries evidence about each element, not an exact description, because an app update can rename an id, reuse one id for every row of a list, or draw the same view one level deeper. Each kind of evidence adds to a score and the best element above a threshold wins:

| Evidence | Score |
| --- | --- |
| Same resource id | 5 |
| Same text | 4 (3.5 if only the case differs, 1.5 if it contains the recorded text) |
| Same content description | 3 (2.5 if only the case differs) |
| Same position in the tree | 3 (1.5 if the last two levels match) |
| Same class | 0.5 |
| Near the recorded spot | up to 1 |
| Id present on both and different | minus 1 |

An element is acted on at a score of `automation.match_threshold` (default 3.0) or more. Among equals, the one nearest to where it was recorded wins, which tells two "Go" labels apart.

A flow uses exact selectors instead. The author says precisely what to look for, and a near miss must not be acted on.

How the hierarchy is read is `automation.hierarchy_reader`. `uiautomator` is the built-in dump, which waits for the screen to go idle. `u2` is a persistent reader that never waits, which matters on screens that never go idle, such as video; install it with `pip install "uiautomator2>=3.2"` in the farm's virtual environment (the `u2` extra). `auto` uses `u2` when it is installed. On a busy screen the persistent reader was about 20 times faster than the built-in dump.

## Record a task

Recording happens in the panel, because it needs the live view. The panel sends each tap to the farm, which reads the screen first, works out which element is under the point, saves a picture of the area, and only then sends the tap. Sampling the screen on a timer would attribute a tap to the previous screen when you tap right after a change. Every step is saved as it happens, so a crash loses nothing.

1. Start an Android and open Automation, then the Record & train tab.
2. Create a dataset: a name for the task and the app's package name. A dataset groups recordings of one task.
3. Choose the dataset and a running Android and press Start recording.
4. Do the task on the live view. Use the buttons beside it for what a tap cannot say: Type a named value, Launch app, Wait, "Next tap only checks the element is visible", Undo last step.
5. Press Stop and save. Record the same task two more times.

What gets recorded about typing matters. What a person typed into a field is content, not identity, so the element's text is blanked for editable and password fields. Typed text is saved literally unless you give it a name: "Type a named value" types the value now but saves only its name, and the value is supplied for each Android when the program runs. The recorder refuses to save literal text when the focused field looks like a password field (or when the screen cannot be read to check), and asks for a named value instead. Names containing words from `automation.forbidden_field_words` (`password`, `passwd`, `pin`, `otp`, `2fa`, `seed`, `card`, `cvv`, `ssn` by default) count as secrets.

Recording is a panel feature. The CLI lists datasets (`sf auto datasets`) and trains them (`sf auto train`), and the API has the recording routes under `/api/automation/record`.

## Train recordings into a program

Training is not model training. A handful of demonstrations cannot tune anything. It is a consensus over the recordings, in two passes:

1. Consensus, deterministic. The recordings are lined up step by step by similarity (same kind, same element, how close the taps were, order), because the same tap can be captured with a different element in different recordings. The recording that agrees most with the others is the template. A step is kept when at least half the recordings made it, its element is decided by majority vote, and its coordinates are the median, so one bad capture is outvoted instead of dropping the step. This pass alone produces a runnable program.
2. A model, optional. If a model is configured it writes what each step is for. It can only annotate. It never adds, reorders or invents a step. Without a model the program is equally valid, only less described.

```bash
sf auto datasets
sf auto train open-settings
sf auto programs
```

In the panel, press Train on a dataset, then see the result under Programs, where Steps shows the outline and flags steps that can be found only by position.

Read the warnings training prints. They are the product: a step that silently vanished is how a program works in the test and fails on the fleet. They say, among others:

- the program was built from a single recording, so nothing could be cross-checked;
- with fewer than three recordings, one bad capture can outvote the rest;
- steps that appeared in fewer than half the recordings were left out;
- steps were not in every recording and will be skipped when their element is missing (optional steps);
- taps have no id or text and can be found only by position, or the recordings tapped far apart at a step with nothing to tell them apart;
- the recordings were made in different apps, or nothing could be derived because they may be of different tasks.

## Programs

A trained program is stored under `<home>/automation/programs/<name>/`. Each step has a selector voted from the elements touched, the alternatives the other recordings saw, the device coordinates as a fallback, a picture of the area, and whether it appeared in every recording.

When a step runs, the farm resolves it in this order:

1. The step's element by score, then the alternatives the other recordings saw.
2. The recorded coordinates, but only if the saved picture of that area still matches the live screen.
3. A fix remembered for this app and screen.
4. A model, if one is configured, for a bounded number of attempts.
5. A person.

Run a program on a selection:

```bash
sf auto run program open-settings 1-10
sf auto run program sign-in tag:eu --var user=demo --secret password
```

## Flows

A flow is a YAML file of steps. It says what to do in terms of what is on screen and never where. This is the shipped `examples/settings-open-entry`:

```yaml
name: settings-open-entry
description: Open Settings, scroll to an entry on its home screen, open it and wait for its page.
package: com.android.settings
variables:
  entry: Accessibility     # the exact title of an entry on the Settings home screen
timeouts:
  step: 30
  run: 180
steps:
  - launch: {fresh: true}
  - wait_for: {id: homepage_title}
    label: wait for the Settings home screen
  - scroll_to: {id: title, text: "{entry}"}
    max_swipes: 10
  - tap: {id: title, text: "{entry}"}
  - wait_for: {id: collapsing_toolbar, desc: "{entry}"}
    label: wait for the page of the entry
```

Wait for an element, never for a fixed delay. `{entry}` is a variable: it has a default here, and `variables: {name: null}` means the run must supply it. The flow does not use the Settings search bar, because the default package trim disables the app behind it; list `com.android.settings.intelligence` in `performance.trim_keep` to keep the search bar. Everything is validated when the flow is loaded. An unknown key, a step with two actions or a bad regular expression is an error with its position, not a surprise halfway through a run on forty devices. YAML aliases are refused, and a flow is limited to 256 KB.

Manage flows:

```bash
sf auto flows ls                        # includes the shipped examples
sf auto flows show examples/open-app
sf auto flows validate login.yaml       # check without saving
sf auto flows save login login.yaml
sf auto flows rm login
sf auto run flow login 1-10 --var user=demo --dry-run
```

In the panel: Automation, then the Flows tab. Pick a flow, edit, Validate, Save, Run…. The bundled examples are read-only; change the name to save your own copy.

### Shipped examples

| Flow | What it does |
| --- | --- |
| `examples/open-app` | Starts an app from a clean state and waits until it is in front. Variable `package`. |
| `examples/settings-open-entry` | Opens Android Settings, scrolls its home screen to an entry, opens it and waits for its page. Variable `entry` (default `Accessibility`). |
| `examples/scroll-until-visible` | Scrolls the current list down until some text is on screen, and fails clearly at the end of the list or after 15 swipes. Variable `text`. |
| `examples/type-into-focused-field` | Replaces the content of the focused text field with a value. Variable `text`. |
| `examples/dismiss-permission-dialog` | Allows or denies a runtime permission dialog if one is showing, and does nothing when none is. Variable `answer` (`allow` or `deny`). Another flow can `call` it. |

The files are in `superfarm/data/flows/examples/`, with comments that explain each step.

### Step reference

The block below is generated from the models in `superfarm/automation/flows.py` by `scripts/gen_flow_docs.py`. It is current by construction.

<!-- BEGIN GENERATED: flow reference (scripts/gen_flow_docs.py). Do not edit by hand. -->

### Selectors

A selector describes one element on the live screen. It is a mapping with any of these keys, a bare string (exact visible text, or `id:`, `text:`, `desc:` and `regex:` prefixes as a shorthand), or `@name` to use a role taught in the app's profile. Text values may contain `{variables}`. A selector needs at least one of the identifying or flag keys, and `text` and `text_regex` cannot be combined.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | text |  | Resource id: the short name (`title`) or the full `package:id/title`. (length at most 300) |
| `text` | text |  | Exact visible text. (length at most 500) |
| `text_regex` | text |  | Regular expression the whole visible text must match. (length at most 500) |
| `desc` | text |  | Exact content description (accessibility label). (length at most 500) |
| `cls` | text |  | Class: short (`Button`) or full (`android.widget.Button`). (length at most 200) |
| `package` | text |  | Package the element belongs to. (length at most 200) |
| `editable` | true / false |  |  |
| `clickable` | true / false |  |  |
| `focused` | true / false |  |  |
| `checked` | true / false |  |  |
| `selected` | true / false |  |  |
| `enabled` | true / false |  | Default: only enabled elements match. `false` matches only disabled ones. |
| `index` | whole number |  | Which match to use when several fit (0 = first, -1 = last). (at least -50, at most 200) |
| `within` | selector |  | Only elements inside one that matches this selector. |
| `role` | text |  | Name of a selector taught in the app profile (written `@name`). |

### Conditions

A condition says what must be true on screen. A bare selector means `visible`. A condition names exactly one of these keys.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `visible` | selector |  |  |
| `gone` | selector |  |  |
| `activity` | text |  | Regular expression searched in `package/activity` of the foreground. (length at most 300) |
| `app` | text |  | Package that must be in the foreground. (length at most 200) |
| `equals` | 2 texts |  | Two texts (usually with {variables}) that must be the same. |
| `any` | list of condition |  | Length at least 1, length at most 20 |
| `all` | list of condition |  | Length at least 1, length at most 20 |
| `not` | condition |  |  |

### Steps

A step names exactly one action: `launch`, `stop_app`, `tap`, `long_press`, `type`, `clear`, `key`, `back`, `home`, `swipe`, `scroll_to`, `wait_for`, `wait_gone`, `assert`, `sleep`, `if`, `repeat`, `set`, `screenshot`, `call`, `fail`. A step that names two, or a key the action does not accept, is refused when the flow is loaded, with its position. `back` and `home` can be written as a bare word (`- home`). These keys work beside every action:

| Key | Type | Notes |
| --- | --- | --- |
| `timeout` | number | Seconds this step may wait for what it needs, more than 0 and at most 3600. Default: `timeouts.step` of the flow, then `automation.step_timeout`. |
| `optional` | true / false | Default `false`. When true, a step that cannot be done on the screen is skipped and counted, instead of stopping this Android to ask a person. |
| `label` | text | Name shown in the run's progress instead of the generated description (up to 120 characters). |

#### launch

The bare value is `package`: `launch: <package>`. A mapping with the keys below also works.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `package` | text |  | App to start. Default: the flow's package. |
| `activity` | text |  | Activity to start instead of the launcher one. |
| `fresh` | true / false | `false` | Force-stop the app first, so it starts from its first screen. |
| `wait` | condition |  | What must be true before the next step. |

#### stop_app

The bare value is `package`: `stop_app: <package>`. A mapping with the keys below also works.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `package` | text |  |  |

#### tap

The value is a selector: `tap: <selector>`.

#### long_press

The value is a selector: `long_press: <selector>`.

Keys beside the action:

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `ms` | whole number | `700` | At least 300, at most 10000 |

#### type

The bare value is `text`: `type: <text>`. A mapping with the keys below also works.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `text` | text | required | Length at most 10000 |
| `into` | selector |  | Field to tap first. Default: whatever has focus. |
| `clear` | true / false | `false` | Empty the field before typing. |
| `enter` | true / false | `false` | Press Enter afterwards. |
| `secret` | true / false | `false` | Never show the typed text in logs. |

#### clear

The value is a selector: `clear: <selector>`.

#### key

The bare value is `name`: `key: <name>`. A mapping with the keys below also works.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | text | required | Matches `^[A-Za-z0-9_]{1,40}$` |

#### back

The value is a mapping: `back: {key: value}`.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `until` | condition |  | Press Back again (after watching the screen) until this holds. |
| `presses` | whole number | `1` | At least 1, at most 5 |

#### home

Takes no arguments: `- home`.

#### swipe

The bare value is `direction`: `swipe: <direction>`. A mapping with the keys below also works.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `direction` | `up` or `down` or `left` or `right` |  | Where the finger moves. |
| `distance` | number | `0.5` | Fraction of the screen (or of `in`) to travel. (more than 0, at most 0.9) |
| `from` | [x, y] as fractions of the screen |  | [x, y] as fractions 0..1 of the screen. |
| `to` | [x, y] as fractions of the screen |  |  |
| `ms` | whole number | `300` | At least 50, at most 10000 |
| `in` | selector |  | Swipe inside this element instead of the whole screen. |

#### scroll_to

The value is a selector: `scroll_to: <selector>`.

Keys beside the action:

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `direction` | `up` or `down` or `left` or `right` | `down` | Which way to look: down = further down the list. |
| `max_swipes` | whole number | `10` | At least 1, at most 100 |
| `in` | selector |  |  |

#### wait_for

The value is a condition: `wait_for: <condition>`.

#### wait_gone

The value is a selector: `wait_gone: <selector>`.

#### assert

The value is a condition: `assert: <condition>`.

Keys beside the action:

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `message` | text |  | Length at most 300 |

#### sleep

The bare value is `seconds`: `sleep: <seconds>`. A mapping with the keys below also works.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `seconds` | number | required | At least 0, at most 600 |

#### if

The value is a condition: `if: <condition>`.

Keys beside the action:

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `then` | list of steps |  | list of steps run when the condition holds |
| `else` | list of steps |  | list of steps run when it does not |

#### repeat

The bare value is `times`: `repeat: <times>`. A mapping with the keys below also works.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `times` | whole number | required | Upper limit of rounds; there is no unbounded loop. (at least 1, at most 10000) |
| `until` | condition |  | Stop as soon as this holds. If it never does, the step fails. |

These keys may also be written beside the action, as in the shipped examples: `until`.

Keys beside the action:

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `steps` | list of steps |  | list of steps that make up one round |

#### set

The value is a mapping: `set: {key: value}`.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `var` | text | required |  |
| `from` | selector |  | Element whose text is captured. |
| `attr` | `text` or `desc` or `id` | `text` |  |
| `value` | text |  | Literal (may contain {variables}). (length at most 10000) |
| `pool` | text |  | Take a fresh line from this pool. |
| `regex` | text |  | Keep only the first group (or the whole match). (length at most 300) |

#### screenshot

The bare value is `name`: `screenshot: <name>`. A mapping with the keys below also works.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | text |  | Matches `^[A-Za-z0-9_-]{0,40}$` |

#### call

The bare value is `flow`: `call: <flow>`. A mapping with the keys below also works.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `flow` | text | required |  |
| `with` | map of text to text |  |  |

These keys may also be written beside the action, as in the shipped examples: `with`.

#### fail

The bare value is `message`: `fail: <message>`. A mapping with the keys below also works.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `message` | text | `the flow asked to stop` | Length at most 300 |

### Pop-up rules

`overlays` in a flow, and in an app profile, are pop-up rules. A rule fires only when its `guard` element and its `dismiss` control are both on screen, or when `key: back` is used with a guard that has an `id`.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | text | required | What this pop-up is, for logs. (length at least 1, length at most 60) |
| `guard` | selector | required | Element that proves the pop-up is showing. |
| `dismiss` | selector |  | Control that closes it. Tapped only while the guard is also visible. |
| `key` | `back` |  | Close with the Back key instead, for a pop-up with no close control. |
| `max_dismissals` | whole number | `2` | Attempts per step before the run stops instead of looping. (at least 1, at most 10) |

### Top-level keys of a flow

| Key | Meaning |
| --- | --- |
| `name` | Flow name. Lower-case letters, digits and `-`, up to 64 characters. |
| `description` | Free text, up to 2000 characters. |
| `package` | App the flow is for. Its profile (roles, pop-up rules, never-tap list) applies. |
| `variables` | Names with a default; `null` means the run must supply it. A default for a name that looks like a secret is refused. |
| `overlays` | Pop-up rules, see above. They are added to the profile's. |
| `never_tap` | Selectors no step may tap. Added to the profile's. |
| `on_unknown` | `stop` (default): a screen nothing covers stops this Android and asks a person. `continue`: skip the step. |
| `timeouts` | `step` (seconds, up to 3600) and `run` (seconds, up to 86400). |
| `steps` | The steps. At least one, at most 2000, nested at most 12 levels. |

### App profile keys

A profile belongs to one package and is stored as `automation/profiles/<package>.yaml`.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `package` | text | required |  |
| `roles` | map of text to selector |  |  |
| `overlays` | list of OverlayRule |  |  |
| `never_tap` | list of selector |  |  |
| `notes` | text |  | Length at most 4000 |

<!-- END GENERATED: flow reference -->

## App profiles

A profile holds what the farm has been taught about one app, so every flow and trained program for that app shares it. It lives in `<home>/automation/profiles/<package>.yaml`.

| Part | What it is |
| --- | --- |
| Roles | Named exact selectors, such as `search_button`. A flow refers to one as `@search_button`. When an app update renames an id, you re-teach one role and every flow keeps working. |
| Overlays | Pop-ups to clear whenever they appear. A rule needs an exact guard element and a dismiss control, and fires only when both are on screen. A guard that is only text once matched a permanent label on the page behind and dismissed "it" forever. A pop-up with no control can be closed with the Back key, but only with a guard that has an `id`. After `max_dismissals` tries (default 2) in one step, the run stops instead of looping. |
| Never-tap list | Elements no run may ever tap, whatever a recording, a flow or a model says: "Delete account", "Buy now". Every tap passes it, including taps a recovery proposes. |

A flow with a `package` gets that app's roles, overlays and never-tap list in addition to its own. A trained program gets the profile of its package. Roles are taught from a live screen: read the Android's elements, pick one and give it a name; the farm stores a selector for it, or tells you why the element has nothing stable to name it by.

Profiles are managed through the API under `/api/automation/profiles/{package}` (roles, overlays, never-tap, and `teach` to turn an element of a live screen into a role). There is no panel page or CLI command for profiles in this version. [API reference](API.md) lists the routes.

## Values and pools

Anything in braces in a flow, `{name}`, is a value looked up for each Android in this order:

1. The variables of the run: the flow's defaults, then what you passed with `--var`, then anything a `set` step captured.
2. The built-ins `instance_id` and `serial`.
3. `pool.<name>`: one line taken from that pool, the same line for the rest of the run.
4. The Android's own value of that name.
5. The shared value of that name.

| Kind | Meaning |
| --- | --- |
| Shared | One value per name, the same on every Android. Reading it never consumes it. |
| Per Android | One value per name per Android, such as a nickname or a city. |
| Pool | A list of lines where each use takes one for good, such as invite codes. The line is written to a used-journal before it is removed, so a crash can lose a value but never hand it out twice. A practice run leaves pools alone. |

```bash
sf auto values set city Berlin
sf auto values set --instance 3 nickname demo
sf auto values pool-add codes codes.txt        # one value per line
sf auto values ls
```

Names that look like secrets (`password`, `pin`, `otp` and the other `automation.forbidden_field_words`) are refused by the store. The values folder is plain files and would end up in backups and model prompts. Pass such a value for one run with `--secret NAME`, which asks without echo or reads stdin. It lives in that run's memory only. A flow default or a schedule variable with a secret-looking name is refused in the same way.

Only printable ASCII is typed by flows and programs. Text with other characters stops the step with an explanation, because silently typing something else would be worse. The panel's own typing on the live view handles any text by pasting.

## Runs

Starting a run makes one task. Each Android in it has its own state and thread, `automation.max_parallel` (8) Androids work at the same time, and the rest wait as `queued`.

| State | Meaning |
| --- | --- |
| `queued` | Waiting for a free slot. |
| `running` | Working. |
| `done` | Finished. A message says if steps were skipped. |
| `failed` | Ended with an error: a `fail` step, the run's time limit passed, or someone chose Stop this Android. |
| `needs_attention` | A screen nothing covers. This Android waits for a person and gives its slot to a queued one. |
| `cancelled` | Cancelled. |

```bash
sf auto run flow examples/open-app 1-10 --var package=com.android.settings
sf auto run flow examples/open-app 1-10 --var package=com.android.settings --dry-run
sf auto runs                 # all runs
sf auto runs t12             # one run, Android by Android
sf auto resolve t12 4 resume # resume | skip | abort
```

A dry run reads the screens and matches every element but taps, types and presses nothing, and leaves pools untouched. It reports the steps that would not be possible on the current screen. Use it before the first real run on a fleet.

When an Android needs attention, look at it on the Control page or the live view, fix the screen by hand if that is the answer, and choose Resume (carry on), Skip this step, or Stop this Android. In the panel these are buttons on the run in the Runs tab. An Android that waits longer than an hour gives up and ends as `needs_attention`. Whether a flow asks a person or skips is its `on_unknown` setting: `stop` asks, `continue` skips. A step marked `optional` is skipped, and an `assert` always stops.

Every wait has a deadline (`automation.step_timeout` 20 s, `automation.run_timeout` 900 s per Android, and per-flow `timeouts`). After each action the farm pauses `automation.settle_seconds` (0.6) before reading the screen again. With `automation.human_jitter` on (default) tap positions and pauses vary slightly, as a person's do.

The state of runs is kept in memory. It is lost when the daemon restarts, and the daemon keeps the most recent 50 runs. A restart does not stop the Androids themselves.

## Schedules

```bash
sf auto schedule add nightly flow examples/open-app all --cron '0 3 * * *' --var package=com.android.settings
sf auto schedule add poll program check-feed tag:eu --every 1800
sf auto schedule ls
sf auto schedule disable s3
```

A schedule is either `--every SECONDS` (at least 30) or a cron line `minute hour day-of-month month day-of-week`, in the host's local time. Fields take `*`, a number, a range, a list or a step such as `*/5`; day of week is 0 to 7 with 0 and 7 both Sunday. As in cron, when both day fields are restricted either one counts.

Schedules are deliberately small. A schedule never overlaps itself: if its previous run is still going when it comes due again, that occurrence is skipped and counted. Occurrences missed while the daemon was down are not replayed; one run starts if it is overdue. The daemon checks about once a minute. Schedules are stored on disk, and the variables kept with one may not be secrets.

In the panel: Automation, then Schedules.

## A model for recovery

Automation is deterministic without a model (`automation.llm.provider: none`, the default). A model can do two bounded jobs and nothing else:

- Annotate a trained program: say in plain words what each step is for. It cannot add, remove or reorder steps.
- Recover a stuck step: propose a few actions (tap one of the elements it was shown, press Back or Escape, swipe, wait, ask a person) to clear an obstacle. It points at elements by their number in the list it was given. Coordinates it makes up are not accepted, every tap still passes the never-tap list, and it gets a bounded number of attempts (two per step) before a person is asked.

Screen text is sent to the model as data, with the contents of fields and anything named like a secret removed first. Everything the model returns is parsed as strict JSON and cut down to those shapes; anything else is dropped.

When a recovery works, `automation.learn_fixes` (on by default) remembers it, keyed by the app and the screen, and replays it on the next device without asking again. The same obstacles turn up on every device of a farm, so a fix learned on one is found by all.

Configure the model with `automation.llm.provider` (`ollama` or `openai`, meaning any OpenAI-compatible endpoint), `base_url`, `model`, `api_key_secret`, `timeout`, `temperature` and `num_ctx`. The same setting is available as the model profile named `automation` for the Assistant and the Pilot ([AI assistants](AI.md)). With no model, a stuck Android goes straight to a person.

## Settings

All under `automation.`, with defaults. Details and ranges are in [Configuration reference](CONFIGURATION.md#automation).

| Setting | Default | Meaning |
| --- | --- | --- |
| `max_parallel` | 8 | Androids one run works on at the same time. |
| `hierarchy_reader` | `auto` | `uiautomator`, `u2` or `auto`. |
| `read_timeout` | 30 | Hard limit in seconds for one read of the screen. |
| `step_timeout` | 20 | Seconds a step waits for its element before recovery starts. |
| `run_timeout` | 900 | Hard limit in seconds for one Android's whole run. |
| `match_threshold` | 3.0 | Score an element needs to be acted on in a trained program. |
| `settle_seconds` | 0.6 | Pause after each action. |
| `human_jitter` | `true` | Vary tap positions and pauses slightly. |
| `learn_fixes` | `true` | Remember recoveries and replay them. |
| `stop_on_unknown_screen` | `true` | A screen no rule covers stops that Android with `needs_attention` instead of guessing. |
| `forbidden_field_words` | see above | Names containing these are never written to disk or sent to a model. |
| `llm.*` | provider `none` | The optional model. |

## Verified and not verified

Verified on the test machine: recording three times, training by consensus and replaying the program; the flows interpreter; the never-tap list; dry runs; hand-off of a stuck Android to a person.

Not verified: more than two real Androids driven by a run at once through Super Farm itself (fleet behaviour is covered by unit tests with fakes), Android versions other than 13, and apps other than Android's own Settings and the video app used for measurements.

## Next

- [AI assistants](AI.md): the Assistant and the Pilot teach and run tasks for you.
- [MCP server](MCP.md): let an assistant run flows with your approval.
- [Apps](APPS.md): install the app first.
- [API reference](API.md): the routes behind runs, flows, values, profiles and schedules.
- [Command reference](CLI.md#sf-auto): `sf auto`.
