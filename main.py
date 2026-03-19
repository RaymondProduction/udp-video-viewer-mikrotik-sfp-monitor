#!/usr/bin/env python3
import argparse
import binascii
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, List, Tuple, Dict

import gi
import paramiko
import serial
from serial.tools import list_ports

gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Gst, GLib, Gdk, GdkPixbuf

Gst.init(None)

APP_VERSION = "0.1 beta"
APP_NAME = "Принц Вандам Галицький"
APP_ID = "knyaz-vandam-ground-station"


def get_app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)

        exe_path = Path(sys.executable).resolve()
        return exe_path.parent

    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    return get_app_base_dir().joinpath(*parts)


def first_existing_path(candidates: List[Path]) -> Optional[Path]:
    for path in candidates:
        if path.exists():
            return path
    return None


def get_user_config_dir() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / APP_ID
    return Path.home() / ".config" / APP_ID


def get_user_data_dir() -> Path:
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / APP_ID
    return Path.home() / ".local" / "share" / APP_ID


def get_desktop_dir() -> Path:
    try:
        output = subprocess.check_output(["xdg-user-dir", "DESKTOP"], text=True).strip()
        if output:
            return Path(output)
    except Exception:
        pass
    return Path.home() / "Desktop"


SETTINGS_DIR = get_user_config_dir()
SETTINGS_FILE = SETTINGS_DIR / "ground_station_settings.json"

PLACEHOLDER_IMAGE_FILE = first_existing_path(
    [
        resource_path("vandam.png"),
        resource_path("vandam.jpg"),
        resource_path("vandam.jpeg"),
        resource_path("80dshv.png"),
        Path(__file__).resolve().parent / "vandam.png",
        Path(__file__).resolve().parent / "vandam.jpg",
        Path(__file__).resolve().parent / "vandam.jpeg",
        Path(__file__).resolve().parent / "80dshv.png",
    ]
)


def get_local_ipv4_networks() -> List[ipaddress.IPv4Network]:
    result = []
    try:
        output = subprocess.check_output(
            ["ip", "-j", "-4", "addr", "show", "up"],
            text=True,
        )
        data = json.loads(output)

        for iface in data:
            for addr_info in iface.get("addr_info", []):
                local = addr_info.get("local")
                prefixlen = addr_info.get("prefixlen")
                if not local or prefixlen is None:
                    continue

                ip_obj = ipaddress.ip_address(local)
                if ip_obj.is_loopback:
                    continue

                network = ipaddress.ip_network(f"{local}/{prefixlen}", strict=False)
                if network.prefixlen < 24:
                    network = ipaddress.ip_network(f"{local}/24", strict=False)

                result.append(network)

    except Exception as e:
        print(f"Не вдалося отримати локальні інтерфейси: {e}", file=sys.stderr)

    unique = []
    seen = set()
    for net in result:
        net_str = str(net)
        if net_str not in seen:
            unique.append(net)
            seen.add(net_str)

    return unique


def get_local_ipv4_interfaces() -> List[Dict[str, str]]:
    result = []
    try:
        output = subprocess.check_output(
            ["ip", "-j", "-4", "addr", "show", "up"],
            text=True,
        )
        data = json.loads(output)

        for iface in data:
            iface_name = iface.get("ifname", "")
            for addr_info in iface.get("addr_info", []):
                local = addr_info.get("local")
                prefixlen = addr_info.get("prefixlen")
                if not local or prefixlen is None:
                    continue

                ip_obj = ipaddress.ip_address(local)
                if ip_obj.is_loopback:
                    continue

                network = ipaddress.ip_network(f"{local}/{prefixlen}", strict=False)
                if network.prefixlen < 24:
                    network = ipaddress.ip_network(f"{local}/24", strict=False)

                result.append(
                    {
                        "ifname": iface_name,
                        "ip": local,
                        "network": str(network),
                    }
                )
    except Exception as e:
        print(f"Не вдалося отримати список інтерфейсів: {e}", file=sys.stderr)

    return result


def tcp_connectable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def parse_dbm_value(value: Optional[str]) -> Optional[float]:
    if not value:
        return None

    cleaned = value.strip().lower().replace("dbm", "").strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None

    try:
        return float(match.group(0))
    except Exception:
        return None


def normalize_wavelength_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    cleaned = value.strip()
    if not cleaned:
        return None

    if "nm" in cleaned.lower():
        return cleaned

    match = re.search(r"(\d{3,4})", cleaned)
    if match:
        return f"{match.group(1)}nm"

    return cleaned


def normalize_distance_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    cleaned = value.strip()
    if not cleaned:
        return None

    lower = cleaned.lower()

    if "km" in lower:
        return cleaned
    if "m" in lower and "km" not in lower:
        return cleaned

    match_km = re.search(r"(\d+(?:\.\d+)?)", cleaned)
    if match_km:
        return f"{match_km.group(1)}km"

    return cleaned


def infer_wavelength_from_text(text: str) -> Optional[str]:
    if not text:
        return None

    match = re.search(r"\b(850|1310|1490|1550|1577|1270)\s*nm\b", text, re.IGNORECASE)
    if match:
        return f"{match.group(1)}nm"

    match = re.search(r"\b(850|1310|1490|1550|1577|1270)\b", text)
    if match:
        return f"{match.group(1)}nm"

    return None


def infer_distance_from_text(text: str) -> Optional[str]:
    if not text:
        return None

    match = re.search(r"\b(\d+(?:\.\d+)?)\s*km\b", text, re.IGNORECASE)
    if match:
        return f"{match.group(1)}km"

    match = re.search(r"(?<!\d)(1|2|3|5|10|20|40|60|80|100|120)\s*km(?!\w)", text, re.IGNORECASE)
    if match:
        return f"{match.group(1)}km"

    match = re.search(r"(?:^|[-_/ ])(1|2|3|5|10|20|40|60|80|100|120)(?:[-_/ ]|$)", text)
    if match:
        return f"{match.group(1)}km"

    return None


def list_serial_devices() -> List[Tuple[str, str]]:
    items = []
    for p in list_ports.comports():
        parts = [p.device]
        if p.description:
            parts.append(p.description)
        if p.manufacturer:
            parts.append(p.manufacturer)
        text = " | ".join(parts)
        items.append((p.device, text))
    items.sort(key=lambda x: x[0])
    return items


def find_controller_serial_device() -> Optional[str]:
    for p in list_ports.comports():
        if p.manufacturer == "Raspberry Pi" and p.product == "Pico":
            return p.device
    return None


class MikroTikSshClient:
    def __init__(self, host: str, username: str, password: str, port: int = 22):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.client: Optional[paramiko.SSHClient] = None

    def connect(self):
        self.disconnect()
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
        )

    def disconnect(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None

    def ensure_connected(self):
        if self.client is None:
            self.connect()

    def run_command(self, command: str) -> str:
        self.ensure_connected()
        stdin, stdout, stderr = self.client.exec_command(command, timeout=10)
        out = stdout.read().decode("utf-8", errors="ignore")
        err = stderr.read().decode("utf-8", errors="ignore")

        if err.strip():
            raise RuntimeError(err.strip())

        return out

    def get_identity(self) -> str:
        out = self.run_command("/system identity print")
        for line in out.splitlines():
            line = line.strip()
            if "name:" in line:
                return line.split("name:", 1)[1].strip()
        return "RouterOS"

    def auto_discover_sfp_interface(self) -> Optional[str]:
        out = self.run_command("/interface ethernet print")
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue

            if "sfp" in line.lower() or "qsfp" in line.lower():
                parts = line.split()
                if parts:
                    return parts[-1]

        return None

    def fetch_sfp_status(
        self,
        interface_name: str,
    ) -> Tuple[
        Optional[str],
        Optional[str],
        Optional[str],
        Optional[str],
        Optional[str],
        Optional[str],
    ]:
        cmd = f'/interface ethernet monitor "{interface_name}" once'
        out = self.run_command(cmd)

        rx_power = None
        tx_power = None
        temperature = None
        voltage = None
        wavelength = None
        distance = None

        vendor_name = None
        vendor_part = None
        model = None

        for line in out.splitlines():
            line = line.strip()
            lower = line.lower()

            if "sfp-rx-power:" in lower:
                rx_power = line.split(":", 1)[1].strip()
            elif "sfp-tx-power:" in lower:
                tx_power = line.split(":", 1)[1].strip()
            elif "sfp-temperature:" in lower:
                temperature = line.split(":", 1)[1].strip()
            elif "sfp-supply-voltage:" in lower:
                voltage = line.split(":", 1)[1].strip()
            elif "sfp-voltage:" in lower:
                voltage = line.split(":", 1)[1].strip()
            elif "sfp-wavelength:" in lower:
                wavelength = line.split(":", 1)[1].strip()
            elif "sfp-link-length-sm:" in lower:
                distance = line.split(":", 1)[1].strip()
            elif "sfp-link-length:" in lower:
                distance = line.split(":", 1)[1].strip()
            elif "sfp-length:" in lower:
                distance = line.split(":", 1)[1].strip()
            elif "sfp-vendor-name:" in lower:
                vendor_name = line.split(":", 1)[1].strip()
            elif "sfp-vendor-part-number:" in lower:
                vendor_part = line.split(":", 1)[1].strip()
            elif "sfp-part-number:" in lower:
                vendor_part = line.split(":", 1)[1].strip()
            elif "sfp-model:" in lower:
                model = line.split(":", 1)[1].strip()

        extra_text = " ".join(x for x in [vendor_name, vendor_part, model] if x)

        wavelength = normalize_wavelength_text(wavelength) or infer_wavelength_from_text(extra_text)
        distance = normalize_distance_text(distance) or infer_distance_from_text(extra_text)

        return rx_power, tx_power, temperature, voltage, wavelength, distance


def try_mikrotik_ssh(host: str, username: str, password: str, port: int) -> bool:
    client = None
    try:
        client = MikroTikSshClient(host=host, username=username, password=password, port=port)
        client.connect()
        identity = client.get_identity()
        return bool(identity)
    except Exception:
        return False
    finally:
        if client is not None:
            client.disconnect()


def auto_discover_mikrotik(username: str, password: str, port: int) -> Optional[str]:
    networks = get_local_ipv4_networks()
    print("Локальні мережі для сканування:", [str(n) for n in networks])

    for network in networks:
        print(f"Сканую мережу {network} ...")
        hosts = list(network.hosts())

        if len(hosts) > 254:
            hosts = hosts[:254]

        for host in hosts:
            ip = str(host)

            if not tcp_connectable(ip, port, timeout=0.2):
                continue

            if try_mikrotik_ssh(
                host=ip,
                username=username,
                password=password,
                port=port,
            ):
                print(f"Знайдено MikroTik через SSH: {ip}:{port}")
                return ip

    return None


class UdpSerialBridge:
    def __init__(
        self,
        remote_host: str,
        remote_port: int,
        serial_dev: str,
        baudrate: int,
        local_bind_ip: str = "0.0.0.0",
        local_bind_port: int = 0,
        serial_timeout: float = 0.01,
        udp_timeout: float = 0.2,
        verbose: bool = False,
        hex_dump: bool = False,
    ):
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.serial_dev = serial_dev
        self.baudrate = baudrate
        self.local_bind_ip = local_bind_ip
        self.local_bind_port = local_bind_port
        self.serial_timeout = serial_timeout
        self.udp_timeout = udp_timeout
        self.verbose = verbose
        self.hex_dump = hex_dump

        self.running = False
        self.failed = False
        self.fail_reason = ""

        self.sock: Optional[socket.socket] = None
        self.ser: Optional[serial.Serial] = None

        self.bytes_udp_to_serial = 0
        self.bytes_serial_to_udp = 0
        self.packets_udp_to_serial = 0
        self.packets_serial_to_udp = 0

        self.actual_local_addr = "N/A"

        self.t_udp_to_serial: Optional[threading.Thread] = None
        self.t_serial_to_udp: Optional[threading.Thread] = None

        now = time.time()
        self.started_at = now
        self.last_udp_to_serial_time = now
        self.last_serial_to_udp_time = now

    def log(self, text: str):
        if self.verbose:
            print(f"[BRIDGE] {text}", flush=True)

    def info(self, text: str):
        print(f"[INFO] {text}", flush=True)

    def err(self, text: str):
        print(f"[ERROR] {text}", file=sys.stderr, flush=True)

    @staticmethod
    def short_hex(data: bytes, max_len: int = 64) -> str:
        if not data:
            return ""
        part = data[:max_len]
        txt = binascii.hexlify(part).decode("ascii")
        if len(data) > max_len:
            txt += "..."
        return txt

    def mark_failed(self, reason: str):
        self.failed = True
        self.fail_reason = reason
        self.running = False
        self.err(f"Bridge marked as failed: {reason}")

    def start(self):
        self.stop()

        self.failed = False
        self.fail_reason = ""

        self.info(f"Opening serial: {self.serial_dev} @ {self.baudrate} (8N1, no flow control)")
        self.ser = serial.Serial(
            port=self.serial_dev,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.serial_timeout,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )

        self.info(
            f"Opening UDP: local {self.local_bind_ip}:{self.local_bind_port} -> "
            f"remote {self.remote_host}:{self.remote_port}"
        )
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.local_bind_ip, self.local_bind_port))
        self.sock.connect((self.remote_host, self.remote_port))
        self.sock.settimeout(self.udp_timeout)

        local_ip, local_port = self.sock.getsockname()
        self.actual_local_addr = f"{local_ip}:{local_port}"

        now = time.time()
        self.started_at = now
        self.last_udp_to_serial_time = now
        self.last_serial_to_udp_time = now

        self.running = True

        self.t_udp_to_serial = threading.Thread(target=self.udp_to_serial_loop, daemon=True, name="udp_to_serial")
        self.t_serial_to_udp = threading.Thread(target=self.serial_to_udp_loop, daemon=True, name="serial_to_udp")

        self.t_udp_to_serial.start()
        self.t_serial_to_udp.start()

        self.info(
            "Bridge started: "
            f"local {self.actual_local_addr} <-> remote {self.remote_host}:{self.remote_port} "
            f"<-> serial {self.serial_dev} @ {self.baudrate}"
        )

    def stop(self):
        self.running = False

        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def is_alive(self) -> bool:
        try:
            serial_ok = self.ser is not None and self.ser.is_open
        except Exception:
            serial_ok = False

        sock_ok = self.sock is not None
        udp_thread_ok = self.t_udp_to_serial is not None and self.t_udp_to_serial.is_alive()
        serial_thread_ok = self.t_serial_to_udp is not None and self.t_serial_to_udp.is_alive()

        return (
            self.running
            and not self.failed
            and serial_ok
            and sock_ok
            and udp_thread_ok
            and serial_thread_ok
        )

    def is_stalled(self, timeout_sec: float = 3.0) -> bool:
        if not self.running or self.failed:
            return True

        now = time.time()
        if now - self.last_serial_to_udp_time > timeout_sec * 5:
            return True

        return False

    def udp_to_serial_loop(self):
        while self.running:
            try:
                if self.sock is None or self.ser is None:
                    self.mark_failed("udp_to_serial: socket or serial is None")
                    break

                data = self.sock.recv(4096)
                if not data:
                    continue

                self.ser.write(data)
                self.packets_udp_to_serial += 1
                self.bytes_udp_to_serial += len(data)
                self.last_udp_to_serial_time = time.time()

                if self.hex_dump:
                    self.log(f"UDP -> SERIAL | {len(data)} bytes | hex={self.short_hex(data)}")
                else:
                    self.log(f"UDP -> SERIAL | {len(data)} bytes")

            except socket.timeout:
                continue
            except OSError as e:
                self.mark_failed(f"udp_to_serial OSError: {e}")
                break
            except Exception as e:
                self.mark_failed(f"udp_to_serial Exception: {e}")
                break

    def serial_to_udp_loop(self):
        while self.running:
            try:
                if self.sock is None or self.ser is None:
                    self.mark_failed("serial_to_udp: socket or serial is None")
                    break

                data = self.ser.read(4096)
                if not data:
                    continue

                self.sock.send(data)
                self.packets_serial_to_udp += 1
                self.bytes_serial_to_udp += len(data)
                self.last_serial_to_udp_time = time.time()

                if self.hex_dump:
                    self.log(f"SERIAL -> UDP | {len(data)} bytes | hex={self.short_hex(data)}")
                else:
                    self.log(f"SERIAL -> UDP | {len(data)} bytes")

            except OSError as e:
                self.mark_failed(f"serial_to_udp OSError: {e}")
                break
            except Exception as e:
                self.mark_failed(f"serial_to_udp Exception: {e}")
                break

    def stats_text(self) -> str:
        status = "OK"
        if self.failed:
            status = f"FAILED: {self.fail_reason}"

        return (
            f"local={self.actual_local_addr} remote={self.remote_host}:{self.remote_port} "
            f"| U->S: {self.packets_udp_to_serial} pkt / {self.bytes_udp_to_serial} B "
            f"| S->U: {self.packets_serial_to_udp} pkt / {self.bytes_serial_to_udp} B "
            f"| bridge: {status}"
        )


class VideoEventBox(Gtk.EventBox):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.set_visible_window(False)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.connect("button-press-event", self.on_button_press)

    def on_button_press(self, widget, event):
        if event.type == Gdk.EventType._2BUTTON_PRESS and event.button == 1:
            GLib.idle_add(self.owner.toggle_fullscreen_video)
            return True
        return False


class UdpVideoWindow:
    def __init__(
        self,
        port: int,
        mode: str,
        always_on_top: bool,
        mikrotik_host: Optional[str],
        mikrotik_user: str,
        mikrotik_password: str,
        mikrotik_interface: Optional[str],
        poll_interval: float,
        ssh_port: int,
        serial_dev: Optional[str],
        serial_baudrate: int,
        bridge_remote_host: str,
        bridge_remote_port: int,
        bridge_local_bind_ip: str,
        bridge_local_bind_port: int,
        bridge_verbose: bool,
        bridge_hex: bool,
    ):
        self.port = port
        self.mode = mode
        self.mikrotik_host = mikrotik_host
        self.mikrotik_user = mikrotik_user
        self.mikrotik_password = mikrotik_password
        self.mikrotik_interface = mikrotik_interface
        self.poll_interval = max(0.5, poll_interval)
        self.ssh_port = ssh_port

        self.serial_dev = serial_dev
        self.serial_baudrate = serial_baudrate
        self.bridge_remote_host = bridge_remote_host
        self.bridge_remote_port = bridge_remote_port
        self.bridge_local_bind_ip = bridge_local_bind_ip
        self.bridge_local_bind_port = bridge_local_bind_port
        self.bridge_verbose = bridge_verbose
        self.bridge_hex = bridge_hex

        self.running = True
        self.identity_name = ""
        self.auto_controller_enabled = not bool(self.serial_dev)
        self.is_video_fullscreen = False

        self.default_root_border = 8
        self.default_root_spacing = 6
        self.network_warning_shown = False

        self.mt_client: Optional[MikroTikSshClient] = None
        self.mt_lock = threading.Lock()
        self.mikrotik_reconnect_requested = False

        self.last_video_frame_time = 0.0
        self.video_signal_timeout_sec = 1.5
        self.placeholder_visible = True
        self.monitor_sink = None

        self.load_settings()

        self.window = Gtk.Window(title=APP_NAME)
        self.window.set_name("video-window")
        self.window.set_default_size(1100, 700)
        self.window.set_keep_above(self.always_on_top)
        self.window.connect("destroy", self.on_destroy)
        self.window.connect("key-press-event", self.on_key_press)

        self.apply_css()
        self.apply_window_icon()

        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self.default_root_spacing)
        self.root.set_name("video-root")
        self.root.set_border_width(self.default_root_border)
        self.window.add(self.root)

        self.top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.root.pack_start(self.top_bar, False, False, 0)
        self.top_bar.pack_start(Gtk.Label(label=""), True, True, 0)

        self.btn_fullscreen = Gtk.Button()
        self.btn_fullscreen.set_image(Gtk.Image.new_from_icon_name("view-fullscreen-symbolic", Gtk.IconSize.BUTTON))
        self.btn_fullscreen.set_tooltip_text("На весь екран")
        self.btn_fullscreen.connect("clicked", self.on_fullscreen_button_clicked)
        self.top_bar.pack_start(self.btn_fullscreen, False, False, 0)

        btn_settings = Gtk.Button()
        btn_settings.set_image(Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON))
        btn_settings.set_tooltip_text("Налаштування")
        btn_settings.connect("clicked", self.open_ground_station_settings)
        self.top_bar.pack_start(btn_settings, False, False, 0)

        self.frame_video = Gtk.Frame()
        self.frame_video.set_name("video-frame")
        self.frame_video.set_shadow_type(Gtk.ShadowType.IN)
        self.root.pack_start(self.frame_video, True, True, 0)

        self.video_overlay = Gtk.Overlay()
        self.frame_video.add(self.video_overlay)

        self.video_event_box = VideoEventBox(self)
        self.video_overlay.add(self.video_event_box)

        self.video_box = Gtk.Box()
        self.video_event_box.add(self.video_box)

        self.placeholder_background = Gtk.Overlay()
        self.placeholder_background.set_name("no-signal-bg")
        self.placeholder_background.set_halign(Gtk.Align.FILL)
        self.placeholder_background.set_valign(Gtk.Align.FILL)
        self.placeholder_background.set_hexpand(True)
        self.placeholder_background.set_vexpand(True)
        self.placeholder_background.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.placeholder_background.connect("button-press-event", self.on_placeholder_button_press)

        self.placeholder_box = Gtk.Box()
        self.placeholder_box.set_halign(Gtk.Align.FILL)
        self.placeholder_box.set_valign(Gtk.Align.FILL)
        self.placeholder_box.set_hexpand(True)
        self.placeholder_box.set_vexpand(True)

        self.placeholder_inner = Gtk.Box()
        self.placeholder_inner.set_halign(Gtk.Align.CENTER)
        self.placeholder_inner.set_valign(Gtk.Align.CENTER)
        self.placeholder_inner.set_hexpand(True)
        self.placeholder_inner.set_vexpand(True)

        self.placeholder_box.pack_start(self.placeholder_inner, True, True, 0)
        self.placeholder_background.add(self.placeholder_box)

        self.placeholder_image = None
        self.placeholder_label = Gtk.Label(label="Немає сигналу з дроном")
        self.placeholder_label.set_name("no-signal-label")
        self.placeholder_label.set_halign(Gtk.Align.CENTER)
        self.placeholder_label.set_valign(Gtk.Align.END)
        self.placeholder_label.set_margin_bottom(32)
        self.placeholder_label.set_justify(Gtk.Justification.CENTER)
        self.placeholder_label.set_line_wrap(True)

        self.placeholder_original_pixbuf = None

        if PLACEHOLDER_IMAGE_FILE and PLACEHOLDER_IMAGE_FILE.exists():
            try:
                self.placeholder_original_pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(PLACEHOLDER_IMAGE_FILE))
                self.placeholder_image = Gtk.Image()
                self.placeholder_image.set_halign(Gtk.Align.CENTER)
                self.placeholder_image.set_valign(Gtk.Align.CENTER)
                self.placeholder_inner.pack_start(self.placeholder_image, True, True, 0)
            except Exception as e:
                print(f"[WARN] Не вдалося завантажити placeholder image: {e}", file=sys.stderr)

        if self.placeholder_image is None:
            fallback_label = Gtk.Label(label="")
            fallback_label.set_halign(Gtk.Align.CENTER)
            fallback_label.set_valign(Gtk.Align.CENTER)
            self.placeholder_inner.pack_start(fallback_label, True, True, 0)

        self.placeholder_background.add_overlay(self.placeholder_label)

        self.video_overlay.add_overlay(self.placeholder_background)
        self.placeholder_background.show_all()
        self.video_overlay.connect("size-allocate", self.on_video_overlay_size_allocate)

        self.pipeline = None
        self.overlay = None
        self.video_sink = None
        self.bus = None

        self.build_and_start_pipeline(self.get_overlay_text_for_pipeline_start())
        self.apply_overlay_visual_settings()
        self.set_placeholder_visible(True)
        self.last_video_frame_time = 0.0

        self.bridge: Optional[UdpSerialBridge] = None
        if self.bridge_remote_host:
            self.ensure_bridge_running()

        self.window.show_all()
        alloc = self.video_overlay.get_allocation()
        self.update_placeholder_image_size(alloc.width, alloc.height)

        GLib.idle_add(self.warn_udp_network_mismatch_if_needed)

        self.poll_thread = threading.Thread(target=self.poll_mikrotik_loop, daemon=True)
        self.poll_thread.start()

        self.bridge_info_thread = threading.Thread(target=self.bridge_info_loop, daemon=True)
        self.bridge_info_thread.start()

        self.controller_watch_thread = threading.Thread(target=self.controller_watch_loop, daemon=True)
        self.controller_watch_thread.start()

        self.video_signal_thread = threading.Thread(target=self.video_signal_loop, daemon=True)
        self.video_signal_thread.start()

    def apply_css(self):
        css = b"""
        #video-window, #video-root, #video-frame, #no-signal-bg {
            background-color: #000000;
        }

        #no-signal-label {
            color: white;
            font-size: 28px;
            font-weight: bold;
            background-color: rgba(0, 0, 0, 0.45);
            padding: 12px;
            border-radius: 12px;
        }
        """
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def apply_window_icon(self):
        icon_source = self.find_icon_source()
        if icon_source is None:
            return
        try:
            self.window.set_icon_from_file(str(icon_source))
        except Exception as e:
            print(f"[WARN] Не вдалося встановити іконку вікна: {e}", file=sys.stderr)

    def set_default_settings(self):
        self.enable_telemetry_osd = True

        self.overlay_xpad = 0
        self.overlay_ypad = 0
        self.overlay_font_size = 8
        self.overlay_background = False
        self.overlay_halign = "right"
        self.overlay_valign = "bottom"

        self.show_loss = True
        self.show_distance = True
        self.show_wavelength = True

        self.port = 5600
        self.mode = "rtp"
        self.always_on_top = True

        self.mikrotik_host = "192.168.121.1"
        self.mikrotik_user = "admin"
        self.mikrotik_password = ""
        self.mikrotik_interface = "sfp1"

        self.serial_dev = None
        self.serial_baudrate = 420000
        self.bridge_remote_host = "192.168.121.50"
        self.bridge_remote_port = 9000
        self.bridge_local_bind_ip = "0.0.0.0"
        self.bridge_local_bind_port = 0
        self.bridge_verbose = False
        self.bridge_hex = True

    def load_settings(self):
        self.set_default_settings()

        try:
            SETTINGS_DIR.mkdir(parents=True, exist_ok=True)

            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            osd = data.get("osd", {})
            self.enable_telemetry_osd = bool(osd.get("enabled", self.enable_telemetry_osd))
            self.overlay_xpad = int(osd.get("xpad", self.overlay_xpad))
            self.overlay_ypad = int(osd.get("ypad", self.overlay_ypad))
            self.overlay_font_size = int(osd.get("font_size", self.overlay_font_size))
            self.overlay_background = bool(osd.get("background", self.overlay_background))

            halign = str(osd.get("halign", self.overlay_halign)).lower()
            if halign in ("left", "right"):
                self.overlay_halign = halign

            valign = str(osd.get("valign", self.overlay_valign)).lower()
            if valign in ("top", "bottom"):
                self.overlay_valign = valign

            self.show_loss = bool(osd.get("show_loss", self.show_loss))
            self.show_distance = bool(osd.get("show_distance", self.show_distance))
            self.show_wavelength = bool(osd.get("show_wavelength", self.show_wavelength))

            bridge = data.get("bridge", {})
            self.serial_dev = bridge.get("serial_dev") or None
            self.serial_baudrate = int(bridge.get("serial_baudrate", self.serial_baudrate))
            self.bridge_remote_host = str(bridge.get("remote_host", self.bridge_remote_host))
            self.bridge_remote_port = int(bridge.get("remote_port", self.bridge_remote_port))
            self.bridge_local_bind_ip = str(bridge.get("local_bind_ip", self.bridge_local_bind_ip))
            self.bridge_local_bind_port = int(bridge.get("local_bind_port", self.bridge_local_bind_port))
            self.bridge_verbose = bool(bridge.get("verbose", self.bridge_verbose))
            self.bridge_hex = bool(bridge.get("hex", self.bridge_hex))

            video = data.get("video", {})
            self.port = int(video.get("port", self.port))
            mode = str(video.get("mode", self.mode)).lower()
            if mode in ("raw", "rtp"):
                self.mode = mode
            self.always_on_top = bool(video.get("always_on_top", self.always_on_top))

            mikrotik = data.get("mikrotik", {})
            self.mikrotik_host = str(mikrotik.get("host", self.mikrotik_host))
            self.mikrotik_user = str(mikrotik.get("user", self.mikrotik_user))
            self.mikrotik_password = str(mikrotik.get("password", self.mikrotik_password))
            self.mikrotik_interface = str(mikrotik.get("interface", self.mikrotik_interface))

            print(f"[INFO] Налаштування завантажено з {SETTINGS_FILE}", flush=True)

        except FileNotFoundError:
            print("[INFO] Файл налаштувань не знайдено, використовую дефолтні", flush=True)
        except Exception as e:
            print(f"[WARN] Не вдалося завантажити налаштування: {e}", file=sys.stderr)

    def save_settings(self):
        data = {
            "osd": {
                "enabled": self.enable_telemetry_osd,
                "xpad": self.overlay_xpad,
                "ypad": self.overlay_ypad,
                "font_size": self.overlay_font_size,
                "background": self.overlay_background,
                "halign": self.overlay_halign,
                "valign": self.overlay_valign,
                "show_loss": self.show_loss,
                "show_distance": self.show_distance,
                "show_wavelength": self.show_wavelength,
            },
            "bridge": {
                "serial_dev": self.serial_dev or "",
                "serial_baudrate": self.serial_baudrate,
                "remote_host": self.bridge_remote_host,
                "remote_port": self.bridge_remote_port,
                "local_bind_ip": self.bridge_local_bind_ip,
                "local_bind_port": self.bridge_local_bind_port,
                "verbose": self.bridge_verbose,
                "hex": self.bridge_hex,
            },
            "video": {
                "port": self.port,
                "mode": self.mode,
                "always_on_top": self.always_on_top,
            },
            "mikrotik": {
                "host": self.mikrotik_host,
                "user": self.mikrotik_user,
                "password": self.mikrotik_password,
                "interface": self.mikrotik_interface,
            },
        }

        try:
            SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"[INFO] Налаштування збережено в {SETTINGS_FILE}", flush=True)
        except Exception as e:
            print(f"[ERROR] Не вдалося зберегти налаштування: {e}", file=sys.stderr)

    def get_overlay_text_for_pipeline_start(self) -> str:
        if not self.enable_telemetry_osd:
            return ""
        return "STATUS: Підключення до MikroTik..."

    def build_pipeline(self, port: int, mode: str, text: str) -> str:
        safe_text = self.escape_gst_text(text)
        bg_value = "true" if self.overlay_background else "false"

        overlay_block = f"""
            ! textoverlay name=overlay
                text="{safe_text}"
                valignment={self.overlay_valign}
                halignment={self.overlay_halign}
                shaded-background={bg_value}
                xpad={self.overlay_xpad}
                ypad={self.overlay_ypad}
                font-desc="Sans Bold {self.overlay_font_size}"
        """

        if mode == "raw":
            return f"""
                udpsrc port={port}
                    caps="video/x-h264,stream-format=byte-stream,alignment=au"
                ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=200000000 leaky=downstream
                ! h264parse config-interval=-1 disable-passthrough=true
                ! decodebin
                ! videoconvert
                {overlay_block}
                ! tee name=t

                t. ! queue
                   ! gtksink name=videosink sync=false

                t. ! queue leaky=downstream max-size-buffers=1
                   ! videoconvert
                   ! video/x-raw,format=RGB
                   ! appsink name=monitorsink emit-signals=true max-buffers=1 drop=true sync=false
            """

        if mode == "rtp":
            return f"""
                udpsrc port={port}
                    caps="application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
                ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=200000000 leaky=downstream
                ! rtpjitterbuffer latency=30 drop-on-latency=true
                ! rtph264depay
                ! h264parse config-interval=-1 disable-passthrough=true
                ! avdec_h264
                ! videoconvert
                {overlay_block}
                ! tee name=t

                t. ! queue
                   ! gtksink name=videosink sync=false

                t. ! queue leaky=downstream max-size-buffers=1
                   ! videoconvert
                   ! video/x-raw,format=RGB
                   ! appsink name=monitorsink emit-signals=true max-buffers=1 drop=true sync=false
            """

        raise ValueError("Невідомий режим. Використовуйте raw або rtp.")

    def build_and_start_pipeline(self, text: str):
        pipeline_str = self.build_pipeline(self.port, self.mode, text)
        print("Pipeline:")
        print(pipeline_str)

        self.pipeline = Gst.parse_launch(pipeline_str)
        self.overlay = self.pipeline.get_by_name("overlay")
        self.video_sink = self.pipeline.get_by_name("videosink")
        self.monitor_sink = self.pipeline.get_by_name("monitorsink")

        if self.video_sink is None:
            raise RuntimeError("Не вдалося знайти gtksink")
        if self.monitor_sink is None:
            raise RuntimeError("Не вдалося знайти monitorsink")
        if self.overlay is None:
            raise RuntimeError("Не вдалося знайти textoverlay")

        self.monitor_sink.connect("new-sample", self.on_monitor_new_sample)

        video_widget = self.video_sink.props.widget
        self.video_box.pack_start(video_widget, True, True, 0)

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.on_bus_message)

        self.pipeline.set_state(Gst.State.PLAYING)

    @staticmethod
    def escape_gst_text(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    def apply_overlay_visual_settings(self):
        if self.overlay is None:
            return

        font_desc = f"Sans Bold {self.overlay_font_size}"

        GLib.idle_add(self.overlay.set_property, "xpad", self.overlay_xpad)
        GLib.idle_add(self.overlay.set_property, "ypad", self.overlay_ypad)
        GLib.idle_add(self.overlay.set_property, "halignment", self.overlay_halign)
        GLib.idle_add(self.overlay.set_property, "valignment", self.overlay_valign)
        GLib.idle_add(self.overlay.set_property, "shaded-background", self.overlay_background)
        GLib.idle_add(self.overlay.set_property, "font-desc", font_desc)

    def refresh_video_area(self):
        self.video_overlay.queue_draw()
        self.video_box.queue_draw()

        if self.placeholder_visible:
            alloc = self.video_overlay.get_allocation()
            self.update_placeholder_image_size(alloc.width, alloc.height)

        return False

    def on_video_overlay_size_allocate(self, widget, allocation):
        if self.placeholder_visible:
            self.update_placeholder_image_size(allocation.width, allocation.height)

    def update_placeholder_image_size(self, avail_width: int, avail_height: int):
        if self.placeholder_original_pixbuf is None or self.placeholder_image is None:
            return

        if avail_width <= 1 or avail_height <= 1:
            return

        orig_w = self.placeholder_original_pixbuf.get_width()
        orig_h = self.placeholder_original_pixbuf.get_height()

        if orig_w <= 0 or orig_h <= 0:
            return

        max_w = max(1, avail_width - 20)
        max_h = max(1, avail_height - 20)

        scale = min(max_w / orig_w, max_h / orig_h)
        new_w = max(1, int(orig_w * scale))
        new_h = max(1, int(orig_h * scale))

        try:
            scaled = self.placeholder_original_pixbuf.scale_simple(
                new_w,
                new_h,
                GdkPixbuf.InterpType.BILINEAR,
            )
            if scaled is not None:
                self.placeholder_image.set_from_pixbuf(scaled)
        except Exception as e:
            print(f"[WARN] Не вдалося масштабувати placeholder image: {e}", file=sys.stderr)

    def on_monitor_new_sample(self, sink):
        self.last_video_frame_time = time.time()
        if self.placeholder_visible:
            GLib.idle_add(self.set_placeholder_visible, False)
        return Gst.FlowReturn.OK

    def set_placeholder_visible(self, visible: bool):
        self.placeholder_visible = visible
        if visible:
            self.placeholder_background.show()
            alloc = self.video_overlay.get_allocation()
            self.update_placeholder_image_size(alloc.width, alloc.height)
        else:
            self.placeholder_background.hide()
        return False

    def video_signal_loop(self):
        while self.running:
            try:
                now = time.time()
                has_signal = (
                    self.last_video_frame_time > 0
                    and (now - self.last_video_frame_time) <= self.video_signal_timeout_sec
                )

                if has_signal:
                    if self.placeholder_visible:
                        GLib.idle_add(self.set_placeholder_visible, False)
                else:
                    if not self.placeholder_visible:
                        GLib.idle_add(self.set_placeholder_visible, True)

            except Exception as e:
                print(f"[WARN] video_signal_loop: {e}", file=sys.stderr)

            time.sleep(0.2)

    def on_fullscreen_button_clicked(self, widget):
        GLib.idle_add(self.toggle_fullscreen_video)

    def on_placeholder_button_press(self, widget, event):
        if event.type == Gdk.EventType._2BUTTON_PRESS and event.button == 1:
            GLib.idle_add(self.toggle_fullscreen_video)
            return True
        return False

    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_F11:
            GLib.idle_add(self.toggle_fullscreen_video)
            return True

        if event.keyval == Gdk.KEY_Escape and self.is_video_fullscreen:
            GLib.idle_add(self.toggle_fullscreen_video, False)
            return True

        return False

    def toggle_fullscreen_video(self, force_state: Optional[bool] = None):
        if force_state is None:
            new_state = not self.is_video_fullscreen
        else:
            new_state = force_state

        if new_state == self.is_video_fullscreen:
            return False

        self.is_video_fullscreen = new_state

        if self.is_video_fullscreen:
            self.top_bar.hide()
            self.root.set_border_width(0)
            self.root.set_spacing(0)
            self.frame_video.set_shadow_type(Gtk.ShadowType.NONE)
            self.window.fullscreen()
            self.btn_fullscreen.set_image(
                Gtk.Image.new_from_icon_name("view-restore-symbolic", Gtk.IconSize.BUTTON)
            )
            self.btn_fullscreen.set_tooltip_text("Вийти з повного екрана")
        else:
            self.window.unfullscreen()
            self.root.set_border_width(self.default_root_border)
            self.root.set_spacing(self.default_root_spacing)
            self.top_bar.show()
            self.frame_video.set_shadow_type(Gtk.ShadowType.IN)
            self.btn_fullscreen.set_image(
                Gtk.Image.new_from_icon_name("view-fullscreen-symbolic", Gtk.IconSize.BUTTON)
            )
            self.btn_fullscreen.set_tooltip_text("На весь екран")

        GLib.idle_add(self.refresh_video_area)
        return False

    def set_overlay_text(self, text: str):
        if self.overlay is not None:
            GLib.idle_add(self.overlay.set_property, "text", text)

    def clear_overlay_text(self):
        self.set_overlay_text("")

    def restart_video_pipeline(self):
        old_text = ""
        try:
            if self.overlay is not None:
                old_text = self.overlay.get_property("text")
        except Exception:
            pass

        try:
            if self.pipeline is not None:
                self.pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass

        self.pipeline = None
        self.overlay = None
        self.video_sink = None
        self.monitor_sink = None
        self.bus = None

        for child in self.video_box.get_children():
            self.video_box.remove(child)

        self.last_video_frame_time = 0.0
        self.set_placeholder_visible(True)

        new_text = old_text
        if not self.enable_telemetry_osd:
            new_text = ""
        if not new_text:
            new_text = self.get_overlay_text_for_pipeline_start()

        self.build_and_start_pipeline(new_text)
        self.apply_overlay_visual_settings()

        self.video_overlay.queue_draw()
        self.video_box.queue_draw()

        if self.is_video_fullscreen:
            self.top_bar.hide()

        if self.placeholder_visible:
            alloc = self.video_overlay.get_allocation()
            self.update_placeholder_image_size(alloc.width, alloc.height)

    def restart_bridge(self):
        if self.bridge is not None:
            try:
                self.bridge.stop()
            except Exception:
                pass
            self.bridge = None

        if self.bridge_remote_host:
            self.ensure_bridge_running()

    def disable_mikrotik_runtime(self):
        with self.mt_lock:
            if self.mt_client is not None:
                try:
                    self.mt_client.disconnect()
                except Exception:
                    pass
            self.mt_client = None
            self.identity_name = ""
            self.mikrotik_reconnect_requested = False

        self.clear_overlay_text()

    def request_mikrotik_reconnect(self):
        with self.mt_lock:
            if self.mt_client is not None:
                try:
                    self.mt_client.disconnect()
                except Exception:
                    pass
            self.mt_client = None
            self.identity_name = ""
            self.mikrotik_reconnect_requested = True

        if self.enable_telemetry_osd:
            self.set_overlay_text("STATUS: Перепідключення до MikroTik...")
        else:
            self.clear_overlay_text()

    def check_bridge_health(self):
        if not self.bridge_remote_host:
            return

        if self.bridge is None:
            self.ensure_bridge_running()
            return

        if not self.bridge.is_alive():
            print("[WARN] Bridge is not alive, restarting...", flush=True)
            try:
                self.bridge.stop()
            except Exception:
                pass
            self.bridge = None
            self.ensure_bridge_running()
            return

        if self.bridge.is_stalled(timeout_sec=3.0):
            print("[WARN] Bridge seems stalled, restarting...", flush=True)
            try:
                self.bridge.stop()
            except Exception:
                pass
            self.bridge = None
            self.ensure_bridge_running()

    def build_overlay_text(
        self,
        rx_power: Optional[str],
        tx_power: Optional[str],
        temperature: Optional[str],
        voltage: Optional[str],
        wavelength: Optional[str],
        distance: Optional[str],
        error_text: Optional[str] = None,
    ) -> str:
        if not self.enable_telemetry_osd:
            return ""

        lines = []

        if error_text:
            lines.append(f"STATUS: {error_text}")
            return "\n".join(lines)

        rx_val = parse_dbm_value(rx_power)
        tx_val = parse_dbm_value(tx_power)

        if self.show_loss:
            loss_text = "N/A"
            if tx_val is not None and rx_val is not None:
                loss_text = f"{(tx_val - rx_val):.2f} dB"
            lines.append(f"LOSS: {loss_text}")

        wl_dist = []
        if self.show_wavelength and wavelength:
            wl_dist.append(f"WL: {wavelength}")
        if self.show_distance and distance:
            wl_dist.append(f"DIST: {distance}")

        if wl_dist:
            lines.append(" | ".join(wl_dist))

        return "\n".join(lines)

    def ensure_mikrotik_ready(self) -> bool:
        if not self.enable_telemetry_osd:
            return False

        if not self.mikrotik_host:
            self.set_overlay_text("STATUS: Пошук MikroTik через SSH...")
            found = auto_discover_mikrotik(
                username=self.mikrotik_user,
                password=self.mikrotik_password,
                port=self.ssh_port,
            )
            if not found:
                self.set_overlay_text("STATUS: MikroTik не знайдено")
                return False
            self.mikrotik_host = found

        client = MikroTikSshClient(
            host=self.mikrotik_host,
            username=self.mikrotik_user,
            password=self.mikrotik_password,
            port=self.ssh_port,
        )

        client.connect()
        identity = client.get_identity() or ""

        if not self.mikrotik_interface:
            self.set_overlay_text("STATUS: Пошук SFP інтерфейсу...")
            found_if = client.auto_discover_sfp_interface()
            if not found_if:
                client.disconnect()
                self.set_overlay_text("STATUS: SFP інтерфейс не знайдено")
                return False
            self.mikrotik_interface = found_if

        with self.mt_lock:
            old_client = self.mt_client
            self.mt_client = client
            self.identity_name = identity
            self.mikrotik_reconnect_requested = False

        if old_client is not None and old_client is not client:
            try:
                old_client.disconnect()
            except Exception:
                pass

        return True

    def poll_mikrotik_loop(self):
        while self.running:
            try:
                if not self.enable_telemetry_osd:
                    time.sleep(self.poll_interval)
                    continue

                reconnect_needed = False
                with self.mt_lock:
                    reconnect_needed = self.mt_client is None or self.mikrotik_reconnect_requested

                if reconnect_needed:
                    if not self.ensure_mikrotik_ready():
                        time.sleep(self.poll_interval)
                        continue

                with self.mt_lock:
                    client = self.mt_client
                    interface_name = self.mikrotik_interface

                if client is None or not interface_name:
                    self.set_overlay_text("STATUS: Немає підключення до MikroTik")
                    time.sleep(self.poll_interval)
                    continue

                try:
                    rx_power, tx_power, temperature, voltage, wavelength, distance = (
                        client.fetch_sfp_status(interface_name)
                    )
                    text = self.build_overlay_text(
                        rx_power=rx_power,
                        tx_power=tx_power,
                        temperature=temperature,
                        voltage=voltage,
                        wavelength=wavelength,
                        distance=distance,
                    )
                except Exception as e:
                    with self.mt_lock:
                        try:
                            if self.mt_client is not None:
                                self.mt_client.disconnect()
                        except Exception:
                            pass
                        self.mt_client = None
                        self.mikrotik_reconnect_requested = True

                    text = self.build_overlay_text(
                        rx_power=None,
                        tx_power=None,
                        temperature=None,
                        voltage=None,
                        wavelength=None,
                        distance=None,
                        error_text=f"SSH ERROR: {type(e).__name__}",
                    )

                self.set_overlay_text(text)
                time.sleep(self.poll_interval)

            except Exception as e:
                self.set_overlay_text(f"STATUS: INIT ERROR: {type(e).__name__}")
                print(f"Init error: {e}", file=sys.stderr)
                time.sleep(self.poll_interval)

    def ensure_bridge_running(self):
        if not self.bridge_remote_host:
            return

        if self.bridge is not None and self.bridge.is_alive():
            return

        if self.bridge is not None:
            try:
                self.bridge.stop()
            except Exception:
                pass
            self.bridge = None

        serial_dev_to_use = self.serial_dev

        if not serial_dev_to_use and self.auto_controller_enabled:
            serial_dev_to_use = find_controller_serial_device()

        if not serial_dev_to_use:
            return

        try:
            self.bridge = UdpSerialBridge(
                remote_host=self.bridge_remote_host,
                remote_port=self.bridge_remote_port,
                serial_dev=serial_dev_to_use,
                baudrate=self.serial_baudrate,
                local_bind_ip=self.bridge_local_bind_ip,
                local_bind_port=self.bridge_local_bind_port,
                verbose=self.bridge_verbose,
                hex_dump=self.bridge_hex,
            )
            self.bridge.start()
            self.serial_dev = serial_dev_to_use
            print(f"[INFO] Контролер підключено: {serial_dev_to_use}", flush=True)
        except Exception as e:
            print(f"[WARN] Не вдалося запустити bridge для {serial_dev_to_use}: {e}", file=sys.stderr)

            if isinstance(e, serial.SerialException) or "Permission denied" in str(e):
                print(
                    "[HINT] Немає доступу до serial-порту. Додайте користувача в групу dialout:\n"
                    "sudo usermod -aG dialout $USER\n"
                    "Потім перелогіньтесь або перезавантажтесь.",
                    file=sys.stderr,
                )

            self.bridge = None
            if self.auto_controller_enabled:
                self.serial_dev = None

    def controller_watch_loop(self):
        last_seen = None

        while self.running:
            try:
                found = find_controller_serial_device() if self.auto_controller_enabled else self.serial_dev

                if found != last_seen:
                    if found:
                        print(f"[INFO] Контролер знайдено: {found}", flush=True)
                    else:
                        print("[INFO] Контролер відключено", flush=True)
                    last_seen = found

                if self.auto_controller_enabled:
                    if found:
                        if self.bridge is None or not self.bridge.is_alive():
                            self.serial_dev = found
                            self.ensure_bridge_running()
                    else:
                        if self.bridge is not None:
                            print("[INFO] Зупиняю bridge, бо контролер зник", flush=True)
                            try:
                                self.bridge.stop()
                            except Exception:
                                pass
                            self.bridge = None
                            self.serial_dev = None
                else:
                    if (self.bridge is None or not self.bridge.is_alive()) and self.serial_dev:
                        self.ensure_bridge_running()

            except Exception as e:
                print(f"[WARN] controller_watch_loop: {e}", file=sys.stderr)

            time.sleep(1.0)

    def bridge_info_loop(self):
        while self.running:
            try:
                self.check_bridge_health()
            except Exception as e:
                print(f"[WARN] bridge_info_loop: {e}", file=sys.stderr)
            time.sleep(1.0)

    def warn_udp_network_mismatch_if_needed(self):
        if self.network_warning_shown:
            return False

        if not self.bridge_remote_host:
            return False

        try:
            remote_ip = ipaddress.ip_address(self.bridge_remote_host)
        except Exception:
            return False

        if remote_ip.is_loopback:
            return False

        interfaces = get_local_ipv4_interfaces()
        if not interfaces:
            return False

        for item in interfaces:
            try:
                network = ipaddress.ip_network(item["network"], strict=False)
                if remote_ip in network:
                    return False
            except Exception:
                continue

        self.network_warning_shown = True
        self.show_message(
            "Попередження про мережу",
            (
                f"UDP віддалений host ({self.bridge_remote_host}) не входить у жодну локальну мережу.\n\n"
                "Перевірте налаштування IP та переконайтеся, що вони співпадають з мережею "
                "вашого мережного інтерфейсу."
            ),
            Gtk.MessageType.WARNING,
        )
        return False

    def on_bus_message(self, bus, message):
        msg_type = message.type

        if msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print("GStreamer ERROR:", err, file=sys.stderr)
            if debug:
                print("DEBUG:", debug, file=sys.stderr)

        elif msg_type == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            print("GStreamer WARNING:", warn, file=sys.stderr)
            if debug:
                print("DEBUG:", debug, file=sys.stderr)

        elif msg_type == Gst.MessageType.EOS:
            print("Кінець потоку")

    def show_message(self, title: str, text: str, message_type=Gtk.MessageType.INFO):
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            flags=0,
            message_type=message_type,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dialog.format_secondary_text(text)
        dialog.run()
        dialog.destroy()

    def find_icon_source(self) -> Optional[Path]:
        candidates = [
            resource_path("prince_ground_station.png"),
            resource_path("prince.png"),
            resource_path("icon.png"),
            resource_path("app.png"),
            Path(__file__).resolve().parent / "prince_ground_station.png",
            Path(__file__).resolve().parent / "prince.png",
            Path(__file__).resolve().parent / "icon.png",
            Path(__file__).resolve().parent / "app.png",
        ]
        return first_existing_path(candidates)

    def create_desktop_shortcut(self):
        try:
            apps_dir = Path.home() / ".local" / "share" / "applications"
            icons_dir = Path.home() / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps"
            desktop_dir = get_desktop_dir()

            apps_dir.mkdir(parents=True, exist_ok=True)
            icons_dir.mkdir(parents=True, exist_ok=True)
            desktop_dir.mkdir(parents=True, exist_ok=True)

            appimage_path = os.environ.get("APPIMAGE")
            if appimage_path:
                exec_line = str(Path(appimage_path).resolve())
            else:
                src = Path(__file__).resolve()
                exec_line = f'python3 "{src}"'

            icon_source = self.find_icon_source()
            icon_line = ""
            if icon_source is not None:
                icon_target = icons_dir / "prince_ground_station.png"
                shutil.copy2(icon_source, icon_target)
                icon_line = str(icon_target.resolve())

                try:
                    subprocess.run(
                        ["gtk-update-icon-cache", "-f", "-t", str(icons_dir.parent)],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass

                try:
                    subprocess.run(
                        ["update-desktop-database", str(apps_dir)],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass
            else:
                self.show_message(
                    "Попередження",
                    "Файл іконки не знайдено. Ярлик буде створено без красивої іконки.",
                    Gtk.MessageType.WARNING,
                )

            desktop_content = f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Comment={APP_NAME}
Exec={exec_line}
Icon={icon_line}
Terminal=false
Categories=Utility;Network;Video;
StartupNotify=true
"""

            menu_desktop_file = apps_dir / f"{APP_ID}.desktop"
            menu_desktop_file.write_text(desktop_content, encoding="utf-8")
            menu_desktop_file.chmod(menu_desktop_file.stat().st_mode | stat.S_IXUSR)

            desktop_shortcut = desktop_dir / f"{APP_NAME}.desktop"
            desktop_shortcut.write_text(desktop_content, encoding="utf-8")
            desktop_shortcut.chmod(desktop_shortcut.stat().st_mode | stat.S_IXUSR)

            self.show_message(
                "Ярлики створено",
                f"Створено ярлик у меню:\n{menu_desktop_file}\n\n"
                f"Створено ярлик на робочому столі:\n{desktop_shortcut}",
                Gtk.MessageType.INFO,
            )

        except Exception as e:
            self.show_message(
                "Помилка створення ярлика",
                str(e),
                Gtk.MessageType.ERROR,
            )

    # def ask_to_fix_udp_network_if_needed(self):
    #     if not self.bridge_remote_host:
    #         return

    #     try:
    #         remote_ip = ipaddress.ip_address(self.bridge_remote_host)
    #     except Exception:
    #         return

    #     if remote_ip.is_loopback:
    #         return

    #     interfaces = get_local_ipv4_interfaces()
    #     if not interfaces:
    #         return

    #     matching = []
    #     for item in interfaces:
    #         try:
    #             network = ipaddress.ip_network(item["network"], strict=False)
    #             if remote_ip in network:
    #                 matching.append(item)
    #         except Exception:
    #             continue

    #     if matching:
    #         return

    #     dialog = Gtk.Dialog(
    #         title="Невідповідність мережі",
    #         transient_for=self.window,
    #         flags=0,
    #     )
    #     dialog.add_button("Пропустити", Gtk.ResponseType.CANCEL)
    #     dialog.add_button("Застосувати", Gtk.ResponseType.OK)
    #     dialog.set_default_size(520, 260)

    #     box = dialog.get_content_area()
    #     box.set_spacing(10)
    #     box.set_border_width(12)

    #     label = Gtk.Label(
    #         label=(
    #             "Поточний UDP host не належить жодній з локальних мереж.\n"
    #             "Оберіть мережний інтерфейс і, за потреби, змініть UDP host."
    #         )
    #     )
    #     label.set_xalign(0.0)
    #     label.set_line_wrap(True)
    #     box.pack_start(label, False, False, 0)

    #     grid = Gtk.Grid()
    #     grid.set_row_spacing(10)
    #     grid.set_column_spacing(12)
    #     box.pack_start(grid, False, False, 0)

    #     combo_iface = Gtk.ComboBoxText()
    #     for item in interfaces:
    #         iface_id = f'{item["ifname"]}|{item["ip"]}|{item["network"]}'
    #         iface_text = f'{item["ifname"]} | {item["ip"]} | {item["network"]}'
    #         combo_iface.append(iface_id, iface_text)
    #     combo_iface.set_active(0)

    #     entry_host = Gtk.Entry()
    #     entry_host.set_text(self.bridge_remote_host)

    #     entry_bind_ip = Gtk.Entry()
    #     entry_bind_ip.set_text(self.bridge_local_bind_ip)

    #     grid.attach(Gtk.Label(label="Інтерфейс:"), 0, 0, 1, 1)
    #     grid.attach(combo_iface, 1, 0, 1, 1)

    #     grid.attach(Gtk.Label(label="UDP host:"), 0, 1, 1, 1)
    #     grid.attach(entry_host, 1, 1, 1, 1)

    #     grid.attach(Gtk.Label(label="Local bind IP:"), 0, 2, 1, 1)
    #     grid.attach(entry_bind_ip, 1, 2, 1, 1)

    #     def on_iface_changed(widget):
    #         iface_data = combo_iface.get_active_id()
    #         if not iface_data:
    #             return

    #         _, iface_ip, iface_network = iface_data.split("|", 2)
    #         entry_bind_ip.set_text(iface_ip)

    #         try:
    #             net = ipaddress.ip_network(iface_network, strict=False)
    #             current_host = entry_host.get_text().strip()
    #             try:
    #                 current_ip = ipaddress.ip_address(current_host)
    #                 if current_ip in net:
    #                     return
    #             except Exception:
    #                 pass

    #             if net.prefixlen <= 24:
    #                 hosts = list(net.hosts())
    #                 if hosts:
    #                     suggested = str(hosts[min(49, len(hosts) - 1)])
    #                     entry_host.set_text(suggested)
    #         except Exception:
    #             pass

    #     combo_iface.connect("changed", on_iface_changed)
    #     on_iface_changed(combo_iface)

    #     dialog.show_all()
    #     response = dialog.run()

    #     if response == Gtk.ResponseType.OK:
    #         self.bridge_remote_host = entry_host.get_text().strip()
    #         self.bridge_local_bind_ip = entry_bind_ip.get_text().strip() or "0.0.0.0"
    #         self.save_settings()
    #         self.restart_bridge()

    #     dialog.destroy()

    def make_section(self, title: str) -> Tuple[Gtk.Frame, Gtk.Grid]:
        frame = Gtk.Frame(label=title)
        frame.set_hexpand(True)
        frame.set_margin_top(4)
        frame.set_margin_bottom(4)

        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(12)
        grid.set_border_width(12)

        frame.add(grid)
        return frame, grid

    def add_labeled_row(self, grid: Gtk.Grid, row: int, label_text: str, widget: Gtk.Widget):
        label = Gtk.Label(label=label_text)
        label.set_xalign(0.0)
        label.set_halign(Gtk.Align.START)
        widget.set_hexpand(True)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)

    def open_ground_station_settings(self, widget):
        dialog = Gtk.Dialog(
            title="Налаштування наземної станції",
            transient_for=self.window,
            flags=0,
        )
        dialog.set_default_size(780, 720)
        dialog.set_resizable(True)

        dialog.add_button("Створити ярлик", 2)
        dialog.add_button("Скинути", 1)
        dialog.add_button("Скасувати", Gtk.ResponseType.CANCEL)
        dialog.add_button("Застосувати", Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer_box.set_border_width(12)
        content.add(outer_box)

        notebook = Gtk.Notebook()
        notebook.set_hexpand(True)
        notebook.set_vexpand(True)
        outer_box.pack_start(notebook, True, True, 0)

        osd_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        osd_page.set_border_width(8)

        info_frame, info_grid = self.make_section("Пояснення")
        info_label = Gtk.Label(
            label=(
                "OSD — це наекранне меню з даними, які беруться з MikroTik / SFP.\n"
                "Можна повністю вимкнути телеметрію та OSD одним чекбоксом нижче."
            )
        )
        info_label.set_xalign(0.0)
        info_label.set_line_wrap(True)
        info_grid.attach(info_label, 0, 0, 2, 1)

        frame_show, grid_show = self.make_section("Телеметрія та OSD")
        chk_enable_telemetry_osd = Gtk.CheckButton(label="Увімкнути телеметрію MikroTik і показ OSD")
        chk_enable_telemetry_osd.set_active(self.enable_telemetry_osd)
        grid_show.attach(chk_enable_telemetry_osd, 0, 0, 2, 1)

        frame_pos, grid_pos = self.make_section("Позиція")
        spin_x = Gtk.SpinButton()
        spin_x.set_range(0, 5000)
        spin_x.set_increments(1, 10)
        spin_x.set_value(self.overlay_xpad)
        self.add_labeled_row(grid_pos, 0, "X:", spin_x)

        spin_y = Gtk.SpinButton()
        spin_y.set_range(0, 5000)
        spin_y.set_increments(1, 10)
        spin_y.set_value(self.overlay_ypad)
        self.add_labeled_row(grid_pos, 1, "Y:", spin_y)

        frame_style, grid_style = self.make_section("Стиль")
        spin_font = Gtk.SpinButton()
        spin_font.set_range(6, 72)
        spin_font.set_increments(1, 2)
        spin_font.set_value(self.overlay_font_size)
        self.add_labeled_row(grid_style, 0, "Розмір шрифту:", spin_font)

        chk_bg = Gtk.CheckButton(label="Фон")
        chk_bg.set_active(self.overlay_background)
        grid_style.attach(chk_bg, 0, 1, 2, 1)

        combo_halign = Gtk.ComboBoxText()
        combo_halign.append("left", "Ліворуч")
        combo_halign.append("right", "Праворуч")
        combo_halign.set_active_id(self.overlay_halign)
        self.add_labeled_row(grid_style, 2, "Горизонтально:", combo_halign)

        combo_valign = Gtk.ComboBoxText()
        combo_valign.append("top", "Вгорі")
        combo_valign.append("bottom", "Внизу")
        combo_valign.set_active_id(self.overlay_valign)
        self.add_labeled_row(grid_style, 3, "Вертикально:", combo_valign)

        frame_data, grid_data = self.make_section("Що показувати")
        chk_show_loss = Gtk.CheckButton(label="Показувати затухання")
        chk_show_loss.set_active(self.show_loss)
        grid_data.attach(chk_show_loss, 0, 0, 2, 1)

        chk_show_distance = Gtk.CheckButton(label="Показувати максимальну дистанцію SFP")
        chk_show_distance.set_active(self.show_distance)
        grid_data.attach(chk_show_distance, 0, 1, 2, 1)

        chk_show_wavelength = Gtk.CheckButton(label="Показувати довжину хвилі SFP")
        chk_show_wavelength.set_active(self.show_wavelength)
        grid_data.attach(chk_show_wavelength, 0, 2, 2, 1)

        osd_page.pack_start(info_frame, False, False, 0)
        osd_page.pack_start(frame_show, False, False, 0)
        osd_page.pack_start(frame_pos, False, False, 0)
        osd_page.pack_start(frame_style, False, False, 0)
        osd_page.pack_start(frame_data, False, False, 0)
        osd_page.pack_start(Gtk.Box(), True, True, 0)
        notebook.append_page(osd_page, Gtk.Label(label="OSD"))

        bridge_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        bridge_page.set_border_width(8)

        hint_frame, hint_grid = self.make_section("Права доступу до serial-порту")
        hint_label = Gtk.Label(
            label=(
                "Якщо bridge не може відкрити /dev/ttyACM0 або /dev/ttyUSB0 через Permission denied,\n"
                "виконайте в терміналі:\n\n"
                "sudo usermod -aG dialout $USER\n\n"
                "Після цього перелогіньтесь або перезавантажтесь."
            )
        )
        hint_label.set_xalign(0.0)
        hint_label.set_line_wrap(True)
        hint_label.set_selectable(True)
        hint_grid.attach(hint_label, 0, 0, 2, 1)

        frame_serial, grid_serial = self.make_section("Serial")
        combo_serial_dev = Gtk.ComboBoxText()
        combo_serial_dev.append("__auto__", "Auto (автопошук Raspberry Pi Pico)")
        current_serial_items = list_serial_devices()
        selected_serial_id = "__auto__" if not self.serial_dev else self.serial_dev
        found_selected = selected_serial_id == "__auto__"

        for dev, text in current_serial_items:
            combo_serial_dev.append(dev, text)
            if dev == self.serial_dev:
                found_selected = True

        if self.serial_dev and not found_selected:
            combo_serial_dev.append(self.serial_dev, f"{self.serial_dev} | (збережений пристрій)")
            found_selected = True

        combo_serial_dev.set_active_id(selected_serial_id if found_selected else "__auto__")
        self.add_labeled_row(grid_serial, 0, "Пристрій:", combo_serial_dev)

        spin_serial_baud = Gtk.SpinButton()
        spin_serial_baud.set_range(1200, 5000000)
        spin_serial_baud.set_increments(100, 1000)
        spin_serial_baud.set_value(self.serial_baudrate)
        self.add_labeled_row(grid_serial, 1, "Baudrate:", spin_serial_baud)

        frame_udp, grid_udp = self.make_section("UDP")
        entry_remote_host = Gtk.Entry()
        entry_remote_host.set_text(self.bridge_remote_host)
        self.add_labeled_row(grid_udp, 0, "Віддалений host:", entry_remote_host)

        spin_remote_port = Gtk.SpinButton()
        spin_remote_port.set_range(0, 65535)
        spin_remote_port.set_value(self.bridge_remote_port)
        self.add_labeled_row(grid_udp, 1, "Віддалений порт:", spin_remote_port)

        entry_local_bind_ip = Gtk.Entry()
        entry_local_bind_ip.set_text(self.bridge_local_bind_ip)
        self.add_labeled_row(grid_udp, 2, "Локальний bind IP:", entry_local_bind_ip)

        spin_local_bind_port = Gtk.SpinButton()
        spin_local_bind_port.set_range(0, 65535)
        spin_local_bind_port.set_value(self.bridge_local_bind_port)
        self.add_labeled_row(grid_udp, 3, "Локальний bind порт:", spin_local_bind_port)

        frame_logs, grid_logs = self.make_section("Логи")
        chk_bridge_verbose = Gtk.CheckButton(label="Показувати логи bridge")
        chk_bridge_verbose.set_active(self.bridge_verbose)
        grid_logs.attach(chk_bridge_verbose, 0, 0, 2, 1)

        chk_bridge_hex = Gtk.CheckButton(label="Показувати hex у логах bridge")
        chk_bridge_hex.set_active(self.bridge_hex)
        grid_logs.attach(chk_bridge_hex, 0, 1, 2, 1)

        bridge_page.pack_start(hint_frame, False, False, 0)
        bridge_page.pack_start(frame_serial, False, False, 0)
        bridge_page.pack_start(frame_udp, False, False, 0)
        bridge_page.pack_start(frame_logs, False, False, 0)
        bridge_page.pack_start(Gtk.Box(), True, True, 0)
        notebook.append_page(bridge_page, Gtk.Label(label="Міст керування"))

        video_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        video_page.set_border_width(8)

        frame_video_main, grid_video_main = self.make_section("Основне")
        spin_video_port = Gtk.SpinButton()
        spin_video_port.set_range(1, 65535)
        spin_video_port.set_value(self.port)
        self.add_labeled_row(grid_video_main, 0, "UDP порт:", spin_video_port)

        combo_video_mode = Gtk.ComboBoxText()
        combo_video_mode.append("raw", "raw")
        combo_video_mode.append("rtp", "rtp")
        combo_video_mode.set_active_id(self.mode)
        self.add_labeled_row(grid_video_main, 1, "Режим:", combo_video_mode)

        frame_window_behavior, grid_window_behavior = self.make_section("Поведінка вікна")
        chk_always_on_top = Gtk.CheckButton(label="Поверх інших вікон")
        chk_always_on_top.set_active(self.always_on_top)
        grid_window_behavior.attach(chk_always_on_top, 0, 0, 2, 1)

        video_page.pack_start(frame_video_main, False, False, 0)
        video_page.pack_start(frame_window_behavior, False, False, 0)
        video_page.pack_start(Gtk.Box(), True, True, 0)
        notebook.append_page(video_page, Gtk.Label(label="Відеопотік"))

        mt_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        mt_page.set_border_width(8)

        frame_mt_conn, grid_mt_conn = self.make_section("Підключення")
        entry_mt_host = Gtk.Entry()
        entry_mt_host.set_text(self.mikrotik_host or "")
        self.add_labeled_row(grid_mt_conn, 0, "MikroTik host:", entry_mt_host)

        entry_mt_user = Gtk.Entry()
        entry_mt_user.set_text(self.mikrotik_user)
        self.add_labeled_row(grid_mt_conn, 1, "Логін:", entry_mt_user)

        entry_mt_password = Gtk.Entry()
        entry_mt_password.set_visibility(False)
        entry_mt_password.set_text(self.mikrotik_password)
        self.add_labeled_row(grid_mt_conn, 2, "Пароль:", entry_mt_password)

        frame_mt_if, grid_mt_if = self.make_section("Інтерфейс")
        entry_mt_if = Gtk.Entry()
        entry_mt_if.set_text(self.mikrotik_interface or "")
        self.add_labeled_row(grid_mt_if, 0, "SFP інтерфейс:", entry_mt_if)

        lbl_version = Gtk.Label(label=f"Версія: {APP_VERSION}")
        lbl_version.set_xalign(0.0)

        mt_page.pack_start(frame_mt_conn, False, False, 0)
        mt_page.pack_start(frame_mt_if, False, False, 0)
        mt_page.pack_start(lbl_version, False, False, 0)
        mt_page.pack_start(Gtk.Box(), True, True, 0)
        notebook.append_page(mt_page, Gtk.Label(label="MikroTik / SFP"))

        def update_osd_widgets_state():
            enabled = chk_enable_telemetry_osd.get_active()

            spin_x.set_sensitive(enabled)
            spin_y.set_sensitive(enabled)
            spin_font.set_sensitive(enabled)
            chk_bg.set_sensitive(enabled)
            combo_halign.set_sensitive(enabled)
            combo_valign.set_sensitive(enabled)
            chk_show_loss.set_sensitive(enabled)
            chk_show_distance.set_sensitive(enabled)
            chk_show_wavelength.set_sensitive(enabled)

            entry_mt_host.set_sensitive(enabled)
            entry_mt_user.set_sensitive(enabled)
            entry_mt_password.set_sensitive(enabled)
            entry_mt_if.set_sensitive(enabled)

        chk_enable_telemetry_osd.connect("toggled", lambda *_: update_osd_widgets_state())

        def apply_defaults_to_widgets():
            chk_enable_telemetry_osd.set_active(True)

            spin_x.set_value(0)
            spin_y.set_value(0)
            spin_font.set_value(8)
            chk_bg.set_active(False)
            combo_halign.set_active_id("right")
            combo_valign.set_active_id("bottom")
            chk_show_loss.set_active(True)
            chk_show_distance.set_active(True)
            chk_show_wavelength.set_active(True)

            combo_serial_dev.set_active_id("__auto__")
            spin_serial_baud.set_value(420000)
            entry_remote_host.set_text("192.168.121.50")
            spin_remote_port.set_value(9000)
            entry_local_bind_ip.set_text("0.0.0.0")
            spin_local_bind_port.set_value(0)
            chk_bridge_verbose.set_active(False)
            chk_bridge_hex.set_active(True)

            spin_video_port.set_value(5600)
            combo_video_mode.set_active_id("rtp")
            chk_always_on_top.set_active(True)

            entry_mt_host.set_text("192.168.121.1")
            entry_mt_user.set_text("admin")
            entry_mt_password.set_text("")
            entry_mt_if.set_text("sfp1")

            update_osd_widgets_state()

        update_osd_widgets_state()
        dialog.show_all()

        while True:
            response = dialog.run()

            if response == 1:
                apply_defaults_to_widgets()
                continue

            if response == 2:
                self.create_desktop_shortcut()
                continue

            if response == Gtk.ResponseType.OK:
                prev_video_port = self.port
                prev_video_mode = self.mode
                prev_enable_telemetry_osd = self.enable_telemetry_osd

                prev_bridge_state = (
                    self.serial_dev,
                    self.serial_baudrate,
                    self.bridge_remote_host,
                    self.bridge_remote_port,
                    self.bridge_local_bind_ip,
                    self.bridge_local_bind_port,
                    self.bridge_verbose,
                    self.bridge_hex,
                )

                prev_mikrotik_state = (
                    self.mikrotik_host,
                    self.mikrotik_user,
                    self.mikrotik_password,
                    self.mikrotik_interface,
                )

                self.enable_telemetry_osd = chk_enable_telemetry_osd.get_active()

                self.overlay_xpad = spin_x.get_value_as_int()
                self.overlay_ypad = spin_y.get_value_as_int()
                self.overlay_font_size = spin_font.get_value_as_int()
                self.overlay_background = chk_bg.get_active()
                self.overlay_halign = combo_halign.get_active_id() or "right"
                self.overlay_valign = combo_valign.get_active_id() or "bottom"
                self.show_loss = chk_show_loss.get_active()
                self.show_distance = chk_show_distance.get_active()
                self.show_wavelength = chk_show_wavelength.get_active()

                selected_serial = combo_serial_dev.get_active_id() or "__auto__"
                self.serial_dev = None if selected_serial == "__auto__" else selected_serial
                self.auto_controller_enabled = not bool(self.serial_dev)

                self.serial_baudrate = spin_serial_baud.get_value_as_int()
                self.bridge_remote_host = entry_remote_host.get_text().strip()
                self.bridge_remote_port = spin_remote_port.get_value_as_int()
                self.bridge_local_bind_ip = entry_local_bind_ip.get_text().strip() or "0.0.0.0"
                self.bridge_local_bind_port = spin_local_bind_port.get_value_as_int()
                self.bridge_verbose = chk_bridge_verbose.get_active()
                self.bridge_hex = chk_bridge_hex.get_active()

                self.port = spin_video_port.get_value_as_int()
                self.mode = combo_video_mode.get_active_id() or "rtp"
                self.always_on_top = chk_always_on_top.get_active()

                self.mikrotik_host = entry_mt_host.get_text().strip()
                self.mikrotik_user = entry_mt_user.get_text().strip() or "admin"
                self.mikrotik_password = entry_mt_password.get_text()
                self.mikrotik_interface = entry_mt_if.get_text().strip()

                self.save_settings()
                self.window.set_keep_above(self.always_on_top)

                video_pipeline_changed = (self.port != prev_video_port) or (self.mode != prev_video_mode)

                bridge_state = (
                    self.serial_dev,
                    self.serial_baudrate,
                    self.bridge_remote_host,
                    self.bridge_remote_port,
                    self.bridge_local_bind_ip,
                    self.bridge_local_bind_port,
                    self.bridge_verbose,
                    self.bridge_hex,
                )
                bridge_changed = bridge_state != prev_bridge_state

                mikrotik_state = (
                    self.mikrotik_host,
                    self.mikrotik_user,
                    self.mikrotik_password,
                    self.mikrotik_interface,
                )
                mikrotik_changed = mikrotik_state != prev_mikrotik_state

                if video_pipeline_changed:
                    self.restart_video_pipeline()
                else:
                    self.apply_overlay_visual_settings()
                    if self.enable_telemetry_osd:
                        if not prev_enable_telemetry_osd:
                            self.set_overlay_text("STATUS: Підключення до MikroTik...")
                        GLib.idle_add(self.refresh_video_area)
                    else:
                        self.clear_overlay_text()
                        GLib.idle_add(self.refresh_video_area)

                if bridge_changed:
                    self.restart_bridge()
                    self.network_warning_shown = False
                    GLib.idle_add(self.warn_udp_network_mismatch_if_needed)

                if self.enable_telemetry_osd:
                    if mikrotik_changed or (prev_enable_telemetry_osd != self.enable_telemetry_osd):
                        self.request_mikrotik_reconnect()
                else:
                    if prev_enable_telemetry_osd:
                        self.disable_mikrotik_runtime()

            break

        dialog.destroy()

    def on_destroy(self, widget):
        self.running = False

        if self.bridge is not None:
            self.bridge.stop()

        with self.mt_lock:
            if self.mt_client is not None:
                self.mt_client.disconnect()
                self.mt_client = None

        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)

        Gtk.main_quit()


def main():
    parser = argparse.ArgumentParser(
        description="UDP H.264 viewer with MikroTik SSH auto-discovery and optional UDP<->Serial bridge"
    )
    parser.add_argument("--port", type=int, default=5600, help="UDP порт відео")
    parser.add_argument("--mode", choices=["raw", "rtp"], default="rtp", help="Тип потоку: raw H264 або RTP H264")
    parser.add_argument("--always-on-top", action="store_true", help="Тримати вікно поверх інших")

    parser.add_argument("--mikrotik-host", default="192.168.121.1", help="IP MikroTik")
    parser.add_argument("--mikrotik-user", default="admin", help="Логін MikroTik")
    parser.add_argument("--mikrotik-password", default="", help="Пароль MikroTik")
    parser.add_argument("--mikrotik-interface", default="sfp1", help="SFP інтерфейс")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Інтервал опитування в секундах")
    parser.add_argument("--ssh-port", type=int, default=22, help="Порт SSH MikroTik")

    parser.add_argument("--serial-dev", default="", help="Serial device для bridge")
    parser.add_argument("--serial-baudrate", type=int, default=420000, help="Baudrate для bridge")
    parser.add_argument("--bridge-remote-host", default="192.168.121.50", help="Віддалена UDP IP-адреса для bridge")
    parser.add_argument("--bridge-remote-port", default=9000, type=int, help="Віддалений UDP порт для bridge")
    parser.add_argument("--bridge-local-bind-ip", default="0.0.0.0", help="Локальний bind IP для bridge")
    parser.add_argument("--bridge-local-bind-port", default=0, type=int, help="Локальний bind порт для bridge")
    parser.add_argument("--bridge-verbose", action="store_true", help="Показувати логи bridge")
    parser.add_argument("--bridge-hex", action="store_true", help="Показувати hex у логах bridge")

    args = parser.parse_args()

    window = UdpVideoWindow(
        port=args.port,
        mode=args.mode,
        always_on_top=args.always_on_top,
        mikrotik_host=args.mikrotik_host or None,
        mikrotik_user=args.mikrotik_user,
        mikrotik_password=args.mikrotik_password,
        mikrotik_interface=args.mikrotik_interface or None,
        poll_interval=args.poll_interval,
        ssh_port=args.ssh_port,
        serial_dev=args.serial_dev or None,
        serial_baudrate=args.serial_baudrate,
        bridge_remote_host=args.bridge_remote_host,
        bridge_remote_port=args.bridge_remote_port,
        bridge_local_bind_ip=args.bridge_local_bind_ip,
        bridge_local_bind_port=args.bridge_local_bind_port,
        bridge_verbose=args.bridge_verbose,
        bridge_hex=args.bridge_hex,
    )
    window.window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()