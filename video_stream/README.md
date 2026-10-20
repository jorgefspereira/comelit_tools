# Entrance-camera recording

A standalone WebRTC client for Comelit endpoints advertising
`RTCSessionController`. Use it to record short entrance-camera clips and inspect
connection problems without Home Assistant.

## Tested hardware and output

Live validation used a **Comelit PL6741 People 5-inch handsfree Wi-Fi ViP indoor
monitor** and its **Entrada** endpoint. The monitor relayed the entrance-panel
camera; the panel model is unknown. A 20-second run decoded **320 frames at
320×240** using direct LAN media, with **aiortc 1.15.0 / PyAV 17.1.0** on macOS.

The monitor accepted the HD request but still supplied 320×240. Audio was
negotiated but no audio frames arrived in the verified capture. Other hardware,
firmware, higher resolutions and audio are not confirmed.

## Installation

From the repository root, with Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r video_stream/requirements.txt
cp comelit.env.example comelit.env
chmod 600 comelit.env
```

Edit the root `comelit.env` with your Comelit email and password. If it already
exists for the door tool, reuse it instead of replacing it. See the
[shared credential instructions](../README.md#configure-credentials).

Keep this virtual environment separate from Home Assistant, which uses different
media dependency constraints.

## Usage

Discover endpoints without starting a camera session:

```sh
.venv/bin/python video_stream/comelit_stream.py --list
```

Record the entrance camera:

```sh
.venv/bin/python video_stream/comelit_stream.py \
  --name Entrada --duration 20 --out entrada.mp4
```

That creates `video_stream/entrada.mp4`. Relative output paths are resolved beside
the script. To choose another directory, pass an absolute path to an existing
directory. The following uses your current working directory:

```sh
.venv/bin/python video_stream/comelit_stream.py \
  --name Entrada --duration 20 --out "$PWD/entrada.mp4"
```

Select by endpoint ID instead of name when necessary:

```sh
.venv/bin/python video_stream/comelit_stream.py \
  --endpoint '<endpointId>' --duration 20 --out entrada.mp4
```

Quote endpoint IDs. The client percent-encodes them when building API paths.

## Options

| Option | Purpose | Default |
| --- | --- | --- |
| `--list` | Authenticate and list devices, then exit | Off |
| `--name` | Select by friendly name | No target selected |
| `--endpoint` | Select by explicit ID; use instead of `--name` | No target selected |
| `--duration` | Recording seconds after the first decoded video frame | `30` |
| `--out` | Output filename; relative to this script | `comelit_feed.mp4` |
| `--resolution` | Request `HD` or `SD` | `HD` |
| `--startup-timeout` | Seconds to wait for decoded video after negotiation | `30` |
| `--debug` | Detailed transport diagnostics | Off |
| `--no-start` | Skip the video-start RPC for diagnosis | Off |
| `--dtls-setup` | Diagnostic DTLS role preference | `actpass` |
| `--sctp-role` | Diagnostic SCTP role override | `auto` |
| `--srtp-profile` | Default negotiation or classic SRTP for diagnosis | `auto` |
| `--call-id` | Optional signalling call identifier | None |

Keep the transport defaults for normal operation. In particular, forcing SCTP
server mode is not required and prevented video on the tested monitor.

## Recording behaviour

The timer starts when a video frame is decoded, rather than when a track is
negotiated. Progress reports decoded frame counts and connection states every
five seconds. MP4 buffering can keep the file small until finalization.

A completed capture is checked for decodable video before replacing the requested
output. Rejected offers, missing video or early disconnections fail and preserve
an existing file. Interrupting after video starts can save a valid shorter clip.

The result is a media file suitable for VLC or ffmpeg. This script does not
provide an RTSP/HLS server or a live network viewing URL.

## Protocol and network requirements

Authentication and SDP signalling use Comelit's cloud. Media uses standard
WebRTC and can remain peer-to-peer on the LAN; relay selection is still possible.
The computer must be able to exchange UDP traffic with the selected peer.

The connection creates `libcomelit-channel` and `iu-channel` before the offer,
advertises only the existing SHA-256 certificate fingerprint, and sends
`set-video-resolution` when the control channel opens. Certificate verification
remains enabled. The extra SHA-384/SHA-512 fingerprint advertisements caused the
tested monitor to fail before usable SCTP/video, so the compatibility filter is
intentional.

For a busy or stalled session, close other viewers, check network connectivity
and retry with default transport settings. Redact debug logs before sharing them.

## Offline tests

From the repository root:

```sh
.venv/bin/python -m unittest discover -s video_stream -p 'test_*.py' -v
```

These tests exercise SDP preservation, recording validation and cleanup without
using the account credentials or operating the door.
