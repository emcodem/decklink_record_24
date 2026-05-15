"""DeckLink COM interfaces using comtypes (vtable-based COM).

Adapted from ffcapture/src/decklink_comtypes.py.
Added IDeckLinkVideoInputFrame2 vtable entries for GetHardwareReferenceTimestamp
and GetTimecode so the capture layer can extract them without QueryInterface fallbacks.
"""

import logging
import ctypes
from ctypes import POINTER, c_uint, c_int, c_uint8, c_uint32, c_long, c_void_p, byref, c_wchar_p, c_int64, c_uint64
from comtypes import IUnknown, GUID, HRESULT, COMMETHOD
from comtypes.client import CreateObject
import numpy as np

logger = logging.getLogger(__name__)

# ============================================================================
# BMD Constants
# ============================================================================

class BMDPixelFormat:
    bmdFormat8BitYUV = 0x32767579
    bmdFormat10BitYUV = 0x76323130
    bmdFormat8BitARGB = 0x20424741
    bmdFormat8BitBGRA = 0x61726762

class BMDDisplayMode:
    bmdModeHD1080i50 = 0x48693530
    bmdModeHD1080i5994 = 0x48693539
    bmdModeHD1080i6000 = 0x48693630
    bmdModeHD1080p25 = 0x48703235
    bmdModeHD1080p50 = 0x48703530
    bmdModeHD1080p5994 = 0x48703539
    bmdModeHD1080p6000 = 0x48703630
    bmdModeHD720p50 = 0x68703530
    bmdModeHD720p5994 = 0x68703539
    bmdModeHD720p60 = 0x68703630

class BMDVideoInputFlags:
    bmdVideoInputFlagDefault = 0
    bmdVideoInputEnableFormatDetection = 1

class BMDVideoOutputFlags:
    bmdVideoOutputFlagDefault = 0

class BMDAudioSampleRate:
    bmdAudioSampleRate48kHz = 48000

class BMDAudioSampleType:
    bmdAudioSampleType16bitInteger = 16
    bmdAudioSampleType32bitInteger = 32

class BMDAudioOutputStreamType:
    bmdAudioOutputStreamContinuous = 0

class BMDFrameFlags:
    bmdFrameFlagDefault = 0
    bmdFrameHasNoInputSource = 1 << 5   # Frame arrived when no SDI signal present

class BMDTimecodeFormat:
    bmdTimecodeRP188Any = 0x52503138    # RP 188 timecode (preferred)
    bmdTimecodeVITC = 0x56495443       # VITC timecode

# ============================================================================
# COM Interfaces
# ============================================================================

class IDeckLinkVideoBuffer(IUnknown):
    _iid_ = GUID("{CCB4B64A-5C86-4E02-B778-885D352709FE}")
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetBytes',
                  (['out'], POINTER(c_void_p), 'buffer')),
    ]

class IDeckLinkAudioInputPacket(IUnknown):
    _iid_ = GUID("{E43D5870-2894-11DE-8C30-0800200C9A66}")
    _methods_ = [
        COMMETHOD([], c_long, 'GetSampleFrameCount'),
        COMMETHOD([], HRESULT, 'GetBytes',
                  (['out'], POINTER(c_void_p), 'buffer')),
        COMMETHOD([], HRESULT, 'GetPacketTime',
                  (['out'], POINTER(c_int64), 'packetTime'),
                  (['in'], c_uint32, 'timeScale')),
    ]

class IDeckLink(IUnknown):
    _iid_ = GUID("{C418FBDD-0587-48ED-8FE5-640F0A14AF91}")
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetModelName',
                  (['out'], POINTER(c_wchar_p), 'modelName')),
        COMMETHOD([], HRESULT, 'GetDisplayName',
                  (['out'], POINTER(c_wchar_p), 'displayName')),
    ]

class IDeckLinkIterator(IUnknown):
    _iid_ = GUID("{50FB36CD-3063-4B73-BDBB-958087F2D8BA}")

class IDeckLinkDisplayMode(IUnknown):
    _iid_ = GUID("{550D4B8C-F0F8-4B68-B87C-FBD2FC21A87C}")
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetName',
                  (['out'], POINTER(c_wchar_p), 'name')),
        COMMETHOD([], c_long, 'GetWidth'),
        COMMETHOD([], c_long, 'GetHeight'),
        COMMETHOD([], HRESULT, 'GetFrameRate',
                  (['out'], POINTER(c_uint32), 'framerate_num'),
                  (['out'], POINTER(c_uint32), 'framerate_den')),
        COMMETHOD([], c_uint32, 'GetDisplayMode'),
        COMMETHOD([], c_uint32, 'GetFieldDominance'),
        COMMETHOD([], c_uint32, 'GetFlags'),
    ]

class IDeckLinkVideoFrame(IUnknown):
    _iid_ = GUID("{6502091C-615F-4F51-BAF6-45C4256DD5B0}")
    _methods_ = [
        COMMETHOD([], c_long, 'GetWidth'),
        COMMETHOD([], c_long, 'GetHeight'),
        COMMETHOD([], c_long, 'GetRowBytes'),
        COMMETHOD([], c_uint32, 'GetPixelFormat'),
        COMMETHOD([], c_uint32, 'GetFlags'),
        COMMETHOD([], HRESULT, 'GetBytes',
                  (['out'], POINTER(c_void_p), 'buffer')),
        COMMETHOD([], HRESULT, 'GetTimecode',
                  (['in'], c_uint32, 'format'),
                  (['out'], POINTER(c_void_p), 'timecode')),
        COMMETHOD([], HRESULT, 'GetAncillaryData',
                  (['out'], POINTER(c_void_p), 'ancillary')),
    ]

class IDeckLinkVideoInputFrame(IDeckLinkVideoFrame):
    _iid_ = GUID("{C9ADD3D2-BE52-488D-AB2D-7FDEF7AF0C95}")
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetStreamTime',
                  (['out'], POINTER(c_int64), 'frameTime'),
                  (['out'], POINTER(c_int64), 'frameDuration'),
                  (['in'], c_uint32, 'timeScale')),
        COMMETHOD([], HRESULT, 'GetHardwareReferenceTimestamp',
                  (['in'], c_uint32, 'timeScale'),
                  (['out'], POINTER(c_int64), 'frameTime'),
                  (['out'], POINTER(c_int64), 'frameDuration')),
    ]

class IDeckLinkMutableVideoFrame(IDeckLinkVideoFrame):
    _iid_ = GUID("{CF9EB134-0374-4C5B-95FA-1EC14819FF62}")
    _methods_ = [
        COMMETHOD([], HRESULT, 'SetFlags',
                  (['in'], c_uint32, 'newFlags')),
        COMMETHOD([], HRESULT, 'SetTimecode',
                  (['in'], c_uint32, 'format'),
                  (['in'], c_void_p, 'timecode')),
        COMMETHOD([], HRESULT, 'SetTimecodeFromComponents',
                  (['in'], c_uint32, 'format'),
                  (['in'], c_uint8, 'hours'),
                  (['in'], c_uint8, 'minutes'),
                  (['in'], c_uint8, 'seconds'),
                  (['in'], c_uint8, 'frames'),
                  (['in'], c_uint32, 'flags')),
    ]

class IDeckLinkInputCallback(IUnknown):
    _iid_ = GUID("{3A94F075-C37D-4BA8-BCC0-1D778C8F881B}")

class IDeckLinkInput(IUnknown):
    _iid_ = GUID("{4095DB82-E294-4B8C-AAA8-3B9E80C49336}")
    _methods_ = [
        COMMETHOD([], HRESULT, 'EnableVideoInput',
                  (['in'], c_uint32, 'displayMode'),
                  (['in'], c_uint32, 'pixelFormat'),
                  (['in'], c_uint32, 'flags')),
        COMMETHOD([], HRESULT, 'DisableVideoInput'),
        COMMETHOD([], HRESULT, 'EnableAudioInput',
                  (['in'], c_uint32, 'sampleRate'),
                  (['in'], c_uint32, 'sampleType'),
                  (['in'], c_uint, 'channelCount')),
        COMMETHOD([], HRESULT, 'DisableAudioInput'),
        COMMETHOD([], HRESULT, 'StartStreams'),
        COMMETHOD([], HRESULT, 'StopStreams'),
        COMMETHOD([], HRESULT, 'PauseStreams'),
        COMMETHOD([], HRESULT, 'FlushStreams'),
        COMMETHOD([], HRESULT, 'SetCallback',
                  (['in'], POINTER(IDeckLinkInputCallback), 'theCallback')),
        COMMETHOD([], HRESULT, 'GetAvailableVideoFrameCount',
                  (['out'], POINTER(c_uint32), 'availableFrameCount')),
    ]

# ============================================================================
# Helper Functions
# ============================================================================

def get_device_by_index(index: int) -> tuple:
    """Get DeckLink device by index. Returns (IDeckLinkInput, display_name)."""
    from comtypes.client import GetModule

    dll_path = r"C:\Program Files\Blackmagic Design\Blackmagic Desktop Video\DeckLinkAPI64.dll"
    logger.debug("Loading DeckLink type library from %s", dll_path)
    decklink_module = GetModule(dll_path)

    iterator = CreateObject(decklink_module.CDeckLinkIterator)
    logger.info("Created DeckLink iterator")

    device = None
    for i in range(index + 1):
        try:
            device = iterator.Next()
            if device is None:
                raise RuntimeError(f"Device index {index} not found (iterator exhausted at {i})")
        except Exception as e:
            raise RuntimeError(f"Device index {index} not found: {e}") from e

    try:
        display_name = device.GetDisplayName()
    except Exception:
        display_name = f"DeckLink#{index}"
    logger.info("Found DeckLink device %d: %s", index, display_name)

    try:
        decklink_input = device.QueryInterface(decklink_module.IDeckLinkInput)
    except Exception as e:
        raise RuntimeError(f"Device {index} does not support IDeckLinkInput: {e}") from e

    return decklink_input, display_name
