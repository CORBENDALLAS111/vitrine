#!/usr/bin/env python3
"""Remove an older Vitrine injection from an IPA before rebuilding it.

This keeps the original Mach-O layout intact while removing only Vitrine's
load commands and bundled files. Running it on a clean IPA is a no-op.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import struct
import sys
import tempfile
import zipfile


LOAD_DYLIB_COMMANDS = {0xC, 0x80000018, 0x8000001F, 0x80000023}
VITRINE_DYLIBS = {
    b"@rpath/spotifyglass.dylib",
    b"@rpath/SpotifyGlassAppGroups.dylib",
}


def strip_load_commands(binary: bytes) -> tuple[bytes, list[str]]:
    if binary[:4] == b"\xcf\xfa\xed\xfe":
        endian = "<"
    elif binary[:4] == b"\xfe\xed\xfa\xcf":
        endian = ">"
    else:
        raise ValueError("Spotify executable is not a thin 64-bit Mach-O")

    header_size = 32
    header = list(struct.unpack_from(endian + "IiiIIIII", binary, 0))
    ncmds, sizeofcmds = header[4], header[5]
    position = header_size
    kept: list[bytes] = []
    removed: list[str] = []

    for _ in range(ncmds):
        command, command_size = struct.unpack_from(endian + "II", binary, position)
        if command_size < 8 or position + command_size > header_size + sizeofcmds:
            raise ValueError("Invalid Mach-O load-command table")

        raw_command = binary[position : position + command_size]
        remove = False
        if command in LOAD_DYLIB_COMMANDS:
            name_offset = struct.unpack_from(endian + "I", raw_command, 8)[0]
            name_end = raw_command.find(b"\0", name_offset)
            if name_end < 0:
                name_end = len(raw_command)
            name = raw_command[name_offset:name_end]
            if name in VITRINE_DYLIBS:
                removed.append(name.decode("utf-8"))
                remove = True

        if not remove:
            kept.append(raw_command)
        position += command_size

    if not removed:
        return binary, []

    new_commands = b"".join(kept)
    if len(new_commands) > sizeofcmds:
        raise ValueError("Rebuilt Mach-O command table grew unexpectedly")

    output = bytearray(binary)
    output[header_size : header_size + sizeofcmds] = new_commands + bytes(
        sizeofcmds - len(new_commands)
    )
    struct.pack_into(endian + "I", output, 16, len(kept))
    struct.pack_into(endian + "I", output, 20, len(new_commands))
    return bytes(output), removed


def clean_ipa(path: str) -> None:
    absolute = os.path.abspath(path)
    directory = os.path.dirname(absolute)
    temporary: str | None = None

    try:
        with zipfile.ZipFile(absolute, "r") as source:
            info_paths = [
                name
                for name in source.namelist()
                if name.startswith("Payload/")
                and name.count("/") == 2
                and name.endswith(".app/Info.plist")
            ]
            if len(info_paths) != 1:
                raise ValueError("Expected exactly one Payload/*.app/Info.plist")

            info_path = info_paths[0]
            app_directory = info_path.rsplit("/", 1)[0]
            app_info = plistlib.loads(source.read(info_path))
            executable_path = f"{app_directory}/{app_info['CFBundleExecutable']}"

            excluded_paths = {
                f"{app_directory}/Frameworks/spotifyglass.dylib",
                f"{app_directory}/Frameworks/SpotifyGlassAppGroups.dylib",
                "Payload/softgaypaws.com",
            }
            excluded_prefixes = (
                f"{app_directory}/PlugIns/SpotifyGlassLiveActivity.appex/",
            )

            patched_executable, removed_commands = strip_load_commands(
                source.read(executable_path)
            )
            removed_files = [
                entry.filename
                for entry in source.infolist()
                if entry.filename in excluded_paths
                or entry.filename.startswith(excluded_prefixes)
            ]
            if not removed_commands and not removed_files:
                print("No previous Vitrine injection found")
                return

            file_descriptor, temporary = tempfile.mkstemp(
                prefix="vitrine-clean-", suffix=".ipa", dir=directory
            )
            os.close(file_descriptor)
            with zipfile.ZipFile(temporary, "w", allowZip64=True) as destination:
                for entry in source.infolist():
                    if entry.filename in excluded_paths or entry.filename.startswith(
                        excluded_prefixes
                    ):
                        removed_files.append(entry.filename)
                        continue

                    if entry.filename == executable_path:
                        destination.writestr(entry, patched_executable)
                        continue

                    with source.open(entry) as reader, destination.open(
                        entry, "w", force_zip64=True
                    ) as writer:
                        shutil.copyfileobj(reader, writer, length=1024 * 1024)

        os.replace(temporary, absolute)
        print(
            f"Removed {len(removed_commands)} Vitrine load commands and "
            f"{len(removed_files)} bundled entries"
        )
    except Exception:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        raise


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} path/to/spotify.ipa")
    clean_ipa(sys.argv[1])
