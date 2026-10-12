# API reference

For developers who call the Super Farm HTTP API from scripts or other programs. The `sf` command and the web panel are both clients of this API.

The conventions below are written by hand. The [route reference](#route-reference) after them is generated from the application's OpenAPI schema by `scripts/gen_api_docs.py`, so it lists every route the daemon serves.

## Conventions

### Base URL and format

The daemon listens on `http://127.0.0.1:7700` by default (`server.host`, `server.port`). Everything under `/api` takes and returns JSON, except uploads, downloads and streams. A request body that names a field the route does not know is refused with `422`, so a typo never passes silently. Every `/api` response carries `Cache-Control: no-store`.

JSON request bodies are limited to 4 MiB (`413 too_large` above that). Uploads are limited by `server.max_upload_mb` instead and are checked on the bytes that arrive, not on the length the client announces.

The OpenAPI schema is served at `GET /api/openapi.json` (authentication required). The interactive documentation pages are off, because they load scripts from a CDN that the panel's content policy forbids. `GET /api/config/schema` returns the JSON schema of the settings.

### Authentication

Every route needs authentication except `GET /api/health`, `POST /api/auth/login` and `POST /api/auth/logout`. There are two ways in. Both prove knowledge of the same secret, the API token:

- Scripts send `Authorization: Bearer <token>`.
- Browsers log in once with `POST /api/auth/login` and a body of `{"token": "<token>"}`, and then carry a session cookie.

Print the token with `sf token`. It lives in `<home>/secrets/api-token` with mode 0600. `sf token --rotate` replaces it, and every browser session and open WebSocket opened with the old one is ended.

The token is never accepted in a URL or query string, and never written to a log. Comparison is constant-time. Wrong tokens are counted per client address: after `server.login_attempts_per_minute` (10) wrong ones in a minute, that address gets `429 rate_limited` with a `Retry-After` header for the rest of the minute. The right token is always accepted, so another process on the same address cannot lock its holder out.

Whether a caller must authenticate at all is decided from the address the daemon is bound to. With `server.auth_required` off and a loopback address, requests need no credentials. A daemon bound to any other address always asks for the token.

The token is equivalent to root on the host. See [Security model](SECURITY_MODEL.md).

### Sessions and CSRF

`POST /api/auth/login` sets two cookies, both `SameSite=Strict`:

| Cookie | Readable by the page | Purpose |
| --- | --- | --- |
| `sf_session` | no (`HttpOnly`) | the session |
| `sf_csrf` | yes | the CSRF value |

The response also returns the CSRF value as `csrf`. Every request that changes something (any method other than GET, HEAD and OPTIONS) and is authenticated by cookie must send that value in the `X-SF-CSRF` header, or it is refused with `403 csrf`. Requests that use a bearer token do not need it. Sessions last `server.session_hours` (12) and are kept in memory, so a daemon restart signs every browser out.

### Host and Origin checks

The daemon refuses a request whose `Host` header is not one it was configured to answer to (`403 forbidden`). That closes DNS rebinding. Loopback names are always accepted, as are the address in `server.host` and the hosts of `server.allowed_origins`. When the daemon listens on all interfaces, any literal IP address is accepted.

A browser request that carries an `Origin` header must name this server or an entry of `server.allowed_origins`, or it is refused. Clients that are not browsers send no `Origin` and are unaffected. CORS headers are added only for the origins listed in `server.allowed_origins`. Behind a reverse proxy, list the proxy in `server.trusted_proxies` so the client address in the audit log is the real one.

### Errors

Every error has one shape:

```json
{"error": {"code": "not_found", "message": "there is no Android 7", "detail": null}}
```

`detail` is present only when there is more to say. The `message` is written for the operator. Validation errors list `field` and `message` for each problem and never echo the rejected value, because a rejected body can hold a password.

| Status | `code` | Meaning |
| --- | --- | --- |
| 400 | `bad_request` | A value is wrong. |
| 401 | `unauthorized` | No credentials, or a wrong token. |
| 403 | `forbidden`, `csrf`, `immutable_setting` | Unknown Host, cross-origin request, missing CSRF header, or a settings change the API may not make. |
| 404 | `not_found` | No such Android, app, profile, task or file. An unknown `/api` path is also 404. |
| 405 | `method_not_allowed` | The route exists but not for this method; the `Allow` header lists the right ones. |
| 409 | `conflict` and others | The farm's current state forbids it. The `code` says which: `busy`, `over_capacity`, `needs_force`, `refused`, `device_unavailable`, `screen_offline`, `screen_booting`, `h264_unavailable`. |
| 413 | `too_large` | Body or upload over the limit. |
| 422 | `validation_error`, `idempotency_key_reused` | The body does not match the route's schema, or an idempotency key was reused with a different request. |
| 429 | `rate_limited` | Too many wrong tokens. |
| 500 | `internal` | A bug. The daemon log has the traceback; the response says nothing about it. |
| 502 | `command_failed`, `screen_error` | Docker, ADB or a device did not do what was asked. |
| 503 | `unavailable`, `viewer_limit` | A component cannot run on this machine (the message says why), or `stream.max_viewers` is reached. |
| 504 | `timeout` | A command ran out of time. |

### Tasks

Work that touches more than one Android, or takes more than a moment, runs as a background task. The route answers `202 Accepted` at once with the task:

```json
{"task": {"id": "t12", "kind": "...", "title": "...", "targets": [1, 2], "state": "queued",
          "current": 0, "total": 2, "message": "", "error": "", "results": {}, "failed": 0,
          "output": null, "created": 0, "started": 0, "finished": 0}}
```

`state` is one of `queued`, `running`, `done`, `failed` and `cancelled`. `results` maps each Android id to `{"ok": true|false, "detail": "..."}`, so one failing Android never hides the others' outcomes. Follow a task with `GET /api/tasks/{id}` until `state` is no longer `queued` or `running`, list tasks with `GET /api/tasks` (`?active=true`, `?limit=`), and cancel with `POST /api/tasks/{id}/cancel`. Tasks live in memory: the daemon keeps the most recent 200 and forgets the rest, and a restart forgets all of them. A task that reports `done` has finished; look at `failed` and `results` for the per-Android outcome.

The `sf` command does this for you; see `--wait` in the [command reference](CLI.md).

### The busy rule

One operator action per Android at a time. Start, stop, restart, recreate, delete, a manual backup and a restore over an Android hold that Android until their task has finished with it. While it is held, another such request that names it is refused as a whole with `409 busy`:

```json
{"error": {"code": "busy", "message": "Android 3 is busy: task t12 (...) is still working on it. ...",
           "detail": {"task": "t12", "ids": [3]}}}
```

The request is not queued and not merged with the first. A double click therefore gives one task and one refusal that points at it. An Android a task has already finished with is free again while the task goes on with the others. The same rule covers `DELETE /api/instances/{id}`.

### Idempotency

`POST /api/instances` accepts an `Idempotency-Key` header: 1 to 128 printable ASCII characters, no spaces, for example a UUID made when a form opened. Repeating the same request with the same key within ten minutes returns the first answer with the header `Idempotency-Replayed: true` and creates nothing. Sent at the same moment, the second request waits for the first. The same key with a different body gives `422 idempotency_key_reused`. If the first request failed, nothing is remembered. Keys are held in memory, at most 256 of them. Other routes ignore the header.

### Capacity and `force`

The capacity estimate (`GET /api/system/capacity`) is advice. With `performance.enforce_capacity` off (the default) a start over the estimate goes ahead. With it on, start, restart and recreate are refused with `409 over_capacity`, which carries the numbers under `error.capacity`. Repeat the request with `"force": true` to run anyway. Routes that can lose data (changing an Android's version over existing data, deleting when the safety backup fails) take `force` the same way and answer `409 needs_force` without it. See [Scaling](SCALING.md).

### Settings changes

`PATCH /api/config` takes `{"patch": {...}}`, a JSON merge patch (RFC 7396): objects merge key by key, other values replace, and `null` removes a key, which puts a setting back to its default. The response returns the new settings plus `changed`, `restart_android` and `restart_daemon` lists. `docker.binary`, `gpu.nvidia.relay_dir` and `gpu.nvidia.icd` decide which program the daemon runs, so a patch that changes them gets `403 immutable_setting`. A patch that repeats the current value passes.

### Uploads

Routes that take a file (`/api/apps/library`, `/api/files/push`, `/api/files/gallery`, `/api/net/profiles`, `/api/media`) accept either a `multipart/form-data` body with one file part plus small text fields, or a raw body whose file name comes from `?filename=`. The file streams to disk and is cut off when it passes `server.max_upload_mb`. The client's file name is reduced to a plain safe name.

### Screens

`GET /api/instances/{id}/frame.jpg` returns one JPEG, `GET /api/instances/{id}/stream.mjpg` a multipart stream of them. A stream counts as a viewer until the connection ends; `stream.max_viewers` limits how many run at once. An Android that is not running answers `409 screen_offline`, and one still booting `409 screen_booting`.

### WebSockets

Two WebSocket routes exist. A socket is accepted when the handshake carries the session cookie or a bearer header. Otherwise the first message must be `{"auth": "<token>"}` within 10 seconds. A token in the URL closes the socket with code 1008. A socket asks every 5 seconds whether its credential still stands and closes with 1008 when it does not. A browser handshake must pass the same Origin check as a request.

`/api/events` sends JSON messages, at most a few per second:

| Message | When |
| --- | --- |
| `{"type": "hello"}` | Once, after authentication. |
| `{"type": "update", "tasks": [...], "instances": [...], "removed": [ids]}` | When tasks or Android states changed. It also carries `approvals` when an AI approval request was created or decided. |
| `{"type": "resync"}` | This client fell behind: fetch the state again over HTTP. |

Events are a convenience. Everything they report can also be read with `GET /api/tasks` and `GET /api/instances`, and a client that only polls works just as well. Fetch the state after `hello`, then apply updates.

`/api/instances/{id}/h264` relays an Android's own H.264 video. The server sends one JSON `info` message and then binary frames; the client sends JSON control messages (touch, tap, key, text, scroll, back, reset). It needs scrcpy, and the first message tells you when it is off (`h264_unavailable`). `GET /api/stream/capabilities` says whether it is offered.

### Audit log

Every request that changes something, or tries to, is written as one JSON line to `<home>/logs/audit.log`: time, client address, how it authenticated, method, path, status, outcome, duration, and the Androids or task it touched. Request bodies and query strings are never written. Requests nobody authenticated go to `audit-anonymous.log`, so they cannot push the real history out. An AI bridge can label its calls with an `X-SF-Caller` header, which appears on the audit line. See `server.audit_log`, `logging.max_mb` and `logging.keep`.

<!-- The route reference below is generated by scripts/gen_api_docs.py. Do not edit it by hand. -->

## Route reference

Generated from the application's OpenAPI schema. Every route needs authentication except the ones marked public. `Body` lists the JSON fields of the request; a field marked required must be present, and fields the route does not list are refused with `422`. Routes that start work return `202` with a task (see [Tasks](#tasks)).

The API has 197 routes under 24 prefixes and 2 WebSocket routes.

| Prefix | Routes |
| --- | --- |
| [`api/health`](#apihealth) | 1 |
| [`api/auth`](#apiauth) | 3 |
| [`api/system`](#apisystem) | 5 |
| [`api/device-profiles`](#apidevice-profiles) | 1 |
| [`api/config`](#apiconfig) | 3 |
| [`api/images`](#apiimages) | 3 |
| [`api/tasks`](#apitasks) | 3 |
| [`api/net`](#apinet) | 10 |
| [`api/instances`](#apiinstances) | 20 |
| [`api/apps`](#apiapps) | 14 |
| [`api/files`](#apifiles) | 2 |
| [`api/device`](#apidevice) | 11 |
| [`api/shell`](#apishell) | 2 |
| [`api/stream`](#apistream) | 1 |
| [`api/input`](#apiinput) | 5 |
| [`api/windows`](#apiwindows) | 3 |
| [`api/camera`](#apicamera) | 7 |
| [`api/media`](#apimedia) | 2 |
| [`api/listen`](#apilisten) | 3 |
| [`api/automation`](#apiautomation) | 65 |
| [`api/backups`](#apibackups) | 8 |
| [`api/approvals`](#apiapprovals) | 8 |
| [`api/mcp`](#apimcp) | 1 |
| [`api/ai`](#apiai) | 16 |

### api/health

#### GET /api/health

Health Public: no authentication.

### api/auth

#### POST /api/auth/login

Login Public: no authentication.

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `token` | text | required; length at least 1, length at most 512 |

#### POST /api/auth/logout

Logout Public: no authentication.

#### GET /api/auth/me

Me

### api/system

#### GET /api/system

System

#### GET /api/system/capacity

Capacity

#### GET /api/system/doctor

Doctor

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `fresh` | query | true / false | optional; default `False` |

#### GET /api/system/metrics

Metrics

#### GET /api/system/gpus

The cards, every rendering choice selectable on this machine (for the per-Android and bulk pickers), and the state of the NVIDIA runtime and relay.

### api/device-profiles

#### GET /api/device-profiles

Device Profiles

### api/config

#### GET /api/config

Config Get

#### PATCH /api/config

Config Patch

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `patch` | object | required; Nested settings to change, e.g. {"android": {"fps": 25}}. |

#### GET /api/config/schema

Config Schema

### api/images

#### GET /api/images

Images

#### POST /api/images/{version}/ensure

Image Ensure

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `version` | path | text | required |

#### POST /api/images/{version}/build

Image Build

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `version` | path | text | required |

### api/tasks

#### GET /api/tasks

Tasks

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `active` | query | true / false | optional; default `False` |
| `limit` | query | whole number | optional; default `50` |

#### GET /api/tasks/{task_id}

Task

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `task_id` | path | text | required |

#### POST /api/tasks/{task_id}/cancel

Task Cancel

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `task_id` | path | text | required |

### api/net

#### GET /api/net/profiles

Profiles

#### POST /api/net/profiles

Profile Upload

#### DELETE /api/net/profiles/{name}

Profile Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

#### GET /api/net/credentials

Names only. There is no way to read a stored username or password back.

#### PUT /api/net/credentials/{name}

Credentials Set

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `username` | text | required; length at least 1, length at most 256 |
| `password` | text | required; length at least 1, length at most 1024 |

#### DELETE /api/net/credentials/{name}

Credentials Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

#### GET /api/net/gateways

Gateways

#### PUT /api/net/gateways/{name}

Gateway Put

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `provider` | `openvpn` or `wireguard` | optional; default `"openvpn"` |
| `profile` | text | required; length at least 1, length at most 200 |
| `credentials` | text | optional; default `"default"`; length at most 64 |
| `subnet` | text | optional; default `""`; length at most 43 |

#### DELETE /api/net/gateways/{name}

Gateway Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |
| `force` | query | true / false | optional; default `False` |

#### POST /api/net/gateways/{name}/{action}

Gateway Action

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |
| `action` | path | `up` or `down` | required |
| `force` | query | true / false | optional; default `False` |

### api/instances

#### GET /api/instances/{id}/net

Mode, tunnel state, kill switch and the public address. `refresh` asks the address again from inside the Android's network (a few seconds) instead of showing the last known one.

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `refresh` | query | true / false | optional; default `False` |

#### GET /api/instances/{id}/apps

Instance Apps

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `third_party` | query | true / false | optional; default `True` |

#### GET /api/instances/{id}/apps/{package}/permissions

Instance App Permissions

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `package` | path | text | required |

#### GET /api/instances/{id}/files

Files List

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `path` | query | text | optional; default `/sdcard` |

#### GET /api/instances/{id}/files/pull

Files Pull

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `path` | query | text | required |

#### GET /api/instances/{id}/info

Device Info

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### GET /api/instances/{id}/screenshot

Device Screenshot

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### GET /api/instances/{id}/logcat

`filter` keeps the lines containing that text; `priority` is the minimum level (V D I W E F).

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `lines` | query | whole number | optional; default `200` |
| `filter` | query | text (may be null) | optional |
| `priority` | query | text (may be null) | optional |
| `package` | query | text (may be null) | optional |

#### GET /api/instances/{id}/frame.jpg

Frame

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `width` | query | whole number (may be null) | optional |
| `max_age` | query | number | optional; default `1.0` |

#### GET /api/instances/{id}/screen

Screen Status

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### GET /api/instances/{id}/stream.mjpg

Mjpeg

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `fps` | query | number (may be null) | optional |
| `width` | query | whole number (may be null) | optional |

#### GET /api/instances

List Instances

#### POST /api/instances

Add Androids. With an `Idempotency-Key` header (any 1-128 printable characters, e.g. a UUID made when the form was opened) a repeat of the same request within ten minutes returns the first answer, marked `Idempotency-Replayed: true`, and creates nothing.

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `count` | whole number | optional; default `1`; at least 1, at most 500 |
| `name` | text | optional; default `""`; length at most 60 |
| `template` | object | optional; InstanceSpec fields shared by the new Androids (android_version, gpu_mode, network, tags, ...). |

#### POST /api/instances/bulk

Bulk

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `action` | `start` or `stop` or `restart` or `recreate` or `delete` | required |
| `selector` | text | required; length at least 1, length at most 500 |
| `force` | true / false | optional; default `False` |

#### GET /api/instances/{id}

Status

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### PATCH /api/instances/{id}

Update

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `patch` | object | required; InstanceSpec fields to change; applied at the next (re)start. |
| `force` | true / false | optional; default `False`; Allow a change that can lose data, such as another Android version on an existing /data. |

#### DELETE /api/instances/{id}

Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `keep_data` | query | true / false | optional; default `False` |
| `force` | query | true / false | optional; default `False` |

#### POST /api/instances/{id}/clone

Clone

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `with_data` | true / false | optional; default `False` |

#### GET /api/instances/{id}/logs

Logs

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `tail` | query | whole number | optional; default `200` |

#### POST /api/instances/{id}/{action}

Lifecycle

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `action` | path | `start` or `stop` or `restart` or `recreate` | required |

Body: see the route's description.

### api/apps

#### GET /api/apps/library

Library

#### POST /api/apps/library

Library Upload

#### DELETE /api/apps/library/{app_id}

Library Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `app_id` | path | text | required |

#### POST /api/apps/install

App Install

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `app_id` | text | required; length at least 1, length at most 200 |
| `grant` | true / false | optional; default `True` |
| `replace` | true / false | optional; default `True` |
| `downgrade` | true / false | optional; default `False` |
| `all_locales` | true / false | optional; default `False` |

#### POST /api/apps/ensure

App Ensure

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `app_id` | text | required; length at least 1, length at most 200 |
| `grant` | true / false | optional; default `True` |

#### POST /api/apps/uninstall

App Uninstall

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `package` | text | required; length at least 1, length at most 255 |
| `keep_data` | true / false | optional; default `False` |

#### POST /api/apps/launch

App Launch

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `package` | text | required; length at least 1, length at most 255 |
| `activity` | text (may be null) | optional |

#### POST /api/apps/stop

App Stop

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `package` | text | required; length at least 1, length at most 255 |

#### POST /api/apps/clear

App Clear

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `package` | text | required; length at least 1, length at most 255 |

#### POST /api/apps/enable

App Enable

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `package` | text | required; length at least 1, length at most 255 |

#### POST /api/apps/disable

App Disable

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `package` | text | required; length at least 1, length at most 255 |

#### POST /api/apps/grant

App Grant

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `package` | text | required; length at least 1, length at most 255 |
| `permission` | text | optional; default `"all"`; length at most 200; One permission name, or `all` for every runtime permission the app requests. |

#### POST /api/apps/revoke

App Revoke

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `package` | text | required; length at least 1, length at most 255 |
| `permission` | text | optional; default `"all"`; length at most 200; One permission name, or `all` for every runtime permission the app requests. |

#### GET /api/apps/matrix

package -> {instance id: version}. With `detail`, also version codes, enabled flags and the Androids that could not be read.

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `selector` | query | text | optional; default `all` |
| `detail` | query | true / false | optional; default `False` |

### api/files

#### POST /api/files/push

Upload one file and push it to every selected Android. Fields: `selector`, and `remote` (a folder ending in `/`, or a full path; default /sdcard/Download/).

#### POST /api/files/gallery

Upload one picture or video and add it to the gallery of every selected Android.

### api/device

#### POST /api/device/locale

Device Locale

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `locale` | text | required; length at least 2, length at most 35 |
| `restart` | true / false | optional; default `True`; Restart the Android framework so every app sees the new language. |

#### POST /api/device/timezone

Device Timezone

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `timezone` | text | required; length at least 1, length at most 100 |

#### POST /api/device/geo

Device Geo

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `lat` | number (may be null) | optional; Latitude. Leave out (with lon) to remove the mock location. |
| `lon` | number (may be null) | optional |
| `accuracy` | number | optional; default `10.0`; more than 0, at most 10000 |

#### POST /api/device/proxy

Device Proxy

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `proxy` | text | optional; default `""`; length at most 300; host:port, or empty to switch the proxy off. |

#### POST /api/device/clipboard

Device Clipboard

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `text` | text | required; length at least 1 |

#### POST /api/device/animations

Device Animations

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `enabled` | true / false | required |

#### POST /api/device/dark-mode

Device Dark Mode

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `enabled` | true / false | required |

#### POST /api/device/font-scale

Device Font Scale

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `scale` | number | required; at least 0.5, at most 2 |

#### POST /api/device/open-url

Device Open Url

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `url` | text | required; length at least 1, length at most 2048 |
| `package` | text (may be null) | optional |

#### POST /api/device/wake

Device Wake

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |

#### POST /api/device/reboot

Device Reboot

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `mode` | text | optional; default `"soft"`; soft: restart the Android framework. container: restart the containers. |
| `force` | true / false | optional; default `False` |

### api/shell

#### POST /api/shell

Shell Run

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |
| `argv` | list of text | required; at most items 256; The command as separate arguments; nothing is passed through a shell on the host. |
| `timeout` | number (may be null) | optional |
| `root` | true / false | optional; default `False` |
| `force` | true / false | optional; default `False`; Run a command the denylist would refuse. |

#### GET /api/shell/{task_id}/output

Shell Output

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `task_id` | path | text | required |

### api/stream

#### GET /api/stream/capabilities

Which ways of watching a device this host offers, so the panel can pick one.

### api/input

#### POST /api/input/tap

Input Tap

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `ids` | list of whole number (may be null) | optional |
| `selector` | text (may be null) | optional |
| `x` | number | required |
| `y` | number | required |
| `normalized` | true / false | optional; default `True`; Coordinates are 0..1 of the screen, so one gesture fits devices of any resolution. |

#### POST /api/input/swipe

Input Swipe

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `ids` | list of whole number (may be null) | optional |
| `selector` | text (may be null) | optional |
| `x1` | number | required |
| `y1` | number | required |
| `x2` | number | required |
| `y2` | number | required |
| `ms` | whole number | optional; default `300`; at least 1, at most 30000 |
| `normalized` | true / false | optional; default `True` |

#### POST /api/input/long-press

Input Long Press

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `ids` | list of whole number (may be null) | optional |
| `selector` | text (may be null) | optional |
| `x` | number | required |
| `y` | number | required |
| `normalized` | true / false | optional; default `True`; Coordinates are 0..1 of the screen, so one gesture fits devices of any resolution. |
| `ms` | whole number | optional; default `700`; at least 1, at most 30000 |

#### POST /api/input/text

Input Text

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `ids` | list of whole number (may be null) | optional |
| `selector` | text (may be null) | optional |
| `value` | text | required; length at least 1, length at most 10000 |

#### POST /api/input/key

Input Key

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `ids` | list of whole number (may be null) | optional |
| `selector` | text (may be null) | optional |
| `name` | text or whole number | required; A key name (HOME, BACK, ENTER, ...) or an Android key code. |

### api/windows

#### GET /api/windows

Windows

#### POST /api/windows/close-all

Windows Close All

#### POST /api/windows/{action}

Windows Action

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `action` | path | `open` or `close` or `grid` | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `ids` | list of whole number (may be null) | optional |
| `selector` | text (may be null) | optional |

### api/camera

#### GET /api/camera

Camera

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `android` | query | true / false | optional; default `False` |

#### GET /api/camera/sources

Camera Sources

#### POST /api/camera/play

Camera Play

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `ids` | list of whole number (may be null) | optional |
| `selector` | text (may be null) | optional |
| `source` | text | required; length at least 1, length at most 200; A file name from the media library, or `rtmp` for the live ingest. |
| `sound` | text | optional; default `"source"`; length at most 200; source \| none \| a library file whose sound plays next to the picture. |

#### POST /api/camera/stop

Camera Stop

Body: see the route's description.

#### POST /api/camera/mic

Camera Mic

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `ids` | list of whole number (may be null) | optional |
| `selector` | text (may be null) | optional |
| `mode` | `mute` or `unmute` or `solo` | required |

#### POST /api/camera/attach

Camera Attach

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `ids` | list of whole number (may be null) | optional |
| `selector` | text (may be null) | optional |

#### POST /api/camera/install-hal

Camera Install Hal

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `ids` | list of whole number (may be null) | optional |
| `selector` | text (may be null) | optional |

### api/media

#### POST /api/media

Add a video or sound file to the media library (the camera's sources).

#### DELETE /api/media/{name}

Media Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

### api/listen

#### GET /api/listen

Listen Status

#### DELETE /api/listen

Listen Stop

#### POST /api/listen/{id}

Listen Start

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

### api/automation

#### GET /api/automation/datasets

Datasets

#### POST /api/automation/datasets

Dataset Create

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `name` | text | required; length at least 1, length at most 120 |
| `description` | text | optional; default `""`; length at most 4000 |
| `package` | text | optional; default `""`; length at most 200 |

#### GET /api/automation/datasets/{slug}

Dataset

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `slug` | path | text | required |

#### PATCH /api/automation/datasets/{slug}

Dataset Update

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `slug` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `name` | text (may be null) | optional |
| `description` | text (may be null) | optional |
| `package` | text (may be null) | optional |

#### DELETE /api/automation/datasets/{slug}

Dataset Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `slug` | path | text | required |

#### POST /api/automation/datasets/{slug}/train

Dataset Train

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `slug` | path | text | required |

#### GET /api/automation/datasets/{slug}/recordings/{rid}

Recording

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `slug` | path | text | required |
| `rid` | path | text | required |

#### DELETE /api/automation/datasets/{slug}/recordings/{rid}

Recording Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `slug` | path | text | required |
| `rid` | path | text | required |

#### GET /api/automation/datasets/{slug}/recordings/{rid}/shots/{relative}

Recording Shot

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `slug` | path | text | required |
| `rid` | path | text | required |
| `relative` | path | text | required |

#### GET /api/automation/record

Record Active

#### POST /api/automation/record/start

Record Start

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `dataset` | text | required; length at least 1, length at most 64 |
| `id` | whole number | required |

#### GET /api/automation/record/{id}

Record Status

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### POST /api/automation/record/{id}/tap

Record Tap

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `x` | number | required |
| `y` | number | required |
| `normalized` | true / false | optional; default `True` |

#### POST /api/automation/record/{id}/long-press

Record Long Press

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `x` | number | required |
| `y` | number | required |
| `normalized` | true / false | optional; default `True` |
| `ms` | whole number | optional; default `700`; at least 1, at most 30000 |

#### POST /api/automation/record/{id}/swipe

Record Swipe

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `x1` | number | required |
| `y1` | number | required |
| `x2` | number | required |
| `y2` | number | required |
| `ms` | whole number | optional; default `300`; at least 1, at most 30000 |
| `normalized` | true / false | optional; default `True` |

#### POST /api/automation/record/{id}/text

Record Text

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `value` | text (may be null) | optional; Text typed now and stored as written. |
| `name` | text (may be null) | optional; Record a placeholder `{name}` instead; the value comes from the run's values. |

#### POST /api/automation/record/{id}/key

Record Key

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `name` | text | required; length at least 1, length at most 40 |

#### POST /api/automation/record/{id}/launch

Record Launch

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `package` | text | required; length at least 1, length at most 200 |

#### POST /api/automation/record/{id}/wait

Record Wait

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `seconds` | number | required; more than 0, at most 600 |

#### POST /api/automation/record/{id}/assert-visible

Record Assert Visible

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `x` | number | required |
| `y` | number | required |
| `normalized` | true / false | optional; default `True` |

#### POST /api/automation/record/{id}/undo

Record Undo

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### POST /api/automation/record/{id}/stop

Record Stop

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### POST /api/automation/record/{id}/cancel

Record Cancel

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### GET /api/automation/programs

Programs

#### GET /api/automation/programs/{slug}

Program

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `slug` | path | text | required |

#### DELETE /api/automation/programs/{slug}

Program Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `slug` | path | text | required |

#### GET /api/automation/programs/{slug}/shots/{relative}

Program Shot

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `slug` | path | text | required |
| `relative` | path | text | required |

#### GET /api/automation/flows

Flows

#### POST /api/automation/flows/validate

400 with the reason when the flow is not valid; the summary when it is.

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `text` | text (may be null) | optional; YAML to check without saving it. |
| `name` | text (may be null) | optional; A saved flow to check in full: flows it calls, and roles it uses. |

#### GET /api/automation/flows/{name}

Flow Get

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

#### PUT /api/automation/flows/{name}

Flow Put

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `text` | text | required; length at least 1, length at most 524288 |

#### DELETE /api/automation/flows/{name}

Flow Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

#### GET /api/automation/profiles

Profiles

#### GET /api/automation/profiles/{package}

Profile Get

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `package` | path | text | required |

#### PUT /api/automation/profiles/{package}

Profile Put

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `package` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `roles` | object | optional |
| `overlays` | list of object | optional |
| `never_tap` | list of any | optional |
| `notes` | text | optional; default `""`; length at most 4000 |

#### DELETE /api/automation/profiles/{package}

Profile Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `package` | path | text | required |

#### PUT /api/automation/profiles/{package}/roles/{role}

Profile Role Put

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `package` | path | text | required |
| `role` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | object or text | required |

#### DELETE /api/automation/profiles/{package}/roles/{role}

Profile Role Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `package` | path | text | required |
| `role` | path | text | required |

#### POST /api/automation/profiles/{package}/overlays

Profile Overlay Add

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `package` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `rule` | object | required |

#### DELETE /api/automation/profiles/{package}/overlays/{name}

Profile Overlay Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `package` | path | text | required |
| `name` | path | text | required |

#### POST /api/automation/profiles/{package}/never-tap

Profile Never Tap Add

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `package` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | object or text | required |

#### DELETE /api/automation/profiles/{package}/never-tap/{index}

Profile Never Tap Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `package` | path | text | required |
| `index` | path | whole number | required |

#### POST /api/automation/profiles/{package}/teach

Profile Teach

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `package` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `id` | whole number | required |
| `role` | text | required; length at least 1, length at most 60 |
| `x` | whole number | required |
| `y` | whole number | required |
| `with_text` | true / false | optional; default `True` |

#### GET /api/automation/values

Values

#### PUT /api/automation/values/shared/{name}

Value Shared Put

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `value` | text | required; length at most 100000 |

#### DELETE /api/automation/values/shared/{name}

Value Shared Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

#### GET /api/automation/values/instances/{id}

Values Instance

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### PUT /api/automation/values/instances/{id}/{name}

Value Instance Put

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `name` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `value` | text | required; length at most 100000 |

#### DELETE /api/automation/values/instances/{id}/{name}

Value Instance Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `name` | path | text | required |

#### GET /api/automation/values/pools

Pools

#### GET /api/automation/values/pools/{name}

Pool

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |
| `limit` | query | whole number | optional; default `50` |

#### POST /api/automation/values/pools/{name}

Pool Add

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `lines` | list of text | required; at most items 20000 |

#### DELETE /api/automation/values/pools/{name}

Pool Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

#### GET /api/automation/runs

Runs

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `active` | query | true / false | optional; default `False` |

#### POST /api/automation/runs

Run Start

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `kind` | `program` or `flow` | required |
| `name` | text | required; length at least 1, length at most 80 |
| `selector` | text | required; length at least 1, length at most 500 |
| `variables` | map of text to text | optional; Values for this run only; kept in memory, never written to disk. |
| `dry_run` | true / false | optional; default `False` |

#### GET /api/automation/runs/{task_id}

Run Status

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `task_id` | path | text | required |

#### POST /api/automation/runs/{task_id}/cancel

Run Cancel

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `task_id` | path | text | required |

Body: see the route's description.

#### POST /api/automation/runs/{task_id}/resolve

Run Resolve

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `task_id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `id` | whole number | required |
| `action` | `resume` or `skip` or `abort` | optional; default `"resume"` |

#### GET /api/automation/schedules

Schedules

#### POST /api/automation/schedules

Schedule Create

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `name` | text | required; length at least 1, length at most 80 |
| `kind` | `program` or `flow` | required |
| `target` | text | required; length at least 1, length at most 80 |
| `selector` | text | required; length at least 1, length at most 500 |
| `every` | whole number | optional; default `0`; at least 0; Seconds between runs. Use this or `cron`. |
| `cron` | text | optional; default `""`; length at most 120; `minute hour day-of-month month day-of-week`. |
| `variables` | map of text to text | optional |
| `dry_run` | true / false | optional; default `False` |
| `enabled` | true / false | optional; default `True` |

#### PATCH /api/automation/schedules/{schedule_id}

Schedule Update

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `schedule_id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `name` | text (may be null) | optional |
| `kind` | `program` or `flow` (may be null) | optional |
| `target` | text (may be null) | optional |
| `selector` | text (may be null) | optional |
| `every` | whole number (may be null) | optional |
| `cron` | text (may be null) | optional |
| `variables` | map of text to text (may be null) | optional |
| `dry_run` | true / false (may be null) | optional |
| `enabled` | true / false (may be null) | optional |

#### DELETE /api/automation/schedules/{schedule_id}

Schedule Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `schedule_id` | path | text | required |

#### GET /api/automation/screen/{id}

Screen

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### GET /api/automation/screen/{id}/element

Screen Element

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `x` | query | whole number | required |
| `y` | query | whole number | required |

#### GET /api/automation/llm

Llm

### api/backups

#### GET /api/backups

Backups

#### POST /api/backups

Backup Create

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `selector` | text | required; length at least 1, length at most 500; Which Androids: all \| running \| stopped \| tag:x \| 5 \| 1-10 \| comma-separated |

#### DELETE /api/backups

The name comes in the body or as `?archive=` (not every client sends a body with DELETE).

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `archive` | query | text (may be null) | optional |

Body: see the route's description.

#### POST /api/backups/farm

Backup Farm

Body: see the route's description.

#### POST /api/backups/verify

Backup Verify

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `archive` | text | required; length at least 1, length at most 80; A backup name as listed: android-<id>/<time> or farm/<time>. |
| `deep` | true / false | optional; default `False`; Also read the data archive end to end (slow). |

#### POST /api/backups/prune

Backup Prune

#### POST /api/backups/restore

Backup Restore

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `archive` | text | required; length at least 1, length at most 80; A backup name as listed: android-<id>/<time> or farm/<time>. |
| `id` | whole number (may be null) | optional; Restore over this Android (it must be stopped; needs force). Leave out to restore as a new Android. |
| `force` | true / false | optional; default `False` |
| `keep_network` | true / false | optional; default `False`; Restoring as new next to the original: keep the copy's network on. Off by default so one identity is never online twice. |
| `trust` | true / false | optional; default `False`; Farm backup made by another farm: also take custom images, boot arguments, properties, adopted volumes, state and schedules from it. Off by default because those can start a privileged container of the archive's choosing. |
| `restore_config` | true / false | optional; default `False`; Farm backup: also replace this farm's config.yaml with the archived one. |
| `restore_secrets` | true / false | optional; default `False`; Farm backup: also restore VPN profiles, credentials and tokens from the archive. |

#### GET /api/backups/download

Backup Download

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `archive` | query | text | required |

### api/approvals

#### GET /api/approvals

Approvals

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `state` | query | text (may be null) | optional |
| `limit` | query | whole number | optional; default `100` |

#### POST /api/approvals

Called by an AI bridge before every changing or destructive tool call. `targets` in the answer are the Androids the selector resolved to here; the bridge acts on exactly those.

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `tool` | text | required; length at least 1, length at most 64 |
| `arguments` | object | optional; The tool's arguments, as the assistant sent them. |
| `caller` | text | required; length at least 1, length at most 80; Who asks, e.g. `mcp:<client name>`. |
| `session` | text | required; length at least 1, length at most 80; Opaque id of the assistant's session; `approve and remember` is tied to it. |

#### DELETE /api/approvals/sessions/{session}

Session End

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `session` | path | text | required |

#### GET /api/approvals/{id}

Approval

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### GET /api/approvals/{id}/wait

Returns as soon as the request is no longer pending, or after `timeout` seconds with its state as it is. Waiting here holds no worker thread, so many assistants can wait.

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `timeout` | query | number | optional; default `20.0` |

#### POST /api/approvals/{id}/approve

Approve

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body: see the route's description.

#### POST /api/approvals/{id}/deny

Deny

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body: see the route's description.

#### POST /api/approvals/{id}/cancel

Cancel

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

### api/mcp

#### GET /api/mcp/info

What AI assistants may do on this farm, and how to connect one.

### api/ai

#### GET /api/ai/models

Model List

#### POST /api/ai/models/{name}/test

Model Test

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `name` | path | text | required |

#### GET /api/ai/conversations

Conversations

#### POST /api/ai/conversations

Conversation Create

Body: see the route's description.

#### GET /api/ai/conversations/{id}

Conversation

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `after` | query | whole number | optional; default `0` |

#### DELETE /api/ai/conversations/{id}

Conversation Delete

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### POST /api/ai/conversations/{id}/messages

Conversation Message

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `text` | text | required; length at least 1, length at most 8000 |

#### POST /api/ai/conversations/{id}/cancel

Conversation Cancel

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### POST /api/ai/pilot

Pilot Start

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `goal` | text | required; length at least 3, length at most 600 |
| `package` | text | required; length at least 3, length at most 200 |
| `selector` | text | required; length at least 1, length at most 500 |
| `model` | text (may be null) | optional |
| `success` | text or object (may be null) | optional; text:..., id:..., desc:..., regex:..., package:..., activity:..., or a selector mapping |
| `max_steps` | whole number (may be null) | optional |
| `dataset` | text (may be null) | optional |
| `runs` | whole number | optional; default `1`; at least 1, at most 10 |
| `values` | map of text to text (may be null) | optional; Named values the Pilot may type (kept in memory for this session only) |
| `allowed_packages` | list of text (may be null) | optional |

#### GET /api/ai/pilot/sessions

Pilot Sessions

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `active` | query | true / false | optional; default `False` |

#### GET /api/ai/pilot/sessions/{id}

Pilot Session

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

#### POST /api/ai/pilot/sessions/{id}/cancel

Pilot Cancel

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body: see the route's description.

#### POST /api/ai/pilot/sessions/{id}/devices/{instance}/resolve

Pilot Resolve

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |
| `instance` | path | text | required |

Body: see the route's description.

#### POST /api/ai/pilot/sessions/{id}/confirm

Pilot Confirm

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `id` | path | text | required |

Body:

| Field | Type | Notes |
| --- | --- | --- |
| `instance` | whole number | required |
| `recording` | text (may be null) | optional |

#### POST /api/ai/pilot/{dataset}/train

Pilot Train

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `dataset` | path | text | required |

#### GET /api/ai/pilot/{dataset}/summary

Pilot Summary

| Parameter | In | Type | Notes |
| --- | --- | --- | --- |
| `dataset` | path | text | required |

### WebSocket routes

| Path | Notes |
| --- | --- |
| `/api/events` | See [WebSockets](#websockets). |
| `/api/instances/{id}/h264` | See [WebSockets](#websockets). |

## Next

- [Command reference](CLI.md) shows the `sf` command for most of these routes.
- [Security model](SECURITY_MODEL.md) explains what the API token allows.
- [MCP server](MCP.md) and [AI assistants](AI.md) use this API on behalf of an assistant.
- [Configuration reference](CONFIGURATION.md) lists the settings that `PATCH /api/config` changes.

