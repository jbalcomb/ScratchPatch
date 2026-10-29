"""Parse DLL: the same as Parse executable, for libraries (.dll, .drv, .ocx ...), with the exports table
up front: every exported function's ordinal, name, address and the chunk file holding it.

A DLL is the same file format as a program (PE, or NE for 16-bit Windows), marked as a library; what
other modules use from it is its exports. See docs/jobs/dll_parse.md. The work is shared with Parse
executable (``taskdeck.shared.binaries``), and so are its settings (the Google Drive folder), so a
program and its DLLs end up side by side.
"""

from taskdeck.shared.binaries import job

EXTENSIONS = (".dll", ".drv", ".ocx", ".cpl", ".sys", ".vxd", ".386")


class DllParse(job.BinaryJob):
    id = "dll_parse"
    title = "Parse DLL"
    description = ("Split a Windows .dll into small text files (exports table, summary, layout, metadata, "
                   "headers, hex chunks, strings) for an AI agent to reverse engineer, optionally in Google Drive.")
    icon = "EXTENSION"
    tags = ("dll", "library", "exports", "ordinal", "drv", "ocx", "binary", "pe", "ne", "windows", "hex",
            "dump", "reverse engineering", "strings", "header", "chunks", "agent")
    file_field = "dll"
    example = "KERNEL32.DLL"
    expects_dll = True
    fields = job.fields("dll", "DLL", "A .dll, .drv, .ocx ... file.", EXTENSIONS, example)
    settings = job.settings(example)
    settings_from = "exe_parse"  # one Drive folder for programs and their DLLs


PLUGIN = DllParse()
