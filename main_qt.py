#!/usr/bin/env python3
import argparse
import binascii
import ipaddress
import json
import socket
import subprocess
import sys
import threading
import time
from typing import Optional, List

import gi
import numpy as np
import paramiko
import serial
from serial.tools import list_ports

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

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


def find_controller_serial_device() -> Optional[str]:
    for p in list_ports.comports():
        if p.manufacturer == "Raspberry Pi" and p.product == "Pico":
            return p.device
    return None


class UiSignals(QObject):
    log_line = Signal(str)
    info_text = Signal(str)
    frame_ready = Signal(QImage)


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

    def fetch_sfp_status(self, interface_name: str):
        cmd = f'/interface ethernet monitor "{interface_name}" once'
        out = self.run_command(cmd)

        rx_power = None
        tx_power = None
        temperature = None
        voltage = None

        for line in out.splitlines():
            line = line.strip()

            if "sfp-rx-power:" in line:
                rx_power = line.split(":", 1)[1].strip()
            elif "sfp-tx-power:" in line:
                tx_power = line.split(":", 1)[1].strip()
            elif "sfp-temperature:" in line:
                temperature = line.split(":", 1)[1].strip()
            elif "sfp-supply-voltage:" in line:
                voltage = line.split(":", 1)[1].strip()
            elif "sfp-voltage:" in line:
                voltage = line.split(":", 1)[1].strip()

        return rx_power, tx_power, temperature, voltage


def try_mikrotik_ssh(host: str, username: str, password: str, port: int) -> bool:
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
        log_fn=None,
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
        self.log_fn = log_fn or print

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
            self.log_fn(f"[BRIDGE] {text}")

    def info(self, text: str):
        self.log_fn(f"[INFO] {text}")

    def err(self, text: str):
        self.log_fn(f"[ERROR] {text}")

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

        self.running = True

        self.t_udp_to_serial = threading.Thread(target=self.udp_to_serial_loop, daemon=True)
        self.t_serial_to_udp = threading.Thread(target=self.serial_to_udp_loop, daemon=True)

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
                    self.log(f"UDP -> SERIAL | {len(data)} bytes | hex={self.short_hex(data)}")
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
                    self.log(f"SERIAL -> UDP | {len(data)} bytes | hex={self.short_hex(data)}")
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


class GstVideoController:
    def __init__(self, port: int, mode: str, frame_callback, log_fn):
        self.port = port
        self.mode = mode
        self.frame_callback = frame_callback
        self.log_fn = log_fn

        self.pipeline: Optional[Gst.Element] = None
        self.overlay: Optional[Gst.Element] = None
        self.appsink: Optional[Gst.Element] = None
        self.bus: Optional[Gst.Bus] = None

    @staticmethod
    def escape_gst_text(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    def build_pipeline(self, port: int, mode: str, text: str) -> str:
        safe_text = self.escape_gst_text(text)

        if mode == "raw":
            return f"""
                udpsrc port={port}
                    caps="video/x-h264,stream-format=byte-stream,alignment=au"
                ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=200000000 leaky=downstream
                ! h264parse config-interval=-1 disable-passthrough=true
                ! decodebin
                ! videoconvert
                ! video/x-raw,format=RGB
                ! textoverlay name=overlay
                    text="{safe_text}"
                    valignment=top
                    halignment=left
                    shaded-background=true
                    font-desc="Sans 14"
                ! videoconvert
                ! video/x-raw,format=RGB
                ! appsink name=videosink emit-signals=true max-buffers=1 drop=true sync=false
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
                ! video/x-raw,format=RGB
                ! textoverlay name=overlay
                    text="{safe_text}"
                    valignment=top
                    halignment=left
                    shaded-background=true
                    font-desc="Sans 14"
                ! videoconvert
                ! video/x-raw,format=RGB
                ! appsink name=videosink emit-signals=true max-buffers=1 drop=true sync=false
            """

        raise ValueError("Невідомий режим. Використовуйте raw або rtp.")

    def start(self):
        pipeline_str = self.build_pipeline(self.port, self.mode, "Connecting to MikroTik SSH...")
        self.log_fn("Pipeline:")
        self.log_fn(pipeline_str)

        self.pipeline = Gst.parse_launch(pipeline_str)
        self.overlay = self.pipeline.get_by_name("overlay")
        self.appsink = self.pipeline.get_by_name("videosink")

        if self.overlay is None:
            raise RuntimeError("Не вдалося знайти textoverlay")
        if self.appsink is None:
            raise RuntimeError("Не вдалося знайти appsink")

        self.appsink.connect("new-sample", self.on_new_sample)

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.on_bus_message)

        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        self.overlay = None
        self.appsink = None
        self.bus = None

    def restart(self, port: int, mode: str):
        self.stop()
        self.port = port
        self.mode = mode
        self.start()

    def set_overlay_text(self, text: str):
        if self.overlay is not None:
            self.overlay.set_property("text", text)

    def on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR

        buffer = sample.get_buffer()
        caps = sample.get_caps()
        structure = caps.get_structure(0)

        width = structure.get_value("width")
        height = structure.get_value("height")

        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.ERROR

        try:
            arr = np.frombuffer(map_info.data, dtype=np.uint8)
            expected = width * height * 3
            if arr.size < expected:
                return Gst.FlowReturn.OK

            arr = arr[:expected].reshape((height, width, 3))
            image = QImage(arr.data, width, height, width * 3, QImage.Format_RGB888).copy()
            self.frame_callback(image)
        finally:
            buffer.unmap(map_info)

        return Gst.FlowReturn.OK

    def on_bus_message(self, bus, message):
        msg_type = message.type

        if msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            self.log_fn(f"GStreamer ERROR: {err}")
            if debug:
                self.log_fn(f"DEBUG: {debug}")

        elif msg_type == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            self.log_fn(f"GStreamer WARNING: {warn}")
            if debug:
                self.log_fn(f"DEBUG: {debug}")

        elif msg_type == Gst.MessageType.EOS:
            self.log_fn("Кінець потоку")


class MainWindow(QMainWindow):
    def __init__(self, args):
        super().__init__()

        self.setWindowTitle("UDP Video Viewer + MikroTik SFP Monitor (Qt + GStreamer)")
        self.resize(1280, 900)
        if args.always_on_top:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self.signals = UiSignals()
        self.signals.log_line.connect(self.append_log)
        self.signals.info_text.connect(self.set_info_text)
        self.signals.frame_ready.connect(self.update_video_frame)

        self.port = args.port
        self.mode = args.mode
        self.mikrotik_host = args.mikrotik_host or None
        self.mikrotik_user = args.mikrotik_user
        self.mikrotik_password = args.mikrotik_password
        self.mikrotik_interface = args.mikrotik_interface or None
        self.poll_interval = max(0.5, args.poll_interval)
        self.ssh_port = args.ssh_port

        self.serial_dev = args.serial_dev or None
        self.serial_baudrate = args.serial_baudrate
        self.bridge_remote_host = args.bridge_remote_host
        self.bridge_remote_port = args.bridge_remote_port
        self.bridge_local_bind_ip = args.bridge_local_bind_ip
        self.bridge_local_bind_port = args.bridge_local_bind_port
        self.bridge_verbose = args.bridge_verbose
        self.bridge_hex = args.bridge_hex

        self.auto_controller_enabled = not bool(self.serial_dev)

        self.running = True
        self.manual_prefix = ""
        self.identity_name = ""

        self.bridge: Optional[UdpSerialBridge] = None
        self.mt_client: Optional[MikroTikSshClient] = None

        self.build_ui()

        self.video = GstVideoController(
            port=self.port,
            mode=self.mode,
            frame_callback=lambda img: self.signals.frame_ready.emit(img),
            log_fn=lambda text: self.signals.log_line.emit(text),
        )
        self.video.start()

        if self.bridge_remote_host:
            self.ensure_bridge_running()

        self.poll_thread = threading.Thread(target=self.poll_mikrotik_loop, daemon=True)
        self.poll_thread.start()

        self.bridge_info_thread = threading.Thread(target=self.bridge_info_loop, daemon=True)
        self.bridge_info_thread.start()

        self.controller_watch_thread = threading.Thread(target=self.controller_watch_loop, daemon=True)
        self.controller_watch_thread.start()

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.refresh_overlay_and_info)
        self.ui_timer.start(700)

        self.refresh_overlay_and_info()

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        controls = QGridLayout()
        root.addLayout(controls)

        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText("Необов'язковий префікс")
        self.prefix_edit.returnPressed.connect(self.apply_prefix)

        self.apply_btn = QPushButton("Застосувати")
        self.apply_btn.clicked.connect(self.apply_prefix)

        self.clear_btn = QPushButton("Очистити")
        self.clear_btn.clicked.connect(self.clear_prefix)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["raw", "rtp"])
        self.mode_combo.setCurrentText(self.mode)
        self.mode_combo.currentTextChanged.connect(self.mode_changed)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self.port)
        self.port_spin.valueChanged.connect(self.port_changed)

        self.poll_spin = QDoubleSpinBox()
        self.poll_spin.setRange(0.5, 60.0)
        self.poll_spin.setSingleStep(0.5)
        self.poll_spin.setValue(self.poll_interval)
        self.poll_spin.valueChanged.connect(self.poll_changed)

        self.bridge_verbose_check = QCheckBox("Bridge verbose")
        self.bridge_verbose_check.setChecked(self.bridge_verbose)

        self.bridge_hex_check = QCheckBox("Bridge hex")
        self.bridge_hex_check.setChecked(self.bridge_hex)

        controls.addWidget(QLabel("Prefix:"), 0, 0)
        controls.addWidget(self.prefix_edit, 0, 1, 1, 3)
        controls.addWidget(self.apply_btn, 0, 4)
        controls.addWidget(self.clear_btn, 0, 5)

        controls.addWidget(QLabel("Mode:"), 1, 0)
        controls.addWidget(self.mode_combo, 1, 1)
        controls.addWidget(QLabel("Port:"), 1, 2)
        controls.addWidget(self.port_spin, 1, 3)
        controls.addWidget(QLabel("Poll:"), 1, 4)
        controls.addWidget(self.poll_spin, 1, 5)

        controls.addWidget(self.bridge_verbose_check, 2, 0, 1, 2)
        controls.addWidget(self.bridge_hex_check, 2, 2, 1, 2)

        self.info_label = QLabel("Init...")
        self.info_label.setWordWrap(True)
        root.addWidget(self.info_label)

        self.video_label = QLabel("No video")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumHeight(480)
        self.video_label.setStyleSheet("background-color: black; color: #7fff7f;")
        root.addWidget(self.video_label, 1)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMinimumHeight(220)
        root.addWidget(self.log_edit)

    def append_log(self, text: str):
        self.log_edit.append(text)

    def set_info_text(self, text: str):
        self.info_label.setText(text)

    def update_video_frame(self, image: QImage):
        pix = QPixmap.fromImage(image)
        if not self.video_label.size().isEmpty():
            pix = pix.scaled(
                self.video_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        self.video_label.setPixmap(pix)

    def mode_changed(self, value: str):
        self.mode = value
        self.append_log(f"[UI] Mode changed to {value}")
        self.video.restart(self.port, self.mode)

    def port_changed(self, value: int):
        self.port = value
        self.append_log(f"[UI] Port changed to {value}")
        self.video.restart(self.port, self.mode)

    def poll_changed(self, value: float):
        self.poll_interval = max(0.5, value)

    def apply_prefix(self):
        self.manual_prefix = self.prefix_edit.text().strip()
        self.refresh_overlay_and_info()

    def clear_prefix(self):
        self.prefix_edit.setText("")
        self.manual_prefix = ""
        self.refresh_overlay_and_info()

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

    def build_overlay_text(
        self,
        rx_power: Optional[str],
        tx_power: Optional[str],
        temperature: Optional[str],
        voltage: Optional[str],
        error_text: Optional[str] = None,
    ) -> str:
        lines = []

        if self.manual_prefix:
            lines.append(self.manual_prefix)

        if self.identity_name:
            lines.append(f"MT: {self.identity_name}")

        if self.mikrotik_host:
            lines.append(f"HOST: {self.mikrotik_host}:{self.ssh_port}")

        if self.mikrotik_interface:
            lines.append(f"IF: {self.mikrotik_interface}")

        if error_text:
            lines.append(f"STATUS: {error_text}")
            return "\n".join(lines)

        lines.append(f"RX: {rx_power or 'N/A'}")
        lines.append(f"TX: {tx_power or 'N/A'}")

        extras = []
        if temperature:
            extras.append(f"TEMP: {temperature}")
        if voltage:
            extras.append(f"VCC: {voltage}")

        if extras:
            lines.append(" | ".join(extras))

        return "\n".join(lines)

    def refresh_overlay_and_info(self):
        self.set_info_text(self.build_info_text())
        self.video.set_overlay_text(self.build_overlay_text(None, None, None, None))

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
                verbose=self.bridge_verbose_check.isChecked(),
                hex_dump=self.bridge_hex_check.isChecked(),
                log_fn=lambda text: self.signals.log_line.emit(text),
            )
            self.bridge.start()
            self.serial_dev = serial_dev_to_use
            self.append_log(f"[INFO] Controller connected: {serial_dev_to_use}")
        except Exception as e:
            self.append_log(f"[WARN] Bridge start failed for {serial_dev_to_use}: {e}")
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
                        self.signals.log_line.emit(f"[INFO] Controller detected: {found}")
                    else:
                        self.signals.log_line.emit("[INFO] Controller disconnected")
                    last_seen = found

                if self.auto_controller_enabled:
                    if found:
                        if self.bridge is None:
                            self.serial_dev = found
                            self.ensure_bridge_running()
                    else:
                        if self.bridge is not None:
                            self.signals.log_line.emit("[INFO] Stopping bridge because controller disappeared")
                            try:
                                self.bridge.stop()
                            except Exception:
                                pass
                            self.bridge = None
                            self.serial_dev = None
                else:
                    if self.bridge is None and self.serial_dev:
                        self.ensure_bridge_running()

                self.signals.info_text.emit(self.build_info_text())

            except Exception as e:
                self.signals.log_line.emit(f"[WARN] controller_watch_loop: {e}")

            time.sleep(1.0)

    def ensure_mikrotik_ready(self) -> bool:
        if not self.mikrotik_host:
            self.video.set_overlay_text("STATUS: Searching MikroTik via SSH...")
            found = auto_discover_mikrotik(
                username=self.mikrotik_user,
                password=self.mikrotik_password,
                port=self.ssh_port,
            )
            if not found:
                self.video.set_overlay_text(
                    "STATUS: MikroTik not found by SSH scan\nCheck IP connectivity or set --mikrotik-host"
                )
                self.signals.info_text.emit(self.build_info_text())
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
            self.video.set_overlay_text("STATUS: Searching SFP interface...")
            found_if = self.mt_client.auto_discover_sfp_interface()
            if not found_if:
                self.video.set_overlay_text(
                    f"HOST: {self.mikrotik_host}:{self.ssh_port}\nSTATUS: SFP interface not found"
                )
                self.signals.info_text.emit(self.build_info_text())
                return False
            self.mikrotik_interface = found_if

        self.signals.info_text.emit(self.build_info_text())
        return True

    def poll_mikrotik_loop(self):
        try:
            if not self.ensure_mikrotik_ready():
                return

            while self.running:
                try:
                    rx_power, tx_power, temperature, voltage = self.mt_client.fetch_sfp_status(
                        self.mikrotik_interface
                    )
                    text = self.build_overlay_text(
                        rx_power=rx_power,
                        tx_power=tx_power,
                        temperature=temperature,
                        voltage=voltage,
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
                        error_text=f"SSH ERROR: {type(e).__name__}",
                    )

                self.video.set_overlay_text(text)
                self.signals.info_text.emit(self.build_info_text())
                time.sleep(self.poll_interval)

        except Exception as e:
            self.video.set_overlay_text(f"STATUS: INIT ERROR: {type(e).__name__}")
            self.signals.log_line.emit(f"Init error: {e}")

    def bridge_info_loop(self):
        while self.running:
            try:
                self.signals.info_text.emit(self.build_info_text())
            except Exception:
                pass
            time.sleep(1.0)

    def closeEvent(self, event):
        self.running = False

        if self.bridge is not None:
            self.bridge.stop()

        if self.mt_client is not None:
            self.mt_client.disconnect()

        self.video.stop()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(
        description="UDP H.264 viewer with MikroTik SSH auto-discovery and optional UDP<->Serial bridge (Qt + GStreamer)"
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

    parser.add_argument("--mikrotik-host", default="", help="IP MikroTik. Можна не вказувати — буде автопошук по SSH")
    parser.add_argument("--mikrotik-user", default="admin", help="Логін MikroTik")
    parser.add_argument("--mikrotik-password", default="", help="Пароль MikroTik")
    parser.add_argument("--mikrotik-interface", default="", help="Можна не вказувати — буде автопошук SFP інтерфейсу")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Інтервал опитування в секундах")
    parser.add_argument("--ssh-port", type=int, default=22, help="Порт SSH MikroTik, зазвичай 22")

    parser.add_argument(
        "--serial-dev",
        default="",
        help="Serial device для bridge, наприклад /dev/ttyACM0. Якщо не вказано — буде автопошук Pico",
    )
    parser.add_argument("--serial-baudrate", type=int, default=420000, help="Baudrate для bridge")
    parser.add_argument("--bridge-remote-host", default="", help="Віддалена UDP IP-адреса для bridge, наприклад 192.168.121.50")
    parser.add_argument("--bridge-remote-port", type=int, default=9000, help="Віддалений UDP порт для bridge")
    parser.add_argument("--bridge-local-bind-ip", default="0.0.0.0", help="Локальний bind IP для bridge")
    parser.add_argument("--bridge-local-bind-port", type=int, default=0, help="Локальний bind порт для bridge, 0 = автоматично")
    parser.add_argument("--bridge-verbose", action="store_true", help="Показувати логи bridge")
    parser.add_argument("--bridge-hex", action="store_true", help="Показувати hex у логах bridge")

    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = MainWindow(args)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()