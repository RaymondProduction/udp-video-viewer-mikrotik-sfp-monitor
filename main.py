#!/usr/bin/env python3
import argparse
import ipaddress
import json
import socket
import subprocess
import sys
import threading
import time
from typing import Optional, List

import gi
import paramiko

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

                # Щоб не сканувати величезні мережі
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
    ):
        self.port = port
        self.mode = mode
        self.mikrotik_host = mikrotik_host
        self.mikrotik_user = mikrotik_user
        self.mikrotik_password = mikrotik_password
        self.mikrotik_interface = mikrotik_interface
        self.poll_interval = max(0.5, poll_interval)
        self.ssh_port = ssh_port

        self.running = True
        self.manual_prefix = ""
        self.identity_name = ""

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

        self.window.show_all()
        self.pipeline.set_state(Gst.State.PLAYING)

        self.mt_client: Optional[MikroTikSshClient] = None
        self.poll_thread = threading.Thread(target=self.poll_mikrotik_loop, daemon=True)
        self.poll_thread.start()

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
                ! textoverlay name=overlay
                    text="{safe_text}"
                    valignment=top
                    halignment=left
                    shaded-background=true
                    font-desc="Sans 14"
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
                    valignment=top
                    halignment=left
                    shaded-background=true
                    font-desc="Sans 14"
                ! gtksink name=videosink sync=false
            """

        raise ValueError("Невідомий режим. Використовуйте raw або rtp.")

    @staticmethod
    def escape_gst_text(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    def apply_prefix(self, widget):
        self.manual_prefix = self.entry.get_text().strip()

    def clear_prefix(self, widget):
        self.entry.set_text("")
        self.manual_prefix = ""

    def set_overlay_text(self, text: str):
        GLib.idle_add(self.overlay.set_property, "text", text)

    def set_info_text(self, text: str):
        GLib.idle_add(self.info_label.set_text, text)

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

        self.set_info_text(
            f"Video: {self.mode} UDP:{self.port} | MikroTik SSH: {self.mikrotik_host}:{self.ssh_port} | IF: {self.mikrotik_interface} | Poll: {self.poll_interval:.1f}s"
        )
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
        if self.mt_client is not None:
            self.mt_client.disconnect()
        self.pipeline.set_state(Gst.State.NULL)
        Gtk.main_quit()


def main():
    parser = argparse.ArgumentParser(
        description="UDP H.264 viewer with MikroTik SSH auto-discovery"
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
    )
    Gtk.main()


if __name__ == "__main__":
    main()