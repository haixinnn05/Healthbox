"""Check everything the real box needs, then start the app on it. Run it through ./run_box.sh.

It checks, in order: the app is not already running, the vision models are here, the Mac can reach the
camera's network, the tray camera answers with fresh pictures, and the motor board answers.
Each problem is reported with what to do about it, and the app only starts when nothing is wrong.

Opening a board's USB port restarts that board, so the motor board may home itself during the check,
and the camera is looked for over Wi-Fi first so that its USB port is left alone.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_CAMERA_URL = "http://172.20.10.2/snapshot"
APP_PORT = 8000
HOTSPOT_PREFIX = "172.20.10."
TEMP_ADDRESS = ("172.20.10.9", "255.255.255.240", "172.20.10.1")
MODELS = ("face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx", "palm_detection_mediapipe_2023feb.onnx")
CAMERA_WAIT_S = 40.0  # the camera retries Wi-Fi in cycles of about 33 seconds
MOTOR_WAIT_S = 9.0    # boot plus homing


def ok(text: str) -> None:
    print(f"  OK    {text}", flush=True)


def problem(text: str, fix: str) -> None:
    print(f"  FIX   {text}\n        -> {fix}", flush=True)


# ------------------------------------------------------------------ network
def mac_address(interface: str = "en0") -> str | None:
    out = subprocess.run(["ifconfig", interface], capture_output=True, text=True).stdout
    found = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", out)
    return found.group(1) if found else None


def network_problem(address: str | None, camera_host: str) -> str | None:
    """None when the Mac can reach the camera's network; otherwise no_wifi, ipv6_only or other_network."""
    if not address:
        return "no_wifi"
    if address == "192.0.0.2":
        return "ipv6_only"  # an iPhone hotspot gave the Mac no real IPv4 address, so it cannot see the camera
    host = camera_host.split(":")[0]
    try:
        prefix = 28 if host.startswith(HOTSPOT_PREFIX) else 24
        same = ipaddress.ip_address(address) in ipaddress.ip_network(f"{host}/{prefix}", strict=False)
    except ValueError:
        return None  # a host name: only trying it can tell
    return None if same else "other_network"


def set_temporary_address() -> bool:
    return subprocess.run(["networksetup", "-setmanual", "Wi-Fi", *TEMP_ADDRESS]).returncode == 0


def restore_automatic_address() -> None:
    subprocess.run(["networksetup", "-setdhcp", "Wi-Fi"])
    print("  Wi-Fi address set back to automatic.", flush=True)


# ------------------------------------------------------------------ tray camera
def camera_status(host: str, timeout: float = 1.5) -> dict | None:
    import httpx
    try:
        reply = httpx.get(f"http://{host}/status", timeout=timeout)
        body = reply.json()
    except Exception:
        return None
    return body if reply.status_code == 200 and str(body.get("fw", "")).startswith("doseguard-tray") else None


def find_camera_on_hotspot(skip: str | None = None) -> tuple[str, dict] | None:
    """The hotspot may give the camera a different address after a restart."""
    hosts = [f"{HOTSPOT_PREFIX}{n}" for n in range(2, 15) if f"{HOTSPOT_PREFIX}{n}" != skip]
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        for host, status in zip(hosts, pool.map(lambda h: camera_status(h, 1.0), hosts)):
            if status:
                return host, status
    return None


def wait_for_camera(host: str, wait_s: float) -> tuple[str, dict] | None:
    end = time.monotonic() + wait_s
    while True:
        status = camera_status(host)
        if status:
            return host, status
        if host.startswith(HOTSPOT_PREFIX):
            elsewhere = find_camera_on_hotspot(skip=host)
            if elsewhere:
                return elsewhere
        if time.monotonic() >= end:
            return None
        time.sleep(1.0)


def fresh_pictures(url: str, attempts: int = 6) -> tuple[int, str | None]:
    """How many frames pass the app's own freshness rules, and the last problem seen."""
    from app.camera import Esp32SnapshotCamera
    from app.timeutil import Clock

    async def run() -> tuple[int, str | None]:
        cam = Esp32SnapshotCamera(url, Clock())
        good, error = 0, None
        for _ in range(attempts):
            if await cam.snapshot() is not None:
                good += 1
            else:
                error = cam.error
        return good, error

    return asyncio.run(run())


# ------------------------------------------------------------------ motor board
def serial_ports() -> list[str]:
    """USB serial ports, most likely motor board first: the camera's CH340 base has short numeric names."""
    ports = glob.glob("/dev/cu.usbserial-*") + glob.glob("/dev/cu.wchusbserial*") + glob.glob("/dev/cu.SLAB*")
    return sorted(ports, key=lambda p: (not p.endswith("-0001"), p))


def probe_port(path: str, wait_s: float = MOTOR_WAIT_S) -> dict | None:
    """Ask a port for the controller's heartbeat reply. Returns the last reply, once the motors are idle."""
    import serial
    try:
        port = serial.Serial(path, 115200, timeout=0.1, write_timeout=0.5, exclusive=True)
    except Exception:
        return None
    last, buffer, next_ping = None, b"", 0.0
    end = time.monotonic() + wait_s
    try:
        while time.monotonic() < end:
            now = time.monotonic()
            if now >= next_ping:
                next_ping = now + 0.25
                try:
                    port.write(b'{"v":3,"cmd":"PING"}\n')
                except Exception:
                    return last
            buffer += port.read(4096)
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("v") == 3 and msg.get("event") == "PONG":
                    last = msg
                    if msg.get("mA") == "idle" and msg.get("mB") == "idle":
                        return msg
    finally:
        port.close()
    return last


def find_motor_board(ports: list[str], wait_s: float = MOTOR_WAIT_S) -> tuple[str, dict] | None:
    for path in ports:
        reply = probe_port(path, wait_s)
        if reply:
            return path, reply
    return None


def motor_problems(reply: dict) -> list[tuple[str, str]]:
    out = []
    if reply.get("fault"):
        out.append((f"The motor board reports a fault: {reply['fault']}.", "Check the box, then unplug the motor board's USB and plug it back in."))
    if not reply.get("door"):
        out.append(("The door switch says the door is open.", "Close the door and check its switch."))
    if not (reply.get("hA") and reply.get("hB")):
        out.append(("A motor could not find its home position.", "Check the home switches and that nothing blocks the rotors."))
    return out


# ------------------------------------------------------------------ the run
def app_already_running(port: int = APP_PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ask_yes(question: str) -> bool:
    try:
        return input(f"        {question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Check the real box, then start the app on it.")
    ap.add_argument("--camera-url", default=os.environ.get("TRAY_CAMERA_URL") or DEFAULT_CAMERA_URL)
    ap.add_argument("--motor-port", help="the motor board's serial port, if it should not be searched for")
    ap.add_argument("--check", action="store_true", help="only check; do not start the app")
    a = ap.parse_args()

    print("\nChecking the box. Keep your hands clear: the motors may home themselves.\n", flush=True)
    failed = 0
    changed_address = False
    url = a.camera_url
    try:
        if app_already_running():
            failed += 1
            problem("The app is already running.", "Press Ctrl+C in the terminal running it, then start this again.")
        else:
            ok("The app isn't already running")

        missing = [m for m in MODELS if not (ROOT / "models" / m).exists()]
        if missing:
            failed += 1
            problem(f"Missing vision models: {', '.join(missing)}.", "Run .venv/bin/python scripts/download_models.py")
        else:
            ok("Vision models are here")

        host = urlsplit(url).netloc
        net = network_problem(mac_address(), host)
        if net == "ipv6_only" and host.startswith(HOTSPOT_PREFIX):
            print("  NOTE  The hotspot gave this Mac an address that can't reach the camera.", flush=True)
            if ask_yes("Use a temporary address until this script ends?") and set_temporary_address():
                changed_address = True
                time.sleep(3)
                net = network_problem(mac_address(), host)
        if net:
            failed += 1
            problem({"no_wifi": "The Mac isn't on Wi-Fi.",
                     "ipv6_only": "The Mac has an address that can't reach the camera.",
                     "other_network": "The Mac is on a different network from the camera."}[net],
                    "Connect the Mac to the same hotspot as the camera, then start this again.")
        else:
            ok("The Mac is on the camera's network")

            found = wait_for_camera(host, CAMERA_WAIT_S)
            if not found:
                failed += 1
                problem("The tray camera isn't answering on Wi-Fi.",
                        "Give the camera power. On the iPhone open Personal Hotspot, turn on Maximize Compatibility, "
                        "and switch Allow Others to Join off and on. If it still fails, reseat the camera ribbon.")
            else:
                if found[0] != host:
                    print(f"  NOTE  The camera moved to {found[0]}. Using that address.", flush=True)
                    url = f"http://{found[0]}/snapshot"
                good, error = fresh_pictures(url)
                if good == 0:
                    failed += 1
                    problem(f"The tray camera answers but sends no usable pictures ({error}).", "Reseat the camera ribbon and restart the camera.")
                elif good < 6:
                    ok(f"Tray camera at {found[0]}: {good} of 6 pictures on time. The connection is weak, so a check may need a second try")
                else:
                    ok(f"Tray camera at {found[0]}: 6 of 6 pictures on time")

        ports = [a.motor_port] if a.motor_port else serial_ports()
        motor = find_motor_board(ports) if ports else None
        if not motor:
            failed += 1
            problem("The motor board isn't answering over USB.",
                    "Plug it into the Mac with a data cable (a charge-only cable won't work), and close anything using its port.")
        else:
            issues = motor_problems(motor[1])
            for text, fix in issues:
                failed += 1
                problem(text, fix)
            if not issues:
                ok(f"Motor board on {motor[0]}: homed, door closed, no faults")

        if failed:
            print(f"\n{failed} thing(s) to fix. The app was not started.\n", flush=True)
            return 1
        print("\nEverything is ready.\n", flush=True)
        if a.check:
            return 0

        env = {**os.environ, "HARDWARE_MODE": "serial", "SERIAL_PORT": motor[0], "CAMERA_MODE": "dual_camera", "TRAY_CAMERA_URL": url}
        print(f"Starting Healthbox: http://127.0.0.1:{APP_PORT}  (press Ctrl+C to stop)\n", flush=True)
        app = subprocess.Popen(["caffeinate", "-dims", sys.executable, "-m", "app.main"], cwd=ROOT, env=env)
        try:
            return app.wait()
        except KeyboardInterrupt:
            return app.wait()
    finally:
        if changed_address:
            restore_automatic_address()


if __name__ == "__main__":
    sys.exit(main())
