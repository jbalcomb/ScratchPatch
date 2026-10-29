"""Splitting executables and DLLs into small text files for an AI agent (Parse executable, Parse DLL).

    formats.py   reads the headers (MZ, PE, NE; LE/LX detected) into regions, tables and exports
    layout.py    writes the folder
    drive.py     mirrors the folder to Google Drive
    job.py       the fields, steps and settings both jobs share
"""
