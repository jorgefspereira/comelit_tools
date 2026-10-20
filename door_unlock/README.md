# Door and relay commands

A standalone, standard-library Python client for Comelit endpoints advertising
`PowerController`. Use it to list the controls available to your account and
send a single door-release or relay-activation command.

For the tested hardware and shared setup, see the [repository README](../README.md).
The physical reference system uses a **PL6741 People 5-inch ViP indoor monitor**.
Live authentication and discovery found **Fechadura entrada** and **Atuador geral**;
physical activation remains unverified by this project.

## Installation and account setup

No pip packages or virtual environment are required for this tool. Install Python
3.11 or newer, clone the repository, and run commands below from its root.

```sh
cp comelit.env.example comelit.env
chmod 600 comelit.env
```

Edit the root `comelit.env` with `COMELIT_EMAIL` and `COMELIT_PASSWORD`, using plain
values without quotes. The root file can be shared with the video tool. Environment
variables with the same names override file values.

## Usage

List the available door/relay endpoints without activating them:

```sh
python3 door_unlock/comelit_unlock.py --list
```

Release a named lock, with an interactive confirmation:

```sh
python3 door_unlock/comelit_unlock.py --name "Fechadura entrada"
```

The default name is `Fechadura entrada`; override it if your installation uses a
different name. Names are matched without case sensitivity.

To select an endpoint explicitly:

```sh
python3 door_unlock/comelit_unlock.py --endpoint '<endpointId>'
```

Quote the ID because it can contain shell-significant characters such as `#`.

For an automation that intentionally bypasses the prompt:

```sh
python3 door_unlock/comelit_unlock.py --name "Fechadura entrada" --yes
```

To activate the general relay instead:

```sh
python3 door_unlock/comelit_unlock.py --name "Atuador geral"
```

These commands perform physical actions. Use the endpoint identified for your
own system; a generic relay name does not reveal what is connected to it.

## Command semantics

The script signs in through OAuth2/PKCE, discovers endpoints and issues one
`PUT /power` with `{"value": true}`. The actuator handles release timing, so the
script does not automatically send `false` afterwards. Any HTTP 2xx response is
reported as acceptance; it does not confirm the mechanical state of the lock.

`--value false` is an explicit low-level power command. It is not a verified
"lock the door" operation. Normal door-release use should leave the default
`--value true` unchanged.

Cloud access is required even on the same LAN. The script does not use WebRTC,
provide a lock-state sensor or automatically retry a failed command.

Keep credentials and unredacted diagnostic output out of version control.
