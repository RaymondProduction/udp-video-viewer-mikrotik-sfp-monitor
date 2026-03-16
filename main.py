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
from typing import Optional, List, Tuple

import gi
import paramiko
import serial
from serial.tools import list_ports

gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")

from gi.repository import Gtk, Gst, GLib

Gst.init(None)


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

    # Формат типу 20KM у part number
    match = re.search(r"(?<!\d)(1|2|3|5|10|20|40|60|80|100|120)\s*km(?!\w)", text, re.IGNORECASE)
    if match:
        return f"{match.group(1)}km"

    # Витягуємо типові дальності навіть якщо вони йдуть біля інших символів
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
        self.manual_prefix = ""
        self.identity_name = ""

        self.auto_controller_enabled = not bool(self.serial_dev)

        # OSD settings
        self.overlay_xpad = 220
        self.overlay_ypad = 110
        self.overlay_font_size = 8
        self.overlay_background = False
        self.overlay_halign = "right"
        self.overlay_valign = "bottom"

        self.window = Gtk.Window(title="UDP Video Viewer + MikroTik SFP Monitor")
        self.window.set_default_size(1100, 700)
        self.window.set_keep_above(always_on_top)
        self.window.connect("destroy", self.on_destroy)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_border_width(8)
        self.window.add(root)

        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        root.pack_start(top_bar, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Необов'язковий префікс")
        self.entry.connect("activate", self.apply_prefix)
        top_bar.pack_start(self.entry, True, True, 0)

        btn_apply = Gtk.Button(label="Застосувати")
        btn_apply.connect("clicked", self.apply_prefix)
        top_bar.pack_start(btn_apply, False, False, 0)

        btn_clear = Gtk.Button(label="Очистити")
        btn_clear.connect("clicked", self.clear_prefix)
        top_bar.pack_start(btn_clear, False, False, 0)

        btn_osd = Gtk.Button(label="OSD...")
        btn_osd.connect("clicked", self.open_osd_settings)
        top_bar.pack_start(btn_osd, False, False, 0)

        self.info_label = Gtk.Label(label="Video init...")
        self.info_label.set_xalign(0.0)
        root.pack_start(self.info_label, False, False, 0)

        self.video_box = Gtk.Box()
        root.pack_start(self.video_box, True, True, 0)

        pipeline_str = self.build_pipeline(port, mode, "Connecting to MikroTik SSH...")
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

        self.bridge: Optional[UdpSerialBridge] = None

        if self.bridge_remote_host:
            self.ensure_bridge_running()

        self.window.show_all()
        self.pipeline.set_state(Gst.State.PLAYING)

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

    @staticmethod
    def escape_gst_text(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    def apply_prefix(self, widget):
        self.manual_prefix = self.entry.get_text().strip()
        self.refresh_overlay_text_only()

    def clear_prefix(self, widget):
        self.entry.set_text("")
        self.manual_prefix = ""
        self.refresh_overlay_text_only()

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
                rx_power=None,
                tx_power=None,
                temperature=None,
                voltage=None,
                wavelength=None,
                distance=None,
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

        for child in self.video_box.get_children():
            self.video_box.remove(child)

        self.video_box.pack_start(video_widget, True, True, 0)
        self.video_box.show_all()

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.on_bus_message)

        self.pipeline.set_state(Gst.State.PLAYING)

    def open_osd_settings(self, widget):
        dialog = Gtk.Dialog(
            title="Налаштування OSD",
            transient_for=self.window,
            flags=0,
        )
        dialog.add_button("Скасувати", Gtk.ResponseType.CANCEL)
        dialog.add_button("Застосувати", Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(10)
        content.add(box)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        box.pack_start(grid, True, True, 0)

        row = 0

        lbl_x = Gtk.Label(label="X:")
        lbl_x.set_xalign(0.0)
        grid.attach(lbl_x, 0, row, 1, 1)

        spin_x = Gtk.SpinButton()
        spin_x.set_range(0, 5000)
        spin_x.set_increments(1, 10)
        spin_x.set_value(self.overlay_xpad)
        grid.attach(spin_x, 1, row, 1, 1)
        row += 1

        lbl_y = Gtk.Label(label="Y:")
        lbl_y.set_xalign(0.0)
        grid.attach(lbl_y, 0, row, 1, 1)

        spin_y = Gtk.SpinButton()
        spin_y.set_range(0, 5000)
        spin_y.set_increments(1, 10)
        spin_y.set_value(self.overlay_ypad)
        grid.attach(spin_y, 1, row, 1, 1)
        row += 1

        lbl_font = Gtk.Label(label="Font size:")
        lbl_font.set_xalign(0.0)
        grid.attach(lbl_font, 0, row, 1, 1)

        spin_font = Gtk.SpinButton()
        spin_font.set_range(6, 72)
        spin_font.set_increments(1, 2)
        spin_font.set_value(self.overlay_font_size)
        grid.attach(spin_font, 1, row, 1, 1)
        row += 1

        chk_bg = Gtk.CheckButton(label="Background")
        chk_bg.set_active(self.overlay_background)
        grid.attach(chk_bg, 0, row, 2, 1)
        row += 1

        lbl_halign = Gtk.Label(label="Horizontal:")
        lbl_halign.set_xalign(0.0)
        grid.attach(lbl_halign, 0, row, 1, 1)

        combo_halign = Gtk.ComboBoxText()
        combo_halign.append("left", "Left")
        combo_halign.append("right", "Right")
        combo_halign.set_active_id(self.overlay_halign)
        grid.attach(combo_halign, 1, row, 1, 1)
        row += 1

        lbl_valign = Gtk.Label(label="Vertical:")
        lbl_valign.set_xalign(0.0)
        grid.attach(lbl_valign, 0, row, 1, 1)

        combo_valign = Gtk.ComboBoxText()
        combo_valign.append("top", "Top")
        combo_valign.append("bottom", "Bottom")
        combo_valign.set_active_id(self.overlay_valign)
        grid.attach(combo_valign, 1, row, 1, 1)

        dialog.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            self.overlay_xpad = spin_x.get_value_as_int()
            self.overlay_ypad = spin_y.get_value_as_int()
            self.overlay_font_size = spin_font.get_value_as_int()
            self.overlay_background = chk_bg.get_active()
            self.overlay_halign = combo_halign.get_active_id() or "right"
            self.overlay_valign = combo_valign.get_active_id() or "bottom"
            self.restart_video_pipeline()

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

        if self.manual_prefix:
            lines.append(self.manual_prefix)

        # if self.identity_name:
        #     lines.append(f"MT: {self.identity_name}")

        # if self.mikrotik_host:
        #     lines.append(f"HOST: {self.mikrotik_host}:{self.ssh_port}")

        # if self.mikrotik_interface:
        #     lines.append(f"IF: {self.mikrotik_interface}")

        if error_text:
            lines.append(f"STATUS: {error_text}")
            return "\n".join(lines)

        rx_val = parse_dbm_value(rx_power)
        tx_val = parse_dbm_value(tx_power)

        loss_text = "N/A"
        if tx_val is not None and rx_val is not None:
            loss_text = f"{(tx_val - rx_val):.2f} dB"

        lines.append(f"LOSS: {loss_text}")

        wl_dist = []
        if wavelength:
            wl_dist.append(f"WL: {wavelength}")
        if distance:
            wl_dist.append(f"DIST: {distance}")

        if wl_dist:
            lines.append(" | ".join(wl_dist))

        extras = []
        # if temperature:
        #     extras.append(f"TEMP: {temperature}")
        # if voltage:
        #     extras.append(f"VCC: {voltage}")

        if extras:
            lines.append(" | ".join(extras))

        return "\n".join(lines)

    def ensure_mikrotik_ready(self) -> bool:
        if not self.mikrotik_host:
            self.set_overlay_text("STATUS: Searching MikroTik via SSH...")
            found = auto_discover_mikrotik(
                username=self.mikrotik_user,
                password=self.mikrotik_password,
                port=self.ssh_port,
            )
            if not found:
                self.set_overlay_text(
                    "STATUS: MikroTik not found by SSH scan\n"
                    "Check IP connectivity or set --mikrotik-host"
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
            self.set_overlay_text("STATUS: Searching SFP interface...")
            found_if = self.mt_client.auto_discover_sfp_interface()
            if not found_if:
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
                self.set_info_text(self.build_info_text())
                time.sleep(self.poll_interval)

        except Exception as e:
            self.set_overlay_text(f"STATUS: INIT ERROR: {type(e).__name__}")
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
        default="",
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
        default="",
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
        default="",
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