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

import cv2
import numpy as np
import paramiko
import serial
from serial.tools import list_ports


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

            if try_mikrotik_ssh(host=ip, username=username, password=password, port=port):
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


class OpenCvUdpVideoWindow:
    def __init__(
        self,
        port: int,
        mode: str,
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
        window_title: str,
    ):
        self.port = port
        self.mode = mode
        self.mikrotik_host = mikrotik_host
        self.mikrotik_user = mikrotik_user
        self.mikrotik_password = mikrotik_password
        self.mikrotik_interface = mikrotik_interface
        self.poll_interval = max(0.5, poll_interval)
        self.ssh_port = ssh_port
        self.window_title = window_title

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
        self.latest_overlay_text = "Initializing..."
        self.overlay_lock = threading.Lock()

        self.auto_controller_enabled = not bool(self.serial_dev)
        self.bridge: Optional[UdpSerialBridge] = None
        self.mt_client: Optional[MikroTikSshClient] = None

        if self.bridge_remote_host:
            self.ensure_bridge_running()

        self.poll_thread = threading.Thread(target=self.poll_mikrotik_loop, daemon=True)
        self.poll_thread.start()

        self.bridge_info_thread = threading.Thread(target=self.bridge_info_loop, daemon=True)
        self.bridge_info_thread.start()

        self.controller_watch_thread = threading.Thread(target=self.controller_watch_loop, daemon=True)
        self.controller_watch_thread.start()

    def build_capture_url(self) -> str:
        if self.mode == "rtp":
            return (
                f"udpsrc port={self.port} "
                f"caps=application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000 ! "
                f"rtpjitterbuffer latency=30 drop-on-latency=true ! "
                f"rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! appsink sync=false drop=true"
            )

        if self.mode == "raw":
            return (
                f"udpsrc port={self.port} "
                f"caps=video/x-h264,stream-format=byte-stream,alignment=au ! "
                f"queue max-size-buffers=0 max-size-bytes=0 max-size-time=200000000 leaky=downstream ! "
                f"h264parse config-interval=-1 disable-passthrough=true ! decodebin ! videoconvert ! "
                f"appsink sync=false drop=true"
            )

        raise ValueError("Невідомий режим. Використовуйте raw або rtp.")

    def set_overlay_text(self, text: str):
        with self.overlay_lock:
            self.latest_overlay_text = text

    def get_overlay_text(self) -> str:
        with self.overlay_lock:
            return self.latest_overlay_text

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

            except Exception as e:
                print(f"[WARN] controller_watch_loop: {e}", file=sys.stderr)

            time.sleep(1.0)

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
                return False
            self.mikrotik_interface = found_if

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

                self.set_overlay_text(text)
                time.sleep(self.poll_interval)

        except Exception as e:
            self.set_overlay_text(f"STATUS: INIT ERROR: {type(e).__name__}")
            print(f"Init error: {e}", file=sys.stderr)

    def bridge_info_loop(self):
        while self.running:
            time.sleep(1.0)

    @staticmethod
    def draw_multiline_text(
        frame: np.ndarray,
        text: str,
        x: int,
        y: int,
        line_height: int = 22,
        font_scale: float = 0.6,
        thickness: int = 1,
    ):
        if not text:
            return

        lines = text.splitlines()
        font = cv2.FONT_HERSHEY_SIMPLEX

        max_width = 0
        for line in lines:
            (w, h), _ = cv2.getTextSize(line, font, font_scale, thickness)
            max_width = max(max_width, w)

        box_height = line_height * len(lines) + 10
        box_width = max_width + 16

        overlay = frame.copy()
        cv2.rectangle(overlay, (x - 6, y - 18), (x - 6 + box_width, y - 18 + box_height), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.45, frame, 0.55, 0, frame)

        yy = y
        for line in lines:
            cv2.putText(frame, line, (x, yy), font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)
            yy += line_height

    def run(self):
        capture_url = self.build_capture_url()
        print("Capture pipeline:")
        print(capture_url)

        cap = cv2.VideoCapture(capture_url, cv2.CAP_GSTREAMER)
        if not cap.isOpened():
            raise RuntimeError("Не вдалося відкрити UDP video stream через OpenCV/GStreamer")

        cv2.namedWindow(self.window_title, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.window_title, 1100, 700)

        try:
            while self.running:
                ret, frame = cap.read()
                if not ret or frame is None:
                    key = cv2.waitKey(10) & 0xFF
                    if key in (27, ord('q')):
                        break
                    continue

                overlay_text = self.get_overlay_text()
                info_text = self.build_info_text()

                self.draw_multiline_text(frame, overlay_text, 12, 28)
                self.draw_multiline_text(frame, info_text, 12, frame.shape[0] - 60, line_height=20, font_scale=0.5)

                cv2.imshow(self.window_title, frame)
                key = cv2.waitKey(1) & 0xFF

                if key in (27, ord('q')):
                    break
                elif key == ord('c'):
                    self.manual_prefix = ""
                    print("[INFO] Manual prefix cleared")

        finally:
            self.running = False
            cap.release()
            cv2.destroyAllWindows()
            if self.bridge is not None:
                self.bridge.stop()
            if self.mt_client is not None:
                self.mt_client.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description="OpenCV UDP H.264 viewer with MikroTik SSH auto-discovery and optional UDP<->Serial bridge"
    )
    parser.add_argument("--port", type=int, default=5600, help="UDP порт відео")
    parser.add_argument(
        "--mode",
        choices=["raw", "rtp"],
        default="rtp",
        help="Тип потоку: raw H264 або RTP H264",
    )
    parser.add_argument(
        "--window-title",
        default="UDP Video Viewer + MikroTik SFP Monitor",
        help="Назва вікна",
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

    app = OpenCvUdpVideoWindow(
        port=args.port,
        mode=args.mode,
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
        window_title=args.window_title,
    )
    app.run()


if __name__ == "__main__":
    main()
