#!/usr/bin/env python3
import argparse
import binascii
import ipaddress
import socket
import sys
import threading
import time
from typing import Optional, List

import cv2
import numpy as np
import paramiko
import psutil
import serial
from serial.tools import list_ports

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QComboBox,
)


def get_local_ipv4_networks() -> List[ipaddress.IPv4Network]:
    result = []
    seen = set()

    for iface_name, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue

            ip = addr.address
            netmask = addr.netmask

            if not ip or not netmask:
                continue

            try:
                ip_obj = ipaddress.ip_address(ip)
                if ip_obj.is_loopback:
                    continue

                network = ipaddress.ip_network(f"{ip}/{netmask}", strict=False)

                if network.prefixlen < 24:
                    network = ipaddress.ip_network(f"{ip}/24", strict=False)

                net_str = str(network)
                if net_str not in seen:
                    seen.add(net_str)
                    result.append(network)
            except Exception:
                continue

    return result


def tcp_connectable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def find_controller_serial_device() -> Optional[str]:
    for p in list_ports.comports():
        text = " ".join(
            str(x)
            for x in [p.device, p.description, p.manufacturer, p.product, p.hwid]
            if x
        ).lower()

        if "raspberry pi pico" in text or " pico " in f" {text} " or text.endswith(" pico"):
            return p.device

        if p.manufacturer == "Raspberry Pi" and p.product == "Pico":
            return p.device

    return None


class LogEmitter(QObject):
    line = Signal(str)
    info_text = Signal(str)
    overlay_text = Signal(str)
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


def auto_discover_mikrotik(username: str, password: str, port: int, logger: LogEmitter) -> Optional[str]:
    networks = get_local_ipv4_networks()
    logger.line.emit(f"Локальні мережі для сканування: {[str(n) for n in networks]}")

    for network in networks:
        logger.line.emit(f"Сканую мережу {network} ...")
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
                logger.line.emit(f"Знайдено MikroTik через SSH: {ip}:{port}")
                return ip

    return None


class UdpSerialBridge:
    def __init__(
        self,
        remote_host: str,
        remote_port: int,
        serial_dev: str,
        baudrate: int,
        logger: LogEmitter,
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
        self.logger = logger

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
            self.logger.line.emit(f"[BRIDGE] {text}")

    def info(self, text: str):
        self.logger.line.emit(f"[INFO] {text}")

    def err(self, text: str):
        self.logger.line.emit(f"[ERROR] {text}")

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


class VideoWorker:
    def __init__(self, port: int, mode: str, logger: LogEmitter):
        self.port = port
        self.mode = mode
        self.logger = logger
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.overlay_text = "Video starting..."
        self.lock = threading.Lock()

    def set_overlay_text(self, text: str):
        with self.lock:
            self.overlay_text = text

    def get_overlay_text(self) -> str:
        with self.lock:
            return self.overlay_text

    def build_source(self) -> str:
        if self.mode == "raw":
            return f"udp://0.0.0.0:{self.port}"
        if self.mode == "rtp":
            return f"udp://0.0.0.0:{self.port}"
        raise ValueError("Невідомий режим")

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self.run, daemon=True, name="video_worker")
        self.thread.start()

    def stop(self):
        self.running = False

    def draw_overlay(self, frame: np.ndarray, text: str) -> np.ndarray:
        lines = text.splitlines()
        if not lines:
            return frame

        x = 10
        y = 25
        line_h = 22
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.55
        thickness = 1

        max_w = 0
        for line in lines:
            (w, h), _ = cv2.getTextSize(line, font, scale, thickness)
            max_w = max(max_w, w)

        box_h = len(lines) * line_h + 12
        box_w = max_w + 20

        overlay = frame.copy()
        cv2.rectangle(overlay, (5, 5), (5 + box_w, 5 + box_h), (0, 0, 0), -1)
        frame = cv2.addWeighted(overlay, 0.45, frame, 0.55, 0)

        for i, line in enumerate(lines):
            yy = y + i * line_h
            cv2.putText(frame, line, (x, yy), font, scale, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(frame, line, (x, yy), font, scale, (0, 255, 0), 1, cv2.LINE_AA)

        return frame

    def run(self):
        src = self.build_source()
        self.logger.line.emit(f"[VIDEO] Opening source: {src}")

        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)

        if not cap.isOpened():
            self.logger.line.emit("[VIDEO] Не вдалося відкрити відеопотік через OpenCV/FFmpeg")
            self.logger.line.emit("[VIDEO] Спробуйте інший режим потоку або іншу OpenCV build")
            self.running = False
            return

        while self.running:
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.02)
                continue

            text = self.get_overlay_text()
            frame = self.draw_overlay(frame, text)

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            bytes_per_line = ch * w
            image = QImage(rgb.data, w, h, bytes_per_line, QImage.Format_RGB888).copy()
            self.logger.frame_ready.emit(image)

        cap.release()


class MainWindow(QMainWindow):
    def __init__(self, args):
        super().__init__()

        self.setWindowTitle("UDP Video Viewer + MikroTik SFP Monitor (Qt)")
        self.resize(1280, 900)
        if args.always_on_top:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self.logger = LogEmitter()
        self.logger.line.connect(self.append_log)
        self.logger.info_text.connect(self.set_info_text)
        self.logger.overlay_text.connect(self.set_overlay_text)
        self.logger.frame_ready.connect(self.update_video_frame)

        self.running = True

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

        self.identity_name = ""
        self.manual_prefix = ""
        self.bridge: Optional[UdpSerialBridge] = None
        self.mt_client: Optional[MikroTikSshClient] = None

        self.video_worker = VideoWorker(self.port, self.mode, self.logger)

        self.build_ui()

        self.video_worker.start()

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
        self.info_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
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

    def set_overlay_text(self, text: str):
        self.video_worker.set_overlay_text(text)

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
        self.restart_video()

    def port_changed(self, value: int):
        self.port = value
        self.append_log(f"[UI] Port changed to {value}")
        self.restart_video()

    def poll_changed(self, value: float):
        self.poll_interval = max(0.5, value)

    def apply_prefix(self):
        self.manual_prefix = self.prefix_edit.text().strip()
        self.refresh_overlay_and_info()

    def clear_prefix(self):
        self.prefix_edit.setText("")
        self.manual_prefix = ""
        self.refresh_overlay_and_info()

    def restart_video(self):
        try:
            self.video_worker.stop()
            time.sleep(0.2)
        except Exception:
            pass
        self.video_worker = VideoWorker(self.port, self.mode, self.logger)
        self.video_worker.start()

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
        self.info_label.setText(self.build_info_text())

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
                logger=self.logger,
                local_bind_ip=self.bridge_local_bind_ip,
                local_bind_port=self.bridge_local_bind_port,
                verbose=self.bridge_verbose_check.isChecked(),
                hex_dump=self.bridge_hex_check.isChecked(),
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
                        self.logger.line.emit(f"[INFO] Controller detected: {found}")
                    else:
                        self.logger.line.emit("[INFO] Controller disconnected")
                    last_seen = found

                if self.auto_controller_enabled:
                    if found:
                        if self.bridge is None:
                            self.serial_dev = found
                            self.ensure_bridge_running()
                    else:
                        if self.bridge is not None:
                            self.logger.line.emit("[INFO] Stopping bridge because controller disappeared")
                            try:
                                self.bridge.stop()
                            except Exception:
                                pass
                            self.bridge = None
                            self.serial_dev = None
                else:
                    if self.bridge is None and self.serial_dev:
                        self.ensure_bridge_running()

                self.logger.info_text.emit(self.build_info_text())

            except Exception as e:
                self.logger.line.emit(f"[WARN] controller_watch_loop: {e}")

            time.sleep(1.0)

    def ensure_mikrotik_ready(self) -> bool:
        if not self.mikrotik_host:
            self.logger.overlay_text.emit("STATUS: Searching MikroTik via SSH...")
            found = auto_discover_mikrotik(
                username=self.mikrotik_user,
                password=self.mikrotik_password,
                port=self.ssh_port,
                logger=self.logger,
            )
            if not found:
                self.logger.overlay_text.emit(
                    "STATUS: MikroTik not found by SSH scan\nCheck IP connectivity or set host"
                )
                self.logger.info_text.emit(self.build_info_text())
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
            self.logger.overlay_text.emit("STATUS: Searching SFP interface...")
            found_if = self.mt_client.auto_discover_sfp_interface()
            if not found_if:
                self.logger.overlay_text.emit(
                    f"HOST: {self.mikrotik_host}:{self.ssh_port}\nSTATUS: SFP interface not found"
                )
                self.logger.info_text.emit(self.build_info_text())
                return False
            self.mikrotik_interface = found_if

        self.logger.info_text.emit(self.build_info_text())
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

                self.logger.overlay_text.emit(text)
                self.logger.info_text.emit(self.build_info_text())
                time.sleep(self.poll_interval)

        except Exception as e:
            self.logger.overlay_text.emit(f"STATUS: INIT ERROR: {type(e).__name__}")
            self.logger.line.emit(f"Init error: {e}")

    def bridge_info_loop(self):
        while self.running:
            try:
                self.logger.info_text.emit(self.build_info_text())
            except Exception:
                pass
            time.sleep(1.0)

    def closeEvent(self, event):
        self.running = False

        try:
            self.video_worker.stop()
        except Exception:
            pass

        if self.bridge is not None:
            self.bridge.stop()

        if self.mt_client is not None:
            self.mt_client.disconnect()

        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(
        description="UDP H.264 viewer with MikroTik SSH auto-discovery and optional UDP<->Serial bridge (Qt/Windows)"
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

    parser.add_argument("--mikrotik-host", default="", help="IP MikroTik")
    parser.add_argument("--mikrotik-user", default="admin", help="Логін MikroTik")
    parser.add_argument("--mikrotik-password", default="", help="Пароль MikroTik")
    parser.add_argument("--mikrotik-interface", default="", help="SFP інтерфейс")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Інтервал опитування")
    parser.add_argument("--ssh-port", type=int, default=22, help="SSH порт")

    parser.add_argument(
        "--serial-dev",
        default="",
        help="Serial device для bridge, наприклад COM5. Якщо не вказано — автопошук Pico",
    )
    parser.add_argument("--serial-baudrate", type=int, default=420000, help="Baudrate для bridge")
    parser.add_argument("--bridge-remote-host", default="", help="Віддалена UDP IP-адреса")
    parser.add_argument("--bridge-remote-port", type=int, default=9000, help="Віддалений UDP порт")
    parser.add_argument("--bridge-local-bind-ip", default="0.0.0.0", help="Локальний bind IP")
    parser.add_argument("--bridge-local-bind-port", type=int, default=0, help="Локальний bind порт")
    parser.add_argument("--bridge-verbose", action="store_true", help="Показувати логи bridge")
    parser.add_argument("--bridge-hex", action="store_true", help="Показувати hex у логах bridge")

    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = MainWindow(args)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()