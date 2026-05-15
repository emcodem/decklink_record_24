import os
import sys

if sys.platform == "win32":
    # PyAV is built against the FFmpeg DLLs in vendor/ffmpeg-dlls/.
    # Python 3.8+ restricts DLL search paths, so we must register the directory
    # explicitly before the first `import av` anywhere in the process.
    _here = os.path.dirname(os.path.abspath(__file__))  # src/ffrecord/
    _dlls = os.path.normpath(os.path.join(_here, "..", "..", "vendor", "ffmpeg-dlls"))
    if os.path.isdir(_dlls):
        os.add_dll_directory(_dlls)
