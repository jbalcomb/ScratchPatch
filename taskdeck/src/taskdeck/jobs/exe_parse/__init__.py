"""Parse Executable: split a DOS or Windows program into small, labelled text files that an AI agent
doing reverse engineering can walk through or search one piece at a time.

The output is a folder next to the executable, named like it with "_" in front (``_WIZARDS.EXE/``), with
a summary and layout, a metadata file, decoded headers and tables, hex-dump chunks per region and the
strings, and optionally a mirror of it in Google Drive. See docs/jobs/exe_parse.md. The work is shared
with Parse DLL: ``taskdeck.shared.binaries``.
"""

from taskdeck.shared.binaries import job

EXTENSIONS = (".exe", ".com", ".ovl", ".scr")


class ExeParse(job.BinaryJob):
    id = "exe_parse"
    title = "Parse executable"
    description = ("Split a DOS or Windows .exe into small text files (summary, layout, metadata, headers, "
                   "hex chunks, strings) for an AI agent to reverse engineer, optionally in Google Drive.")
    icon = "MEMORY"
    tags = ("exe", "binary", "mz", "pe", "ne", "dos", "windows", "hex", "dump", "disassemble",
            "reverse engineering", "strings", "header", "chunks", "agent")
    file_field = "exe"
    example = "WIZARDS.EXE"
    expects_dll = False
    fields = job.fields("exe", "Executable", "A .exe, .com, .ovl ... file.", EXTENSIONS, example)
    settings = job.settings(example)


PLUGIN = ExeParse()
