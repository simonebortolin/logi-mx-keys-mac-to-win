"""KeySwitcher - Windows tray app to swap Win <-> Alt keys for Mac keyboards."""

import json
import os
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont
import pystray

from keyboard_hook import KeyboardHook

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", ""), "KeySwitcher")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "bt_device_name": "MX KEYS S MAC",
    # USB receivers the keyboard may be paired with (e.g. Logitech Unifying
    # "VID_046D&PID_C52B" or Bolt "VID_046D&PID_C548"). Swap turns on while one is plugged in.
    "usb_receiver_ids": [],
    "auto_detect": True,
    "poll_interval_seconds": 5,
}


def load_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        # Merge with defaults for any missing keys
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    # First run - write defaults
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def _get_bt_address(device_name: str) -> str | None:
    """Resolve a Bluetooth device's address from its friendly name."""
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"(Get-PnpDevice -FriendlyName '{device_name}' -ErrorAction SilentlyContinue"
                f" | Get-PnpDeviceProperty -KeyName 'DEVPKEY_Bluetooth_DeviceAddress').Data",
            ],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        addr = result.stdout.strip()
        return addr.upper() if addr else None
    except Exception:
        return None


# Cache the BT address so we only resolve it once
_bt_address_cache: dict[str, str | None] = {}


def is_bt_device_connected(device_name: str) -> bool:
    """Check if a BT keyboard is actually connected by checking its HID child device."""
    bt_addr = _bt_address_cache.get(device_name)
    if not bt_addr:
        # Not cached on failure: at logon the BT stack may not be ready yet
        bt_addr = _get_bt_address(device_name)
        if not bt_addr:
            return False
        _bt_address_cache[device_name] = bt_addr

    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"(Get-PnpDevice -Class Keyboard -InstanceId '*{bt_addr}*' -ErrorAction SilentlyContinue).Status",
            ],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.stdout.strip() == "OK"
    except Exception:
        return False


def is_usb_receiver_connected(receiver_ids: list[str]) -> bool:
    """Check if a keyboard behind one of the given USB receivers is present and working."""
    if not receiver_ids:
        return False
    patterns = ",".join(f"'HID\\{rid}*'" for rid in receiver_ids)
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"(Get-PnpDevice -Class Keyboard -PresentOnly -InstanceId {patterns}"
                f" -ErrorAction SilentlyContinue).Status",
            ],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "OK" in result.stdout.split()
    except Exception:
        return False


def is_keyboard_connected(config: dict) -> bool:
    """True if the Mac keyboard is reachable via Bluetooth or a configured USB receiver."""
    return (is_usb_receiver_connected(config.get("usb_receiver_ids", []))
            or is_bt_device_connected(config["bt_device_name"]))


def create_icon_image(active: bool, auto: bool = False) -> Image.Image:
    """Create a tray icon. Green=ON, Gray=OFF, Blue=AUTO."""
    size = 64
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if active:
        bg_color = (76, 175, 80, 255)    # Green
    elif auto:
        bg_color = (33, 150, 243, 255)   # Blue (auto mode, waiting)
    else:
        bg_color = (158, 158, 158, 255)  # Gray
    draw.ellipse([2, 2, size - 2, size - 2], fill=bg_color)

    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except (OSError, IOError):
        font = ImageFont.load_default()

    text = "KS"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2
    y = (size - th) // 2 - 2
    draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)

    return img


# --- Startup management ---

# Startup is handled by the "KeySwitcher" Scheduled Task (see install-service.ps1).

TASK_NAME = "KeySwitcher"
_INSTALL_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install-service.ps1")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW


def is_startup_enabled():
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME],
        capture_output=True, creationflags=_NO_WINDOW,
    )
    return result.returncode == 0


def _run_install_script(*extra_args):
    """Run install-service.ps1 (it asks for admin rights via UAC)."""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
         "-File", _INSTALL_SCRIPT, "-FromApp", *extra_args],
        creationflags=_NO_WINDOW,
    )


def enable_startup():
    _run_install_script()


def disable_startup():
    _run_install_script("-Uninstall")


class KeySwitcherApp:
    def __init__(self):
        self.hook = KeyboardHook()
        self.hook_thread = None
        self.tray_icon = None
        self._active = False
        self._auto_mode = True
        self._bt_poll_thread = None
        self._running = True
        self.config = load_config()

    def _start_hook_thread(self):
        def run():
            self.hook.install()
            self.hook.run_message_loop()
            self.hook.uninstall()

        self.hook_thread = threading.Thread(target=run, daemon=True)
        self.hook_thread.start()

    def _set_active(self, active: bool):
        """Set the swap state and update the tray icon."""
        self._active = active
        self.hook.enabled = active
        self._update_icon()

    def _update_icon(self):
        if self.tray_icon:
            self.tray_icon.icon = create_icon_image(self._active, self._auto_mode)
            status = "ON" if self._active else "OFF"
            mode = " (Auto)" if self._auto_mode else ""
            self.tray_icon.title = f"KeySwitcher - {status}{mode}"

    def toggle(self, icon=None, item=None):
        """Manual toggle — disables auto mode."""
        if self._auto_mode:
            self._auto_mode = False
        self._set_active(not self._active)

    def toggle_auto(self, icon=None, item=None):
        """Toggle Bluetooth auto-detection mode."""
        self._auto_mode = not self._auto_mode
        if self._auto_mode:
            # Immediately check current state
            connected = is_keyboard_connected(self.config)
            self._set_active(connected)
        self._update_icon()

    def toggle_startup(self, icon=None, item=None):
        if is_startup_enabled():
            disable_startup()
        else:
            enable_startup()

    def _bt_poll_loop(self):
        """Background thread that polls Bluetooth device status."""
        while self._running:
            if self._auto_mode:
                connected = is_keyboard_connected(self.config)
                if connected != self._active:
                    self._set_active(connected)
            time.sleep(self.config.get("poll_interval_seconds", 5))

    def quit_app(self, icon=None, item=None):
        self._running = False
        self.hook.enabled = False
        self.hook.stop_message_loop()
        if self.tray_icon:
            self.tray_icon.stop()

    def run(self):
        self._start_hook_thread()

        # Start Bluetooth polling thread
        self._bt_poll_thread = threading.Thread(target=self._bt_poll_loop, daemon=True)
        self._bt_poll_thread.start()

        # Check keyboard state immediately on startup
        connected = is_keyboard_connected(self.config)
        self._set_active(connected)

        device_name = self.config["bt_device_name"]

        menu = pystray.Menu(
            pystray.MenuItem(
                lambda item: f"Swap Keys: {'ON' if self._active else 'OFF'}",
                self.toggle,
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: f"Auto ({device_name}): {'ON' if self._auto_mode else 'OFF'}",
                self.toggle_auto,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: f"Start with Windows: {'ON' if is_startup_enabled() else 'OFF'}",
                self.toggle_startup,
            ),
            pystray.MenuItem("Quit", self.quit_app),
        )

        self.tray_icon = pystray.Icon(
            name="KeySwitcher",
            icon=create_icon_image(self._active, self._auto_mode),
            title="KeySwitcher - OFF",
            menu=menu,
        )

        self.tray_icon.run()


def main():
    app = KeySwitcherApp()
    app.run()


if __name__ == "__main__":
    main()
