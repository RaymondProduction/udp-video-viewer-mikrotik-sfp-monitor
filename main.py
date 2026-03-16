#!/usr/bin/env python3
import argparse
import binascii
import ipaddress
import json
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, List, Tuple

import gi
import paramiko
import serial
from serial.tools import list_ports

gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")

from gi.repository import Gtk, Gst, GLib

Gst.init(None)

SETTINGS_FILE = Path(__file__).resolve().parent / "ground_station_settings.json"


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


def try_mikrotik_ssh(
    host: str,
    username: str,
    password: str,
    port: int,
) -> bool:
    client = None
    try:
        client = MikroTikSshClient(
            host=host,
            username=username,
            password=password,
            port=port,
        )
        client.connect()
        identity = client.get_identity()
        return bool(identity)
    except Exception:
        return False
    finally:
        if client is not None:
            client.disconnect()


def auto_discover_mikrotik(
    username: str,
    password: str,
    port: int,
) -> Optional[str]:
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
        self.sock: Optional[socket.socket] = None
        self.ser: Optional[serial.Serial] = None

        self.bytes_udp_to_serial = 0
        self.bytes_serial_to_udp = 0
        self.packets_udp_to_serial = 0
        self.packets_serial_to_udp = 0

        self.actual_local_addr = "N/A"

        self.t_udp_to_serial: Optional[threading.Thread] = None
        self.t_serial_to_udp: Optional[threading.Thread] = None

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

    def start(self):
        self.stop()

        self.info(
            f"Opening serial: {self.serial_dev} @ {self.baudrate} (8N1, no flow control)"
        )
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

        self.running = True

        self.t_udp_to_serial = threading.Thread(
            target=self.udp_to_serial_loop,
            daemon=True,
            name="udp_to_serial",
        )
        self.t_serial_to_udp = threading.Thread(
            target=self.serial_to_udp_loop,
            daemon=True,
            name="serial_to_udp",
        )

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
            return self.ser is not None and self.ser.is_open
        except Exception:
            return False

    def udp_to_serial_loop(self):
        while self.running:
            try:
                if self.sock is None or self.ser is None:
                    break

                data = self.sock.recv(4096)
                if not data:
                    continue

                self.ser.write(data)
                self.packets_udp_to_serial += 1
                self.bytes_udp_to_serial += len(data)

                if self.hex_dump:
                    self.log(
                        f"UDP -> SERIAL | {len(data)} bytes | hex={self.short_hex(data)}"
                    )
                else:
                    self.log(f"UDP -> SERIAL | {len(data)} bytes")

            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                self.err(f"udp_to_serial: {e}")
                time.sleep(0.1)

    def serial_to_udp_loop(self):
        while self.running:
            try:
                if self.sock is None or self.ser is None:
                    break

                data = self.ser.read(4096)
                if not data:
                    continue

                self.sock.send(data)
                self.packets_serial_to_udp += 1
                self.bytes_serial_to_udp += len(data)

                if self.hex_dump:
                    self.log(
                        f"SERIAL -> UDP | {len(data)} bytes | hex={self.short_hex(data)}"
                    )
                else:
                    self.log(f"SERIAL -> UDP | {len(data)} bytes")

            except OSError:
                break
            except Exception as e:
                self.err(f"serial_to_udp: {e}")
                time.sleep(0.1)

    def stats_text(self) -> str:
        return (
            f"local={self.actual_local_addr} remote={self.remote_host}:{self.remote_port} "
            f"| U->S: {self.packets_udp_to_serial} pkt / {self.bytes_udp_to_serial} B "
            f"| S->U: {self.packets_serial_to_udp} pkt / {self.bytes_serial_to_udp} B"
        )


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

        self.load_settings()

        self.window = Gtk.Window(title="UDP Video Viewer + MikroTik SFP Monitor")
        self.window.set_default_size(1180, 760)
        self.window.set_keep_above(self.always_on_top)
        self.window.connect("destroy", self.on_destroy)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_border_width(10)
        self.window.add(root)

        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        root.pack_start(top_bar, False, False, 0)

        top_bar.pack_start(Gtk.Label(label=""), True, True, 0)

        btn_settings = Gtk.Button(label="Налаштування...")
        btn_settings.connect("clicked", self.open_ground_station_settings)
        top_bar.pack_start(btn_settings, False, False, 0)

        self.info_label = Gtk.Label(label="Video init...")
        self.info_label.set_xalign(0.0)
        self.info_label.set_line_wrap(True)
        root.pack_start(self.info_label, False, False, 0)

        frame_video = Gtk.Frame()
        frame_video.set_shadow_type(Gtk.ShadowType.IN)
        root.pack_start(frame_video, True, True, 0)

        self.video_box = Gtk.Box()
        frame_video.add(self.video_box)

        self.pipeline = None
        self.overlay = None
        self.video_sink = None
        self.bus = None

        self.last_rx_power = None
        self.last_tx_power = None
        self.last_temperature = None
        self.last_voltage = None
        self.last_wavelength = None
        self.last_distance = None
        self.last_error_text = None

        self.build_and_start_pipeline("Connecting to MikroTik SSH...")

        self.bridge: Optional[UdpSerialBridge] = None
        if self.bridge_remote_host:
            self.ensure_bridge_running()

        self.window.show_all()

        self.mt_client: Optional[MikroTikSshClient] = None
        self.poll_thread = threading.Thread(target=self.poll_mikrotik_loop, daemon=True)
        self.poll_thread.start()

        self.bridge_info_thread = threading.Thread(target=self.bridge_info_loop, daemon=True)
        self.bridge_info_thread.start()

        self.controller_watch_thread = threading.Thread(
            target=self.controller_watch_loop,
            daemon=True,
        )
        self.controller_watch_thread.start()

    def set_default_settings(self):
        self.overlay_xpad = 0
        self.overlay_ypad = 400
        self.overlay_font_size = 8
        self.overlay_background = False
        self.overlay_halign = "right"
        self.overlay_valign = "top"

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
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            osd = data.get("osd", {})
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

            print(f"[INFO] Ground station settings loaded from {SETTINGS_FILE}", flush=True)

        except FileNotFoundError:
            print("[INFO] Settings file not found, using defaults", flush=True)
        except Exception as e:
            print(f"[WARN] Failed to load settings: {e}", file=sys.stderr)

    def save_settings(self):
        data = {
            "osd": {
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
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"[INFO] Ground station settings saved to {SETTINGS_FILE}", flush=True)
        except Exception as e:
            print(f"[ERROR] Failed to save settings: {e}", file=sys.stderr)

    def _make_frame_with_grid(self, title: str):
        frame = Gtk.Frame(label=title)
        frame.set_margin_top(6)
        frame.set_margin_bottom(6)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.set_border_width(12)
        frame.add(inner)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.set_column_homogeneous(False)
        inner.pack_start(grid, False, False, 0)

        return frame, grid

    def _grid_add_labeled(self, grid: Gtk.Grid, row: int, label_text: str, widget):
        label = Gtk.Label(label=label_text)
        label.set_xalign(0.0)
        label.set_halign(Gtk.Align.START)
        grid.attach(label, 0, row, 1, 1)
        widget.set_hexpand(True)
        grid.attach(widget, 1, row, 1, 1)

    def build_pipeline(self, port: int, mode: str, text: str) -> str:
        safe_text = self.escape_gst_text(text)
        bg_value = "true" if self.overlay_background else "false"

        if mode == "raw":
            return f"""
                udpsrc port={port}
                    caps="video/x-h264,stream-format=byte-stream,alignment=au"
                ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=200000000 leaky=downstream
                ! h264parse config-interval=-1 disable-passthrough=true
                ! decodebin
                ! videoconvert
                ! textoverlay name=overlay
                    text="{safe_text}"
                    valignment={self.overlay_valign}
                    halignment={self.overlay_halign}
                    shaded-background={bg_value}
                    xpad={self.overlay_xpad}
                    ypad={self.overlay_ypad}
                    font-desc="Sans Bold {self.overlay_font_size}"
                ! gtksink name=videosink sync=false
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
                ! textoverlay name=overlay
                    text="{safe_text}"
                    valignment={self.overlay_valign}
                    halignment={self.overlay_halign}
                    shaded-background={bg_value}
                    xpad={self.overlay_xpad}
                    ypad={self.overlay_ypad}
                    font-desc="Sans Bold {self.overlay_font_size}"
                ! gtksink name=videosink sync=false
            """

        raise ValueError("Невідомий режим. Використовуйте raw або rtp.")

    def build_and_start_pipeline(self, text: str):
        pipeline_str = self.build_pipeline(self.port, self.mode, text)
        print("Pipeline:")
        print(pipeline_str)

        self.pipeline = Gst.parse_launch(pipeline_str)
        self.overlay = self.pipeline.get_by_name("overlay")
        self.video_sink = self.pipeline.get_by_name("videosink")

        if self.overlay is None:
            raise RuntimeError("Не вдалося знайти textoverlay")
        if self.video_sink is None:
            raise RuntimeError("Не вдалося знайти gtksink")

        video_widget = self.video_sink.props.widget
        self.video_box.pack_start(video_widget, True, True, 0)

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.on_bus_message)

        self.pipeline.set_state(Gst.State.PLAYING)

    @staticmethod
    def escape_gst_text(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    def set_overlay_text(self, text: str):
        GLib.idle_add(self.overlay.set_property, "text", text)

    def set_info_text(self, text: str):
        GLib.idle_add(self.info_label.set_text, text)

    def build_info_text(self) -> str:
        mt_host = self.mikrotik_host or "N/A"
        mt_if = self.mikrotik_interface or "N/A"

        lines = [
            f"Video: {self.mode} UDP:{self.port} | MikroTik SSH: {mt_host}:{self.ssh_port} | IF: {mt_if} | Poll: {self.poll_interval:.1f}s"
        ]

        if self.bridge is not None:
            lines.append(self.bridge.stats_text())
        elif self.bridge_remote_host:
            if self.auto_controller_enabled:
                lines.append("Controller bridge: waiting for Raspberry Pi Pico...")
            else:
                lines.append(f"Controller bridge: configured serial {self.serial_dev}, not running")

        return "\n".join(lines)

    def refresh_overlay_text_only(self):
        try:
            text = self.build_overlay_text(
                rx_power=self.last_rx_power,
                tx_power=self.last_tx_power,
                temperature=self.last_temperature,
                voltage=self.last_voltage,
                wavelength=self.last_wavelength,
                distance=self.last_distance,
                error_text=self.last_error_text,
            )
            self.set_overlay_text(text)
        except Exception:
            pass

    def restart_video_pipeline(self):
        old_text = ""
        try:
            old_text = self.overlay.get_property("text")
        except Exception:
            pass

        try:
            self.pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass

        self.pipeline = None
        self.overlay = None
        self.video_sink = None
        self.bus = None

        for child in self.video_box.get_children():
            self.video_box.remove(child)

        pipeline_str = self.build_pipeline(
            self.port,
            self.mode,
            old_text or "Connecting to MikroTik SSH...",
        )
        print("Restart pipeline:")
        print(pipeline_str)

        self.pipeline = Gst.parse_launch(pipeline_str)
        self.overlay = self.pipeline.get_by_name("overlay")
        self.video_sink = self.pipeline.get_by_name("videosink")

        if self.overlay is None:
            raise RuntimeError("Не вдалося знайти textoverlay")
        if self.video_sink is None:
            raise RuntimeError("Не вдалося знайти gtksink")

        video_widget = self.video_sink.props.widget
        self.video_box.pack_start(video_widget, True, True, 0)
        self.video_box.show_all()

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.on_bus_message)

        self.pipeline.set_state(Gst.State.PLAYING)
        self.refresh_overlay_text_only()

    def restart_bridge(self):
        if self.bridge is not None:
            try:
                self.bridge.stop()
            except Exception:
                pass
            self.bridge = None

        self.auto_controller_enabled = not bool(self.serial_dev)

        if self.bridge_remote_host:
            self.ensure_bridge_running()

    def open_ground_station_settings(self, widget):
        dialog = Gtk.Dialog(
            title="Налаштування наземної станції",
            transient_for=self.window,
            flags=0,
        )
        dialog.set_default_size(820, 700)
        dialog.set_modal(True)

        dialog.add_button("Reset", 1)
        dialog.add_button("Скасувати", Gtk.ResponseType.CANCEL)
        dialog.add_button("Застосувати", Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        content.set_border_width(0)

        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrapper.set_border_width(10)
        content.add(wrapper)

        notebook = Gtk.Notebook()
        wrapper.pack_start(notebook, True, True, 0)

        # ---------------- OSD ----------------
        osd_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        osd_page.set_border_width(10)

        frame_pos, grid_pos = self._make_frame_with_grid("Позиція")
        spin_x = Gtk.SpinButton()
        spin_x.set_range(0, 5000)
        spin_x.set_increments(1, 10)
        spin_x.set_value(self.overlay_xpad)
        self._grid_add_labeled(grid_pos, 0, "X:", spin_x)

        spin_y = Gtk.SpinButton()
        spin_y.set_range(0, 5000)
        spin_y.set_increments(1, 10)
        spin_y.set_value(self.overlay_ypad)
        self._grid_add_labeled(grid_pos, 1, "Y:", spin_y)
        osd_page.pack_start(frame_pos, False, False, 0)

        frame_style, grid_style = self._make_frame_with_grid("Стиль")
        spin_font = Gtk.SpinButton()
        spin_font.set_range(6, 72)
        spin_font.set_increments(1, 2)
        spin_font.set_value(self.overlay_font_size)
        self._grid_add_labeled(grid_style, 0, "Font size:", spin_font)

        chk_bg = Gtk.CheckButton(label="Увімкнути фон")
        chk_bg.set_active(self.overlay_background)
        grid_style.attach(chk_bg, 0, 1, 2, 1)

        combo_halign = Gtk.ComboBoxText()
        combo_halign.append("left", "Left")
        combo_halign.append("right", "Right")
        combo_halign.set_active_id(self.overlay_halign)
        self._grid_add_labeled(grid_style, 2, "Horizontal:", combo_halign)

        combo_valign = Gtk.ComboBoxText()
        combo_valign.append("top", "Top")
        combo_valign.append("bottom", "Bottom")
        combo_valign.set_active_id(self.overlay_valign)
        self._grid_add_labeled(grid_style, 3, "Vertical:", combo_valign)
        osd_page.pack_start(frame_style, False, False, 0)

        frame_visible, grid_visible = self._make_frame_with_grid("Що показувати")
        chk_show_loss = Gtk.CheckButton(label="Показувати затухання")
        chk_show_loss.set_active(self.show_loss)
        grid_visible.attach(chk_show_loss, 0, 0, 2, 1)

        chk_show_distance = Gtk.CheckButton(label="Показувати дистанцію")
        chk_show_distance.set_active(self.show_distance)
        grid_visible.attach(chk_show_distance, 0, 1, 2, 1)

        chk_show_wavelength = Gtk.CheckButton(label="Показувати довжину хвилі SFP")
        chk_show_wavelength.set_active(self.show_wavelength)
        grid_visible.attach(chk_show_wavelength, 0, 2, 2, 1)
        osd_page.pack_start(frame_visible, False, False, 0)

        osd_page.pack_start(Gtk.Label(label=""), True, True, 0)
        notebook.append_page(osd_page, Gtk.Label(label="OSD"))

        # ---------------- Bridge ----------------
        bridge_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        bridge_page.set_border_width(10)

        frame_serial, grid_serial = self._make_frame_with_grid("Serial")
        entry_serial_dev = Gtk.Entry()
        entry_serial_dev.set_text(self.serial_dev or "")
        self._grid_add_labeled(grid_serial, 0, "Serial device:", entry_serial_dev)

        spin_serial_baud = Gtk.SpinButton()
        spin_serial_baud.set_range(1200, 5000000)
        spin_serial_baud.set_increments(100, 1000)
        spin_serial_baud.set_value(self.serial_baudrate)
        self._grid_add_labeled(grid_serial, 1, "Baudrate:", spin_serial_baud)
        bridge_page.pack_start(frame_serial, False, False, 0)

        frame_udp, grid_udp = self._make_frame_with_grid("UDP")
        entry_remote_host = Gtk.Entry()
        entry_remote_host.set_text(self.bridge_remote_host)
        self._grid_add_labeled(grid_udp, 0, "Remote host:", entry_remote_host)

        spin_remote_port = Gtk.SpinButton()
        spin_remote_port.set_range(0, 65535)
        spin_remote_port.set_value(self.bridge_remote_port)
        self._grid_add_labeled(grid_udp, 1, "Remote port:", spin_remote_port)

        entry_local_bind_ip = Gtk.Entry()
        entry_local_bind_ip.set_text(self.bridge_local_bind_ip)
        self._grid_add_labeled(grid_udp, 2, "Local bind IP:", entry_local_bind_ip)

        spin_local_bind_port = Gtk.SpinButton()
        spin_local_bind_port.set_range(0, 65535)
        spin_local_bind_port.set_value(self.bridge_local_bind_port)
        self._grid_add_labeled(grid_udp, 3, "Local bind port:", spin_local_bind_port)
        bridge_page.pack_start(frame_udp, False, False, 0)

        frame_logs, grid_logs = self._make_frame_with_grid("Логи")
        chk_bridge_verbose = Gtk.CheckButton(label="Показувати логи bridge")
        chk_bridge_verbose.set_active(self.bridge_verbose)
        grid_logs.attach(chk_bridge_verbose, 0, 0, 2, 1)

        chk_bridge_hex = Gtk.CheckButton(label="Показувати hex у логах bridge")
        chk_bridge_hex.set_active(self.bridge_hex)
        grid_logs.attach(chk_bridge_hex, 0, 1, 2, 1)
        bridge_page.pack_start(frame_logs, False, False, 0)

        bridge_page.pack_start(Gtk.Label(label=""), True, True, 0)
        notebook.append_page(bridge_page, Gtk.Label(label="Міст керування"))

        # ---------------- Video ----------------
        video_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        video_page.set_border_width(10)

        frame_video_main, grid_video_main = self._make_frame_with_grid("Основне")
        spin_video_port = Gtk.SpinButton()
        spin_video_port.set_range(1, 65535)
        spin_video_port.set_value(self.port)
        self._grid_add_labeled(grid_video_main, 0, "UDP port:", spin_video_port)

        combo_video_mode = Gtk.ComboBoxText()
        combo_video_mode.append("raw", "raw")
        combo_video_mode.append("rtp", "rtp")
        combo_video_mode.set_active_id(self.mode)
        self._grid_add_labeled(grid_video_main, 1, "Mode:", combo_video_mode)
        video_page.pack_start(frame_video_main, False, False, 0)

        frame_video_behavior, grid_video_behavior = self._make_frame_with_grid("Поведінка вікна")
        chk_always_on_top = Gtk.CheckButton(label="Тримати вікно поверх інших")
        chk_always_on_top.set_active(self.always_on_top)
        grid_video_behavior.attach(chk_always_on_top, 0, 0, 2, 1)
        video_page.pack_start(frame_video_behavior, False, False, 0)

        video_page.pack_start(Gtk.Label(label=""), True, True, 0)
        notebook.append_page(video_page, Gtk.Label(label="Відеопотік"))

        # ---------------- MikroTik ----------------
        mt_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        mt_page.set_border_width(10)

        frame_mt_conn, grid_mt_conn = self._make_frame_with_grid("Підключення")
        entry_mt_host = Gtk.Entry()
        entry_mt_host.set_text(self.mikrotik_host or "")
        self._grid_add_labeled(grid_mt_conn, 0, "MikroTik host:", entry_mt_host)

        entry_mt_user = Gtk.Entry()
        entry_mt_user.set_text(self.mikrotik_user)
        self._grid_add_labeled(grid_mt_conn, 1, "MikroTik user:", entry_mt_user)

        entry_mt_password = Gtk.Entry()
        entry_mt_password.set_visibility(False)
        entry_mt_password.set_text(self.mikrotik_password)
        self._grid_add_labeled(grid_mt_conn, 2, "MikroTik password:", entry_mt_password)
        mt_page.pack_start(frame_mt_conn, False, False, 0)

        frame_mt_if, grid_mt_if = self._make_frame_with_grid("Інтерфейс")
        entry_mt_if = Gtk.Entry()
        entry_mt_if.set_text(self.mikrotik_interface or "")
        self._grid_add_labeled(grid_mt_if, 0, "SFP interface:", entry_mt_if)
        mt_page.pack_start(frame_mt_if, False, False, 0)

        mt_page.pack_start(Gtk.Label(label=""), True, True, 0)
        notebook.append_page(mt_page, Gtk.Label(label="MikroTik / SFP"))

        def apply_defaults_to_widgets():
            spin_x.set_value(0)
            spin_y.set_value(400)
            spin_font.set_value(8)
            chk_bg.set_active(False)
            combo_halign.set_active_id("right")
            combo_valign.set_active_id("top")
            chk_show_loss.set_active(True)
            chk_show_distance.set_active(True)
            chk_show_wavelength.set_active(True)

            entry_serial_dev.set_text("")
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

        dialog.show_all()

        while True:
            response = dialog.run()

            if response == 1:
                apply_defaults_to_widgets()
                continue

            if response == Gtk.ResponseType.OK:
                self.overlay_xpad = spin_x.get_value_as_int()
                self.overlay_ypad = spin_y.get_value_as_int()
                self.overlay_font_size = spin_font.get_value_as_int()
                self.overlay_background = chk_bg.get_active()
                self.overlay_halign = combo_halign.get_active_id() or "right"
                self.overlay_valign = combo_valign.get_active_id() or "top"
                self.show_loss = chk_show_loss.get_active()
                self.show_distance = chk_show_distance.get_active()
                self.show_wavelength = chk_show_wavelength.get_active()

                serial_dev_text = entry_serial_dev.get_text().strip()
                self.serial_dev = serial_dev_text if serial_dev_text else None
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
                self.restart_video_pipeline()
                self.restart_bridge()
                self.refresh_overlay_text_only()
                self.set_info_text(self.build_info_text())

            break

        dialog.destroy()

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
            print(f"[INFO] Controller connected: {serial_dev_to_use}", flush=True)
        except Exception as e:
            print(f"[WARN] Bridge start failed for {serial_dev_to_use}: {e}", file=sys.stderr)
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
                        print(f"[INFO] Controller detected: {found}", flush=True)
                    else:
                        print("[INFO] Controller disconnected", flush=True)
                    last_seen = found

                if self.auto_controller_enabled:
                    if found:
                        if self.bridge is None:
                            self.serial_dev = found
                            self.ensure_bridge_running()
                    else:
                        if self.bridge is not None:
                            print("[INFO] Stopping bridge because controller disappeared", flush=True)
                            try:
                                self.bridge.stop()
                            except Exception:
                                pass
                            self.bridge = None
                            self.serial_dev = None
                else:
                    if self.bridge is None and self.serial_dev:
                        self.ensure_bridge_running()

                self.set_info_text(self.build_info_text())

            except Exception as e:
                print(f"[WARN] controller_watch_loop: {e}", file=sys.stderr)

            time.sleep(1.0)

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
        if not self.mikrotik_host:
            self.last_error_text = "Searching MikroTik via SSH..."
            self.set_overlay_text(f"STATUS: {self.last_error_text}")
            found = auto_discover_mikrotik(
                username=self.mikrotik_user,
                password=self.mikrotik_password,
                port=self.ssh_port,
            )
            if not found:
                self.last_error_text = "MikroTik not found by SSH scan"
                self.set_overlay_text(
                    "STATUS: MikroTik not found by SSH scan\nCheck IP connectivity or set --mikrotik-host"
                )
                self.set_info_text(self.build_info_text())
                return False
            self.mikrotik_host = found

        self.mt_client = MikroTikSshClient(
            host=self.mikrotik_host,
            username=self.mikrotik_user,
            password=self.mikrotik_password,
            port=self.ssh_port,
        )

        self.mt_client.connect()
        self.identity_name = self.mt_client.get_identity() or ""

        if not self.mikrotik_interface:
            self.last_error_text = "Searching SFP interface..."
            self.set_overlay_text(f"STATUS: {self.last_error_text}")
            found_if = self.mt_client.auto_discover_sfp_interface()
            if not found_if:
                self.last_error_text = "SFP interface not found"
                self.set_overlay_text(
                    f"HOST: {self.mikrotik_host}:{self.ssh_port}\nSTATUS: SFP interface not found"
                )
                self.set_info_text(self.build_info_text())
                return False
            self.mikrotik_interface = found_if

        self.set_info_text(self.build_info_text())
        return True

    def poll_mikrotik_loop(self):
        try:
            if not self.ensure_mikrotik_ready():
                return

            while self.running:
                try:
                    rx_power, tx_power, temperature, voltage, wavelength, distance = (
                        self.mt_client.fetch_sfp_status(self.mikrotik_interface)
                    )

                    self.last_rx_power = rx_power
                    self.last_tx_power = tx_power
                    self.last_temperature = temperature
                    self.last_voltage = voltage
                    self.last_wavelength = wavelength
                    self.last_distance = distance
                    self.last_error_text = None

                    text = self.build_overlay_text(
                        rx_power=rx_power,
                        tx_power=tx_power,
                        temperature=temperature,
                        voltage=voltage,
                        wavelength=wavelength,
                        distance=distance,
                    )
                except Exception as e:
                    try:
                        if self.mt_client is not None:
                            self.mt_client.disconnect()
                            self.mt_client.connect()
                    except Exception:
                        pass

                    self.last_error_text = f"SSH ERROR: {type(e).__name__}"
                    text = self.build_overlay_text(
                        rx_power=None,
                        tx_power=None,
                        temperature=None,
                        voltage=None,
                        wavelength=None,
                        distance=None,
                        error_text=self.last_error_text,
                    )

                self.set_overlay_text(text)
                self.set_info_text(self.build_info_text())
                time.sleep(self.poll_interval)

        except Exception as e:
            self.last_error_text = f"INIT ERROR: {type(e).__name__}"
            self.set_overlay_text(f"STATUS: {self.last_error_text}")
            print(f"Init error: {e}", file=sys.stderr)

    def bridge_info_loop(self):
        while self.running:
            try:
                self.set_info_text(self.build_info_text())
            except Exception:
                pass
            time.sleep(1.0)

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

    def on_destroy(self, widget):
        self.running = False

        if self.bridge is not None:
            self.bridge.stop()

        if self.mt_client is not None:
            self.mt_client.disconnect()

        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)

        Gtk.main_quit()


def main():
    parser = argparse.ArgumentParser(
        description="UDP H.264 viewer with MikroTik SSH auto-discovery and optional UDP<->Serial bridge"
    )
    parser.add_argument("--port", type=int, default=5600, help="UDP порт відео")
    parser.add_argument(
        "--mode",
        choices=["raw", "rtp"],
        default="rtp",
        help="Тип потоку: raw H264 або RTP H264",
    )
    parser.add_argument(
        "--always-on-top",
        action="store_true",
        help="Тримати вікно поверх інших",
    )

    parser.add_argument(
        "--mikrotik-host",
        default="192.168.121.1",
        help="IP MikroTik. Можна не вказувати — буде автопошук по SSH",
    )
    parser.add_argument(
        "--mikrotik-user",
        default="admin",
        help="Логін MikroTik",
    )
    parser.add_argument(
        "--mikrotik-password",
        default="",
        help="Пароль MikroTik",
    )
    parser.add_argument(
        "--mikrotik-interface",
        default="sfp1",
        help="Можна не вказувати — буде автопошук SFP інтерфейсу",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Інтервал опитування в секундах",
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=22,
        help="Порт SSH MikroTik, зазвичай 22",
    )

    parser.add_argument(
        "--serial-dev",
        default="",
        help="Serial device для bridge, наприклад /dev/ttyACM0. Якщо не вказано — буде автопошук Pico",
    )
    parser.add_argument(
        "--serial-baudrate",
        type=int,
        default=420000,
        help="Baudrate для bridge",
    )
    parser.add_argument(
        "--bridge-remote-host",
        default="192.168.121.50",
        help="Віддалена UDP IP-адреса для bridge, наприклад 192.168.121.50",
    )
    parser.add_argument(
        "--bridge-remote-port",
        type=int,
        default=9000,
        help="Віддалений UDP порт для bridge",
    )
    parser.add_argument(
        "--bridge-local-bind-ip",
        default="0.0.0.0",
        help="Локальний bind IP для bridge",
    )
    parser.add_argument(
        "--bridge-local-bind-port",
        type=int,
        default=0,
        help="Локальний bind порт для bridge, 0 = автоматично",
    )
    parser.add_argument(
        "--bridge-verbose",
        action="store_true",
        help="Показувати логи bridge",
    )
    parser.add_argument(
        "--bridge-hex",
        action="store_true",
        help="Показувати hex у логах bridge",
    )

    args = parser.parse_args()

    UdpVideoWindow(
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
    Gtk.main()


if __name__ == "__main__":
    main()