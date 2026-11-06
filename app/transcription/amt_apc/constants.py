"""Every value of AMT-APC's config.json that inference uses, copied unchanged."""

# Features: a log-mel spectrogram of mono 16 kHz audio.
SAMPLE_RATE = 16000
HOP_SAMPLES = 256
MEL_BINS = 256
N_BINS = 256
FFT_BINS = 2048
WINDOW_LENGTH = 2048
LOG_OFFSET = 1e-08
PAD_MODE = "constant"

# Input windows: NUM_FRAME frames, with context frames on each side.
MARGIN_BEFORE = 32
MARGIN_AFTER = 32
NUM_FRAME = 512
# What the margin before the first frame and after the last one is filled with.
MIN_VALUE = -18.0

# Output: 88 piano keys from MIDI note 21 (A0), and 128 velocity classes.
NOTE_MIN = 21
NUM_NOTE = 88
NUM_VELOCITY = 128

# The network.
CNN_CHANNEL = 4
CNN_KERNEL = 5
HID_DIM = 256
PF_DIM = 512
ENCODER_LAYERS = 3
ENCODER_HEADS = 4
DECODER_LAYERS = 3
DECODER_HEADS = 4
STYLE_DIM = 24

# Turning the network's output into notes.
THRESHOLD_ONSET = 0.5
THRESHOLD_OFFSET = 1.0
THRESHOLD_FRAME = 0.5
# Seconds; shorter notes are dropped.
MIN_DURATION = 0.08

# The model computes 64 frames and 22 notes at a time. The original does all
# 512 frames at once and needs about 3.1 GiB of GPU memory; this needs about
# 0.7 GiB for the same result.
FRAME_CHUNK = 64
NOTE_CHUNK = 22
