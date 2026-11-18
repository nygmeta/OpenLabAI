# Driving a Hamilton without the Zadig driver swap

## The problem

PyLabRobot talks to a Hamilton STAR over USB using libusb. On Windows, libusb can
only open a device that is bound to a libusb-compatible driver, so you replace
Hamilton's own driver using a tool such as Zadig. After that, Venus on the same PC
can no longer see the robot until the driver is put back.

On macOS and Linux libusb opens the device directly, with no driver to replace.

## What cannot be fixed

A USB device has one owner at a time. Whatever talks to the Hamilton itself
(PyLabRobot, or Venus) takes it from the other. No transport can share it. The
only choices are which program owns it and how you hand it over.

## What this adds

`RemoteUSB` (`mcp_servers/remote_usb.py`) is a PyLabRobot transport. PyLabRobot's
Hamilton backends hold an IO object with `setup`, `stop`, `write` and `read`;
`RemoteUSB` implements those four calls over TCP. `usb_relay.py` is the other end:
it runs on a Mac or Linux machine that the robot is cabled to, opens the device
through PyLabRobot's own USB layer, and forwards the calls.

```
 your program (OpenLabAI, a script, an agent)         Mac / Linux box cabled to the robot
 PyLabRobot STARBackend -> RemoteUSB  --- TCP --->  usb_relay.py -> PyLabRobot USB -> Hamilton STAR
 (anywhere, including the Venus PC)                  (libusb, no driver swap)
```

The Windows PC that runs Venus keeps Hamilton's driver and is never touched. To use
Venus again, move the USB cable back to that PC (or use a USB switch).

If your agent can simply run on the Mac or Linux machine that the robot is cabled
to, you do not need the relay: run PyLabRobot there directly.

## Using it

On the machine the robot is cabled to:

```bash
pip install "pylabrobot[usb]"
python mcp_servers/usb_relay.py --generate-token ~/.openlabai_relay_token
python mcp_servers/usb_relay.py --token-file ~/.openlabai_relay_token
```

Copy the token file to the machine that runs your program, securely, then:

```python
from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.resources import STARLetDeck
from remote_usb import RemoteSTARBackend      # mcp_servers/ on your path

token = open("relay_token").read().strip()
lh = LiquidHandler(backend=RemoteSTARBackend("relay-host.local", 8765, token), deck=STARLetDeck())
await lh.setup()
```

The relay listens on localhost only. To reach it from another machine, start it with
`--bind <address> --allow-network`, or tunnel it with SSH (`ssh -L 8765:localhost:8765 relay-host`),
which also encrypts the traffic.

## Safeguards

* **Authentication.** The relay sends a random nonce; the client answers with
  HMAC-SHA256 of the token. The token never crosses the network. Failed attempts are
  delayed and logged.
* **One controller, one reader.** A second controller is refused while the first is
  connected.
* **One device.** The relay serves the vendor and product id on its command line
  (default Hamilton STAR, `0x08AF:0x8000`), nothing else.
* **Local by default.** Binding to a non-local address needs `--allow-network`.
* **Audit log.** Every connection, refusal and write (with the command text) goes to
  `relay_audit.jsonl`. It never contains the token.

## Limits you should know

* **Not run on a Hamilton.** The tests use a fake instrument behind a real relay on a
  real socket, and PyLabRobot's real `send_command` over it. PyUSB, libusb and a
  physical STAR have not been involved.
* **Whoever can authenticate can send raw firmware commands**, including ones that
  move the arm. The traffic is not encrypted. Keep the relay on a trusted network or
  behind an SSH tunnel.
* **If the controlling program disconnects mid-run, the relay cannot stop the
  robot.** The instrument finishes its current command and idles, as if the program had
  crashed.
* **Not wired into `hamilton_server.py`** yet. Its generated scripts still build a
  plain `STARBackend()`.

## The other route, not built

PyHamilton (https://github.com/dgretton/pyhamilton) avoids a driver swap by going
through Venus, which keeps the driver. It is Windows-only, needs a Venus licence and
Hamilton's libraries, and its documentation says initialization can hang if Venus
is also talking to the robot. It would also keep Hamilton's liquid classes, which
PyLabRobot does not have. It is not included here because it cannot be tested
without Venus and a robot.
