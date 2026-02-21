#!/usr/bin/env python3
import os
import re
import subprocess
import sys
import shutil

# === helpers ===
def pack_major_minor(major, minor):
    if not (0 <= major <= 15 and 0 <= minor <= 15):
        raise ValueError(f"major/minor must be 0..15 (nibble). Got {major}.{minor}")
    return (major << 4) | minor

def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def get_define_recursive(name, text, visited=None, include_dirs=None, config_h_path=None):
    if visited is None:
        visited = set()
    if include_dirs is None:
        include_dirs = []

    if name in visited:
        raise ValueError(f"Circular define: {name}")

    # regex: match #define at start of line ignoring commented lines
    m = re.search(r"^[ \t]*#define[ \t]+" + re.escape(name) + r"[ \t]+([^\s/]+)", text, re.MULTILINE)
    if m:
        val = m.group(1)
        visited.add(name)
        if re.match(r"^(0x[0-9A-Fa-f]+|\d+)$", val):
            return int(val, 0)
        return get_define_recursive(val, text, visited, include_dirs, config_h_path)

    # check includes
    includes = re.findall(r'#include\s+[<"]([^>"]+)[>"]', text)
    for inc in includes:
        for d in ([os.path.dirname(config_h_path)] if config_h_path else []) + include_dirs:
            inc_path = os.path.normpath(os.path.abspath(os.path.join(d, inc)))
            if os.path.exists(inc_path):
                try:
                    return get_define_recursive(name, read_file(inc_path), visited.copy(), include_dirs, config_h_path)
                except ValueError as e:
                    if "Missing #define" in str(e):
                        continue
                    else:
                        raise

    raise ValueError(f"Missing #define {name} in {config_h_path or 'headers'}")

def extract_versions(config_h, include_dirs=None):
    text = read_file(config_h)
    include_dirs = include_dirs or []

    # APP
    app_major = get_define_recursive("APP_MAJOR", text, include_dirs=include_dirs, config_h_path=config_h)
    app_minor = get_define_recursive("APP_MINOR", text, include_dirs=include_dirs, config_h_path=config_h)
    app_build = get_define_recursive("APP_BUILD", text, include_dirs=include_dirs, config_h_path=config_h)

    # STACK
    stack_major = get_define_recursive("STACK_MAJOR", text, include_dirs=include_dirs, config_h_path=config_h)
    stack_minor = get_define_recursive("STACK_MINOR", text, include_dirs=include_dirs, config_h_path=config_h)
    stack_build = get_define_recursive("STACK_BUILD", text, include_dirs=include_dirs, config_h_path=config_h)

    # HW
    hw_major = get_define_recursive("HW_MAJOR", text, include_dirs=include_dirs, config_h_path=config_h)
    hw_minor = get_define_recursive("HW_MINOR", text, include_dirs=include_dirs, config_h_path=config_h)

    # OTA fields
    ota_version = (pack_major_minor(app_major, app_minor) << 24) | (app_build << 16) | (pack_major_minor(stack_major, stack_minor) << 8) | stack_build
    hw_version = (hw_major << 8) | hw_minor

    manuf_id = get_define_recursive("DEVICE_OTA_MANUFACTURER_CODE", text, include_dirs=include_dirs, config_h_path=config_h)
    image_type = get_define_recursive("DEVICE_OTA_IMAGE_TYPE", text, include_dirs=include_dirs, config_h_path=config_h)

    return f"0x{ota_version:08X}", f"0x{hw_version:04X}", f"0x{manuf_id:04X}", f"0x{image_type:04X}"

def build_ota(
    project_path,
    project_name,
    config_h_rel="main/include/config.h",
    build_dir="build_prod",
    header_string=None,
    include_dirs=None,
    ota_output_dir=None,  # remove from call if not provided
):
    # Resolve paths
    project_path = os.path.abspath(os.path.dirname(project_path))
    print("project_path",project_path)
    config_h = os.path.join(project_path, config_h_rel)
    print("config_h",config_h)
    build_dir = os.path.join(project_path, build_dir)
    print("build_dir",build_dir)

    if include_dirs is None:
        include_dirs = []

    # If OTA output folder not given, use the folder where this script sits
    if ota_output_dir is None:
        ota_output_dir = os.path.dirname(os.path.abspath(__file__))

    # Ensure build_dir exists
    bin_file = os.path.join(build_dir, f"{project_name}.bin")
    ota_file = os.path.join(ota_output_dir, f"{project_name}.ota")

    if not os.path.exists(bin_file):
        raise FileNotFoundError(f"{bin_file} not found. Run `idf.py build` first.")

    ota_version_hex, hw_version_hex, manuf_hex, image_hex = extract_versions(config_h, include_dirs)

    # image_builder_tool path (adjust if needed)
   # tool path relative to script (adjust if your tree is different)
    tool = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "components", "esp-zigbee-sdk", "tools", "image_builder_tool", "image_builder_tool.py"))
    if not os.path.exists(tool):
        raise FileNotFoundError(f"image_builder_tool not found at {tool}")

    cmd = [
        sys.executable,
        tool,
        "--create", ota_file,
        "--manuf-id", manuf_hex,
        "--image-type", image_hex,
        "--version", ota_version_hex,
        "--tag-id", "0",
        "--tag-file", bin_file,
        "--min-hw-ver", hw_version_hex,
        "--max-hw-ver", hw_version_hex,
        "--header_string", f"\"{header_string or project_name + ' OTA'}\"",
    ]

    print("Running OTA builder:", " ".join(cmd))
    subprocess.check_call(cmd)
    print(f"✅ OTA image created: {ota_file}")
    print(f"   OTA version = {ota_version_hex}, HW version = {hw_version_hex}")
    print(f"   Manufacturer = {manuf_hex}, Image type = {image_hex}")

    # Copy to OTA images dir if requested
    if ota_output_dir:
        dest_file = os.path.join(ota_output_dir, os.path.basename(ota_file))
        if os.path.abspath(ota_file) != os.path.abspath(dest_file):
            os.makedirs(ota_output_dir, exist_ok=True)
            shutil.copy2(ota_file, dest_file)
            print(f"✅ OTA image copied to: {dest_file}")
        return dest_file

    return ota_file