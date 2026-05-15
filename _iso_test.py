import os
os.add_dll_directory(r'C:\dev\ffrecord\vendor\ffmpeg-dlls')
import av
import numpy as np
from pathlib import Path

p = Path(r'C:\dev\ffrecord\test_archive\test_segment.mov').resolve()
p.parent.mkdir(parents=True, exist_ok=True)
print('writing:', p)

container = av.open(str(p), mode='w')
vstream = container.add_stream('hevc_nvenc', rate=30)
vstream.options = {'profile': 'main10'}
vstream.bit_rate = 50_000_000
vstream.codec_context.width = 1920
vstream.codec_context.height = 1080
vstream.codec_context.pix_fmt = 'p010le'
vstream.codec_context.options['preset'] = 'p7'

astream = container.add_stream('pcm_s24le', rate=48000, layout='7.1')
print('streams added; v-fmt expected:', vstream.codec_context.pix_fmt,
      'a-fmt expected:', astream.codec_context.format)

# Feed 10 frames so NVENC actually emits packets
total_pkts = 0
total_audio_pkts = 0
for i in range(10):
    arr = np.zeros((1080 * 3 // 2, 1920), dtype=np.uint8)
    arr[:] = (i * 25) % 255
    frame = av.VideoFrame.from_ndarray(arr, format='yuv420p')
    frame.pts = i
    for pkt in vstream.encode(frame):
        container.mux(pkt)
        total_pkts += 1
    # audio: 1600 samples * 8 channels
    ad = np.zeros((1600, 8), dtype=np.int32)
    af = av.AudioFrame.from_ndarray(ad.reshape(1, -1), format='s32', layout='7.1')
    af.sample_rate = 48000
    for pkt in astream.encode(af):
        container.mux(pkt)
        total_audio_pkts += 1

# flush
for pkt in vstream.encode(None):
    container.mux(pkt)
    total_pkts += 1
for pkt in astream.encode(None):
    container.mux(pkt)
    total_audio_pkts += 1

container.close()
print(f'OK: muxed {total_pkts} video pkts, {total_audio_pkts} audio pkts')
print(f'output: {p.stat().st_size} bytes')
