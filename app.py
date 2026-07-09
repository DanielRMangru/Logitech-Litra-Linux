import os
import sys
import json
import logging
import math
import argparse
import usb.core
import usb.util

# Set up logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")

# Logitech Litra USB details
VENDOR_ID = 0x046d
LITRA_PRODUCTS = {
    0xc900: {"name": "Logitech Litra Glow", "endpoint": 0x02, "buffer_length": 64},
    0xc901: {"name": "Logitech Litra Beam", "endpoint": 0x01, "buffer_length": 32},
    0xca03: {"name": "Logitech Litra Beam LX", "endpoint": 0x01, "buffer_length": 32}
}

TIMEOUT_MS = 3000
MIN_BRIGHTNESS_BYTE = 0x14  # 20
MAX_BRIGHTNESS_BYTE = 0xfa  # 250

STATE_FILE = "litra_state.json"

def load_state():
    """Load persisted labels and control states."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error loading state file: {e}")
    return {}

def save_state(state):
    """Save labels and control states to file."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving state file: {e}")

def get_device_unique_id(dev):
    """Attempt to get serial number, fallback to bus/address combination."""
    serial = None
    try:
        serial = dev.serial_number
    except Exception:
        try:
            serial = usb.util.get_string(dev, dev.iSerialNumber)
        except Exception:
            pass
    
    if serial:
        return serial.strip()
    return f"USB-{dev.bus}-{dev.address}"

def find_usb_devices():
    """Scans the USB bus for matching Logitech Litra devices."""
    found = []
    for pid, info in LITRA_PRODUCTS.items():
        try:
            devs = usb.core.find(idVendor=VENDOR_ID, idProduct=pid, find_all=True)
            if devs:
                for dev in devs:
                    found.append((dev, pid, info))
        except Exception as e:
            logging.error(f"Error finding devices for PID 0x{pid:04x}: {e}")
    return found

def get_devices_status():
    """Scans USB and returns details including capabilities, state, and permissions."""
    usb_devs = find_usb_devices()
    state = load_state()
    devices_info = []

    for dev, pid, info in usb_devs:
        device_id = get_device_unique_id(dev)
        
        # Check if we have permission to open the device
        permission_denied = False
        try:
            _ = dev.serial_number
        except Exception as e:
            err_str = str(e).lower()
            if "permission" in err_str or "access" in err_str or "langid" in err_str:
                permission_denied = True

        # Get or initialize state for this device
        dev_state = state.get(device_id, {})
        if not dev_state:
            suffix = device_id[-4:] if len(device_id) > 4 else device_id
            dev_state = {
                "label": f"{info['name']} ({suffix})",
                "on": False,
                "brightness": 50,
                "temperature": 4000
            }
            state[device_id] = dev_state
            save_state(state)

        devices_info.append({
            "id": device_id,
            "model": info["name"],
            "pid": pid,
            "bus": dev.bus,
            "address": dev.address,
            "label": dev_state.get("label"),
            "state": {
                "on": dev_state.get("on", False),
                "brightness": dev_state.get("brightness", 50),
                "temperature": dev_state.get("temperature", 4000)
            },
            "permission_denied": permission_denied,
            "device_handle": dev  # Keep reference for write operations in memory
        })

    return devices_info

def write_to_litra(dev, pid, command_bytes):
    """Write command bytes to Litra USB device, detaching kernel driver if active."""
    info = LITRA_PRODUCTS[pid]
    endpoint = info["endpoint"]
    buffer_length = info["buffer_length"]
    
    reattach = False
    try:
        if dev.is_kernel_driver_active(0):
            reattach = True
            dev.detach_kernel_driver(0)
    except NotImplementedError:
        pass
    except Exception as e:
        logging.warning(f"Failed to check/detach kernel driver: {e}")

    try:
        dev.set_configuration()
        usb.util.claim_interface(dev, 0)
        
        # Pad package to 20 bytes if shorter
        padded_command = list(command_bytes)
        if len(padded_command) < 20:
            padded_command += [0x00] * (20 - len(padded_command))
            
        dev.write(endpoint, padded_command, TIMEOUT_MS)
        
        # Read confirmation payload
        _ = dev.read(endpoint, buffer_length, TIMEOUT_MS)
        
    finally:
        try:
            usb.util.release_interface(dev, 0)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass
        if reattach:
            try:
                dev.attach_kernel_driver(0)
            except Exception:
                pass

def commit_device_control(device_id, on=None, brightness=None, temperature=None):
    """Sends control values to the USB device and updates configuration state."""
    # Find matching connected device
    devices = get_devices_status()
    target = None
    for d in devices:
        if d["id"] == device_id:
            target = d
            break
            
    if not target:
        raise ValueError(f"Device with ID {device_id} is not connected.")
        
    if target["permission_denied"]:
        raise PermissionError(f"Permission denied to communicate with Litra device {device_id}. Please setup UDEV rules.")

    state = load_state()
    dev_state = state.get(device_id, {})
    if not dev_state:
        dev_state = target["state"]

    # Update states if provided
    if on is not None:
        dev_state["on"] = bool(on)
    if brightness is not None:
        dev_state["brightness"] = max(0, min(100, int(brightness)))
    if temperature is not None:
        dev_state["temperature"] = max(2700, min(6500, int(temperature)))

    # We must write commands to the device.
    # Note: USB configurations require us to re-query the usb device handle from the OS 
    # to avoid stale descriptor errors.
    usb_devices = find_usb_devices()
    target_dev = None
    for dev, pid, info in usb_devices:
        if get_device_unique_id(dev) == device_id:
            target_dev = dev
            break
            
    if not target_dev:
        raise ConnectionError(f"Device {device_id} lost connection.")

    # 1. Send Power Command
    power_status = 0x01 if dev_state.get("on", False) else 0x00
    power_cmd = [0x11, 0xff, 0x04, 0x1c, power_status]
    write_to_litra(target_dev, target["pid"], power_cmd)

    # Re-query
    usb_devices = find_usb_devices()
    for dev, pid, info in usb_devices:
        if get_device_unique_id(dev) == device_id:
            target_dev = dev
            break

    # 2. Send Brightness Command
    level = dev_state.get("brightness", 50)
    adjusted_level = math.floor(MIN_BRIGHTNESS_BYTE + ((level / 100.0) * (MAX_BRIGHTNESS_BYTE - MIN_BRIGHTNESS_BYTE)))
    brightness_cmd = [0x11, 0xff, 0x04, 0x4c, 0x00, adjusted_level]
    write_to_litra(target_dev, target["pid"], brightness_cmd)

    # Re-query
    usb_devices = find_usb_devices()
    for dev, pid, info in usb_devices:
        if get_device_unique_id(dev) == device_id:
            target_dev = dev
            break

    # 3. Send Temperature Command
    temp = dev_state.get("temperature", 4000)
    temp_bytes = temp.to_bytes(2, "big")
    temp_cmd = [0x11, 0xff, 0x04, 0x9c, temp_bytes[0], temp_bytes[1]]
    write_to_litra(target_dev, target["pid"], temp_cmd)

    # Save final state back to file
    state[device_id] = dev_state
    save_state(state)
    return dev_state

# --- CLI Implementation ---

def run_cli(args):
    """Execute command-line parameters."""
    if args.scan:
        devices = get_devices_status()
        if not devices:
            print("No Logitech Litra devices detected.")
            return 0
        print(f"Found {len(devices)} device(s):")
        for d in devices:
            perm_str = "Writable" if not d["permission_denied"] else "Permission Denied (Setup UDEV rules)"
            state_str = "ON" if d["state"]["on"] else "OFF"
            print(f"\n- Label: {d['label']}")
            print(f"  ID:    {d['id']}")
            print(f"  Model: {d['model']} (PID: 0x{d['pid']:04x}, Bus: {d['bus']}, Address: {d['address']})")
            print(f"  State: {state_str} (Brightness: {d['state']['brightness']}%, Temperature: {d['state']['temperature']}K)")
            print(f"  Access: {perm_str}")
        return 0

    # Controls logic
    devices = get_devices_status()
    if not devices:
        print("Error: No connected Litra devices found.")
        return 1

    target_id = args.device
    if not target_id:
        if len(devices) == 1:
            target_id = devices[0]["id"]
        else:
            print("Error: Multiple devices found. Please select one using --device <id>:")
            for d in devices:
                print(f"  - ID: {d['id']} ({d['label']})")
            return 1

    # Find the target device info
    target_device = None
    for d in devices:
        if d["id"] == target_id:
            target_device = d
            break
            
    if not target_device:
        print(f"Error: Device with ID '{target_id}' is not connected.")
        return 1

    # Apply changes
    state_updated = False
    
    # 1. Handle custom labeling
    if args.label is not None:
        state = load_state()
        if target_id not in state:
            state[target_id] = target_device["state"]
        state[target_id]["label"] = args.label.strip()
        save_state(state)
        print(f"Updated label for '{target_id}' to '{args.label.strip()}'")
        state_updated = True

    # 2. Handle hardware control states
    has_control_action = (args.on or args.off or args.brightness is not None or args.temperature is not None)
    if has_control_action:
        if target_device["permission_denied"]:
            print(f"Error: Permission denied accessing device '{target_id}'.")
            print("Please configure UDEV rules by copying '82-litra.rules' to '/etc/udev/rules.d/' and reloading.")
            return 1
            
        on_val = None
        if args.on:
            on_val = True
        elif args.off:
            on_val = False
            
        try:
            new_state = commit_device_control(
                target_id, 
                on=on_val, 
                brightness=args.brightness, 
                temperature=args.temperature
            )
            state_str = "ON" if new_state["on"] else "OFF"
            print(f"Applied control values to '{target_device['label']}':")
            print(f"  State: {state_str}")
            print(f"  Brightness: {new_state['brightness']}%")
            print(f"  Temperature: {new_state['temperature']}K")
            state_updated = True
        except Exception as e:
            print(f"Error communicating with device: {e}")
            return 1

    if not state_updated:
        # If no control options were provided but --device was specified
        print(f"Device ID: {target_device['id']}")
        print(f"  Label: {target_device['label']}")
        print(f"  Model: {target_device['model']}")
        state_str = "ON" if target_device["state"]["on"] else "OFF"
        print(f"  State: {state_str} (Brightness: {target_device['state']['brightness']}%, Temperature: {target_device['state']['temperature']}K)")

    return 0

# --- Desktop GUI (Tkinter) Implementation ---

def run_gui():
    """Initializes and runs the native Tkinter graphical interface."""
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError:
        print("Error: The 'tkinter' module is not found.")
        print("To run the graphical user interface on Ubuntu/Debian/Linux Mint:")
        print("  sudo apt install python3-tk")
        print("\nAlternatively, use the command-line options. Run with --help for details.")
        return 1

    class LitraAppWindow(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Litra Control Center")
            self.geometry("450x720")
            self.configure(bg="#181a1f")
            
            # Dark theme styles
            self.style = ttk.Style()
            self.style.theme_use("clam")
            
            # Configure frame panel styling
            self.style.configure(".", background="#181a1f", foreground="#ffffff")
            self.style.configure("TLabel", background="#181a1f", foreground="#ffffff", font=("Inter", 10))
            self.style.configure("Title.TLabel", font=("Inter", 14, "bold"), foreground="#ffffff")
            self.style.configure("Subtitle.TLabel", font=("Inter", 9), foreground="#9aa0a6")
            
            self.style.configure("Card.TFrame", background="#21252b", relief="flat")
            self.style.configure("TButton", background="#2c313c", foreground="#ffffff", borderwidth=0, font=("Inter", 9, "bold"))
            self.style.map("TButton",
                background=[("active", "#3e4451"), ("disabled", "#1e222b")],
                foreground=[("active", "#ffffff"), ("disabled", "#5c6370")]
            )
            
            # Configure scale (slider) styling
            self.style.configure("Horizontal.TScale", 
                                 troughcolor="#15181f", 
                                 background="#3e4451", 
                                 lightcolor="#3e4451", 
                                 darkcolor="#21252b",
                                 bordercolor="#2d3139", 
                                 sliderthickness=14)
            
            self.debounce_timers = {} # Keep track of active debounce timers
            self.create_widgets()
            self.scan_and_render()

        def create_widgets(self):
            # Top Header Bar
            header_frame = ttk.Frame(self, padding=15)
            header_frame.pack(fill="x", side="top")
            
            title_lbl = ttk.Label(header_frame, text="Litra Control Center", style="Title.TLabel")
            title_lbl.pack(side="left")
            
            self.btn_rescan = ttk.Button(header_frame, text="Rescan USB", command=self.scan_and_render)
            self.btn_rescan.pack(side="right")
            
            # Bottom Footer (Packed first at bottom to span full window width and align left)
            footer_lbl = ttk.Label(self, text="Logitech Litra Controller • Running locally on Linux 24.04", style="Subtitle.TLabel", anchor="w", padding=15)
            footer_lbl.pack(fill="x", side="bottom")
            
            # Scrollbar (Packed on the right side)
            self.scrollbar = ttk.Scrollbar(self, orient="vertical")
            self.scrollbar.pack(side="right", fill="y")
            
            # Scrollable main Canvas (Fills the remaining center space)
            self.canvas = tk.Canvas(self, bg="#181a1f", highlightthickness=0)
            self.canvas.pack(side="left", fill="both", expand=True)
            
            self.scrollable_frame = ttk.Frame(self.canvas, padding=10)
            self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
            
            # Bind scrollbar events
            self.scrollbar.configure(command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=self.scrollbar.set)
            
            # Update scrollregion when the inner frame size changes
            self.scrollable_frame.bind(
                "<Configure>",
                lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            )
            
            # Dynamically update the width of the inner frame to match the canvas viewport width
            def on_canvas_configure(event):
                # Ensure the configure event came from the canvas itself and not bubbling children
                if event.widget == self.canvas:
                    # Subtract 10px margin (scrollbar is now outside the canvas viewport)
                    width = max(200, event.width - 10)
                    self.canvas.itemconfig(self.canvas_window, width=width)
                
            self.canvas.bind("<Configure>", on_canvas_configure)

        def scan_and_render(self):
            # Clear previous widgets
            for widget in self.scrollable_frame.winfo_children():
                widget.destroy()
                
            self.btn_rescan.configure(state="disabled")
            self.update()
            
            devices = get_devices_status()
            self.btn_rescan.configure(state="normal")
            
            if not devices:
                self.render_no_devices()
                return

            has_perm_denied = False
            for d in devices:
                if d["permission_denied"]:
                    has_perm_denied = True
                self.render_device_card(d)
                
            if has_perm_denied:
                self.render_permission_warning()

        def render_no_devices(self):
            card = ttk.Frame(self.scrollable_frame, style="Card.TFrame", padding=20)
            card.pack(fill="x", pady=10)
            
            lbl = ttk.Label(card, text="No Litra Devices Detected", font=("Inter", 12, "bold"), foreground="#ffa600", background="#21252b")
            lbl.pack(anchor="w", pady=(0, 10))
            
            desc = ("We searched the USB controllers but couldn't find any connected Litra lamps.\n\n"
                    "If a device is connected, this is usually a permissions issue. "
                    "Please install the UDEV rules by executing:\n\n"
                    "  sudo cp 82-litra.rules /etc/udev/rules.d/\n"
                    "  sudo udevadm control --reload-rules && sudo udevadm trigger\n\n"
                    "Then disconnect and reconnect your light, and click Rescan USB.")
            
            desc_lbl = tk.Text(card, bg="#1e222b", fg="#9aa0a6", font=("Inter", 9), relief="flat", wrap="word", height=12)
            desc_lbl.insert("1.0", desc)
            desc_lbl.configure(state="disabled")
            desc_lbl.pack(fill="x")

        def render_permission_warning(self):
            card = ttk.Frame(self.scrollable_frame, style="Card.TFrame", padding=15)
            card.pack(fill="x", pady=10)
            
            lbl = ttk.Label(card, text="Access Permission Denied", font=("Inter", 11, "bold"), foreground="#ff3333", background="#21252b")
            lbl.pack(anchor="w", pady=(0, 5))
            
            txt = ("Litra devices were detected but the current user lacks write permissions. "
                   "Copy the rules to enable system-wide non-root access:\n\n"
                   "  sudo cp 82-litra.rules /etc/udev/rules.d/\n"
                   "  sudo udevadm control --reload-rules && sudo udevadm trigger")
            
            desc_lbl = tk.Text(card, bg="#1e222b", fg="#9aa0a6", font=("Inter", 9), relief="flat", wrap="word", height=6)
            desc_lbl.insert("1.0", txt)
            desc_lbl.configure(state="disabled")
            desc_lbl.pack(fill="x")

        def render_device_card(self, device):
            # Define accent color based on device model (matching web version color scheme)
            accent_color = "#ffae00" if "glow" in device["model"].lower() else "#00d2ff"
            active_accent = accent_color if device["state"]["on"] else "#2d3139"
            
            card = tk.Frame(self.scrollable_frame, bg="#21252b", bd=0, highlightthickness=1, highlightbackground=active_accent, highlightcolor=active_accent)
            card.pack(fill="x", pady=8, ipady=5)
            
            # Top accent color bar (visual color indicator matching web version)
            accent_bar = tk.Frame(card, height=3, bg=active_accent)
            accent_bar.pack(fill="x", side="top")
            
            # Inner padding frame
            pad_frame = tk.Frame(card, bg="#21252b")
            pad_frame.pack(fill="both", expand=True, padx=12, pady=10)
            
            # Header containing Label and Power Switch
            header_f = tk.Frame(pad_frame, bg="#21252b")
            header_f.pack(fill="x", pady=(0, 10))
            
            # Double-click editable Label text
            lbl_var = tk.StringVar(value=device["label"])
            lbl_widget = tk.Label(header_f, textvariable=lbl_var, font=("Inter", 11, "bold"), fg="#ffffff", bg="#21252b", cursor="hand2")
            lbl_widget.pack(side="left", anchor="w")
            
            # Handle edit renaming
            def start_edit(event):
                # Replace with Entry
                lbl_widget.pack_forget()
                entry = tk.Entry(header_f, font=("Inter", 11, "bold"), fg="#ffffff", bg="#121214", insertbackground="white", bd=1, relief="solid")
                entry.insert(0, lbl_var.get())
                entry.pack(side="left", fill="x", expand=True)
                entry.focus()
                entry.select_range(0, tk.END)
                
                def finish_edit(save=True):
                    new_val = entry.get().strip()
                    entry.destroy()
                    lbl_widget.pack(side="left", anchor="w")
                    if save and new_val and new_val != lbl_var.get():
                        lbl_var.set(new_val)
                        # Save state
                        state = load_state()
                        if device["id"] not in state:
                            state[device["id"]] = device["state"]
                        state[device["id"]]["label"] = new_val
                        save_state(state)
                        
                entry.bind("<Return>", lambda e: finish_edit(True))
                entry.bind("<Escape>", lambda e: finish_edit(False))
                entry.bind("<FocusOut>", lambda e: finish_edit(True))
                
            lbl_widget.bind("<Button-1>", start_edit)
            
            # State/Power Toggle Button
            power_on = device["state"]["on"]
            
            btn_color = "#00e676" if power_on else "#5f6368"
            btn_text = "Active" if power_on else "Standby"
            
            btn_power = tk.Button(
                header_f, 
                text=btn_text, 
                bg=btn_color, 
                fg="#ffffff", 
                font=("Inter", 9, "bold"),
                relief="flat", 
                bd=0, 
                padx=10, 
                pady=2,
                state="disabled" if device["permission_denied"] else "normal"
            )
            btn_power.pack(side="right")
            
            # Brightness Control Sliders
            b_frame = tk.Frame(pad_frame, bg="#21252b")
            b_frame.pack(fill="x", pady=6)
            
            tk.Label(b_frame, text="Brightness", fg="#9aa0a6", bg="#21252b", font=("Inter", 9)).pack(side="left")
            b_val_lbl = tk.Label(b_frame, text=f"{device['state']['brightness']}%", fg="#ffffff", bg="#21252b", font=("Inter", 9, "bold"))
            b_val_lbl.pack(side="right")
            
            slider_brightness = ttk.Scale(
                pad_frame, 
                from_=0, 
                to=100, 
                orient="horizontal", 
                style="Horizontal.TScale",
                state="disabled" if device["permission_denied"] else "normal"
            )
            slider_brightness.set(device["state"]["brightness"])
            slider_brightness.pack(fill="x", pady=(0, 2))
            
            # Brightness Visual Gradient Bar
            b_grad = tk.Canvas(pad_frame, height=4, bg="#21252b", highlightthickness=0)
            b_grad.pack(fill="x", pady=(0, 10))
            
            def draw_b_gradient(event):
                canvas = event.widget
                canvas.delete("all")
                width = event.width
                height = event.height
                for x in range(width):
                    ratio = x / width
                    r = int(21 - (21 * ratio))
                    g = int(24 + (210 - 24) * ratio)
                    b = int(31 + (255 - 31) * ratio)
                    canvas.create_line(x, 0, x, height, fill=f"#{r:02x}{g:02x}{b:02x}")
            b_grad.bind("<Configure>", draw_b_gradient)
            
            # Temperature Control Sliders
            t_frame = tk.Frame(pad_frame, bg="#21252b")
            t_frame.pack(fill="x", pady=6)
            
            tk.Label(t_frame, text="Color Temperature", fg="#9aa0a6", bg="#21252b", font=("Inter", 9)).pack(side="left")
            t_val_lbl = tk.Label(t_frame, text=f"{device['state']['temperature']}K", fg="#ffffff", bg="#21252b", font=("Inter", 9, "bold"))
            t_val_lbl.pack(side="right")
            
            slider_temp = ttk.Scale(
                pad_frame, 
                from_=2700, 
                to=6500, 
                orient="horizontal", 
                style="Horizontal.TScale",
                state="disabled" if device["permission_denied"] else "normal"
            )
            slider_temp.set(device["state"]["temperature"])
            slider_temp.pack(fill="x", pady=(0, 2))
            
            # Color Temperature Visual Gradient Bar (Warm Amber -> Cool Blue)
            t_grad = tk.Canvas(pad_frame, height=4, bg="#21252b", highlightthickness=0)
            t_grad.pack(fill="x", pady=(0, 8))
            
            def draw_t_gradient(event):
                canvas = event.widget
                canvas.delete("all")
                width = event.width
                height = event.height
                for x in range(width):
                    ratio = x / width
                    if ratio < 0.5:
                        r = 255
                        g = int(166 + (255 - 166) * (ratio * 2))
                        b = int(255 * (ratio * 2))
                    else:
                        r = int(255 - (255 - 80) * ((ratio - 0.5) * 2))
                        g = int(255 - (255 - 180) * ((ratio - 0.5) * 2))
                        b = 255
                    canvas.create_line(x, 0, x, height, fill=f"#{r:02x}{g:02x}{b:02x}")
            t_grad.bind("<Configure>", draw_t_gradient)
            
            # Interactive Demarcation Presets Row
            presets_f = tk.Frame(pad_frame, bg="#21252b")
            presets_f.pack(fill="x", pady=(0, 8))
            
            presets = [
                (2700, "Warm (2700K)"),
                (4500, "Neutral (4500K)"),
                (6500, "Cool (6500K)")
            ]
            for val, label in presets:
                btn = tk.Button(
                    presets_f, 
                    text=label, 
                    bg="#2c313c", 
                    fg="#9aa0a6", 
                    font=("Inter", 8),
                    activebackground="#3e4451",
                    activeforeground="#ffffff",
                    relief="flat", 
                    bd=0, 
                    padx=6, 
                    pady=2,
                    cursor="hand2",
                    state="disabled" if device["permission_denied"] else "normal",
                    command=lambda v=val, s=slider_temp: s.set(v)
                )
                btn.pack(side="left", expand=True, padx=3)
            
            # Metadata Footer
            footer_f = tk.Frame(pad_frame, bg="#21252b")
            footer_f.pack(fill="x", pady=(8, 0))
            
            tk.Label(footer_f, text=device["model"], fg="#00d2ff", bg="#21252b", font=("Inter", 8, "bold")).pack(side="left")
            tk.Label(footer_f, text=device["id"][:14], fg="#5f6368", bg="#21252b", font=("Inter", 8)).pack(side="right")
            
            # Handle real-time hardware state updates with debouncing
            def trigger_device_update(*_):
                device_id = device["id"]
                
                # Dynamic visual toggle update (instant feedback)
                is_on = btn_power.cget("text") == "Active"
                
                # Update visual color accents dynamically
                border_color = accent_color if is_on else "#2d3139"
                accent_bar.configure(bg=border_color)
                card.configure(highlightbackground=border_color, highlightcolor=border_color)
                
                # Check slider labels
                b_val = int(float(slider_brightness.get()))
                t_val = int(round(float(slider_temp.get()) / 100.0) * 100)
                b_val_lbl.configure(text=f"{b_val}%")
                t_val_lbl.configure(text=f"{t_val}K")
                
                # Cancel existing timer
                if device_id in self.debounce_timers:
                    self.after_cancel(self.debounce_timers[device_id])
                    
                # Schedule new write task (debounce delay: 80ms)
                self.debounce_timers[device_id] = self.after(80, lambda: self.commit_state_change(device_id, is_on, b_val, t_val))
                
            def toggle_power():
                # Toggle text and color state
                if btn_power.cget("text") == "Active":
                    btn_power.configure(text="Standby", bg="#5f6368")
                else:
                    btn_power.configure(text="Active", bg="#00e676")
                trigger_device_update()
                
            btn_power.configure(command=toggle_power)
            
            # Configure commands now that the callback is defined
            slider_brightness.configure(command=trigger_device_update)
            slider_temp.configure(command=trigger_device_update)

        def commit_state_change(self, device_id, on, brightness, temperature):
            try:
                commit_device_control(device_id, on=on, brightness=brightness, temperature=temperature)
            except Exception as e:
                logging.error(f"Failed to commit control update to device {device_id}: {e}")

    # Launch GUI Window Loop
    app = LitraAppWindow()
    app.mainloop()
    return 0

# --- Unified Entrypoint ---

def main():
    args = parse_args()
    
    # If the user sets explicit control/scanning flags, run the CLI
    has_cli_options = (args.scan or args.on or args.off or args.brightness is not None or 
                       args.temperature is not None or args.label is not None)
                       
    if has_cli_options:
        return run_cli(args)
        
    # Check if graphical display session is active (to avoid Tkinter crash in headless mode)
    gui_available = "DISPLAY" in os.environ or "WAYLAND_DISPLAY" in os.environ
    if args.gui or gui_available:
        return run_gui()
    else:
        # Default to printing CLI help if headless and no options are set
        print("No command flags provided and no GUI display session detected.")
        print("Please run with command-line arguments. Examples:")
        print("  python3 app.py --scan")
        print("  python3 app.py --on --brightness 80")
        print("\nAlternatively, start with graphical options inside a desktop environment.")
        return 1

def parse_args():
    parser = argparse.ArgumentParser(description="Logitech Litra Controller (CLI & Desktop GUI)")
    
    # Action groups
    parser.add_argument("-s", "--scan", action="store_true", help="Scan and list detected Litra lights with state configurations")
    parser.add_argument("-d", "--device", type=str, help="Specify the USB ID or Serial Number of the device to control")
    
    # Control flags
    parser.add_argument("--on", action="store_true", help="Turn target light ON")
    parser.add_argument("--off", action="store_true", help="Turn target light OFF")
    parser.add_argument("-b", "--brightness", type=int, help="Adjust brightness percentage (0-100%%)")
    parser.add_argument("-t", "--temperature", type=int, help="Adjust color temperature (2700K - 6500K)")
    parser.add_argument("-l", "--label", type=str, help="Save a custom label name for the target device")
    
    # GUI force flag
    parser.add_argument("-g", "--gui", action="store_true", help="Launch the Desktop GUI interface")
    
    return parser.parse_args()

if __name__ == "__main__":
    sys.exit(main())
