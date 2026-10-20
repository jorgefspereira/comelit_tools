# Comelit Tools

Standalone Python tools for a Comelit ViP entrance camera and door-release
controls. These clients established the protocol used by the companion
[Comelit Intercom Home Assistant integration](https://github.com/jorgefspereira/comelit_intercom).
They run independently of Home Assistant.

This is an **unofficial project**, not affiliated with or supported by Comelit.

## Tools and use cases

| Tool | Use cases | Requirements |
| --- | --- | --- |
| [`door_unlock/`](door_unlock/README.md) | Discover locks and relays; send a one-shot door-release command interactively or from an explicitly configured automation | Python standard library only |
| [`video_stream/`](video_stream/README.md) | Discover camera endpoints; record short MP4 clips; diagnose WebRTC connectivity | Python with `aiortc`, `av` and `aiohttp` in a separate virtual environment |

The video tool records media to a file. It does not provide an RTSP server or a
live VLC URL. Recorded MP4 files can be opened in VLC or processed with ffmpeg.

## Tested devices and results

| Device or environment | Validation |
| --- | --- |
| **Comelit PL6741 — People 5-inch handsfree Wi-Fi ViP indoor monitor** | Physical monitor used for the live protocol tests. It relays the building entrance-panel camera as a WebRTC peer. |
| Camera endpoint named **Entrada** | A 20-second capture decoded 320 video frames at 320×240. Media used a direct LAN host candidate. The entrance-panel model is unknown. |
| Lock endpoint **Fechadura entrada** and relay **Atuador geral** | Live account authentication and device discovery verified. Physical actuation has not been independently verified by this project. |
| **macOS client, aiortc 1.15.0, PyAV 17.1.0** | Runtime used for the original live video validation. |
| **Comelit Android app 7.5.0-7** | Protocol reference; an app traffic capture confirmed direct LAN WebRTC media. No APK is needed to use these tools. |

HD was accepted by the monitor but still produced 320×240 video. Audio was
negotiated but no audio frames arrived in the verified recording. Treat audio and
higher resolution as unverified.

Only this installation has been tested. Other models, firmware and legacy ViP
transports are not confirmed compatible. The endpoint names above are examples;
use `--list` to find the names on your account.

## Install

Use **Python 3.11 or newer** for the shared setup below. Commands are written for
macOS/Linux shells; Windows has not been validated.

```sh
git clone https://github.com/jorgefspereira/comelit_tools.git
cd comelit_tools
```

For door release only, no Python packages need to be installed.

For video, create a dedicated environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r video_stream/requirements.txt
```

The CLI requirements intentionally retain the original tested combination of
`aiortc==1.15.0` and `av==17.1.0`. Do not install these requirements into Home
Assistant's Python environment: the integration has its own dependency selection
compatible with Home Assistant's PyAV 19 constraints.

## Configure credentials

Create one credentials file at the repository root:

```sh
cp comelit.env.example comelit.env
chmod 600 comelit.env
```

Edit it with the account used in the official Comelit app:

```dotenv
COMELIT_EMAIL=you@example.com
COMELIT_PASSWORD=your-comelit-password
```

Use plain `KEY=value` entries without shell quotes or `export`. Alternatively,
provide these two names as environment variables; existing environment values
have priority. Both scripts look beside the script, then in the parent and
next parent directory, so the root file serves both tools.

The real `comelit.env` is ignored by Git. Do not commit or share it. Passwords
are sent only to Comelit's official login endpoint. Diagnostics and server error
responses can include account/device details; redact logs before sharing them.

## Discover devices

These commands authenticate and list endpoints without activating a door or
starting video:

```sh
python3 door_unlock/comelit_unlock.py --list
.venv/bin/python video_stream/comelit_stream.py --list
```

Video requires `RTCSessionController`; door and relay commands require
`PowerController` on the discovered endpoint.

## Record the entrance camera

From the repository root:

```sh
.venv/bin/python video_stream/comelit_stream.py \
  --name Entrada --duration 20 --out entrada.mp4
```

The file is written to **`video_stream/entrada.mp4`**: relative output paths are
resolved beside the script, not relative to your current shell directory. Use an
absolute path to choose a different location; the destination directory must exist.

Recording time starts with the first decoded video frame. The client verifies
the temporary recording before replacing an existing output file. Status reports
frame counts; MP4 file size may stay small until finalization.

See [video usage and diagnostic options](video_stream/README.md).

## Release a door or activate a relay

Choose the exact name returned by `--list`:

```sh
python3 door_unlock/comelit_unlock.py --name "Fechadura entrada"
```

The script asks for confirmation. To intentionally skip that prompt in an
automation:

```sh
python3 door_unlock/comelit_unlock.py --name "Fechadura entrada" --yes
```

This operates the real door/relay. It sends one command; the device determines
the momentary timing. An accepted command does not prove the lock physically
opened. There is no lock-position sensor and no automatic retry after failure.

See [door and relay usage](door_unlock/README.md).

## How the connection works

Both clients use Comelit's OAuth2 authorization-code flow with PKCE, then discover
endpoints through the official cloud API. Door activation is a cloud command.
Video signalling also uses the cloud, while WebRTC media can flow directly
between your computer and the indoor monitor on the LAN. ICE may select a relay;
the tools do not enforce local-only media.

The working video flow creates both Connexus data channels, advertises only the
real SHA-256 certificate fingerprint and sends `set-video-resolution` after
connection. Certificate verification remains enabled. A negotiated video track
alone is not evidence that frames are arriving.

## Tests

Run the video regressions from the repository root:

```sh
.venv/bin/python -m unittest discover -s video_stream -p 'test_*.py' -v
```

The tests do not use your credentials or open the physical door. They cover SDP
preservation, recording validation, cleanup and preserving an existing output
when an offer is rejected.

## Troubleshooting and limitations

- **Authentication fails:** check the account in the official app and the syntax
  of `comelit.env`. Network access to Comelit's API is required.
- **Endpoint not found:** use the name from `--list`, or pass a quoted endpoint ID.
- **No video:** close other active viewers and check LAN/UDP connectivity. Cloud
  discovery succeeding does not establish a usable media route.
- **Video negotiation succeeds but no frames arrive:** keep the default SCTP
  role (`auto`). A track event or connected transport does not guarantee video.
- **Small or missing recording:** wait for finalization; inspect the decoded frame
  counts. Failed runs do not replace existing output.
- **Only low resolution/no sound:** these match the known limitations of the tested
  installation. An accepted HD request does not guarantee HD output.

Doorbell events, talkback, legacy encrypted ViP transport, continuous recording
and an RTSP/HLS server are not implemented.

Report issues at [jorgefspereira/comelit_tools](https://github.com/jorgefspereira/comelit_tools/issues)
with your device model, Python/package versions and redacted output.
