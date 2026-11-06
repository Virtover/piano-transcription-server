"""The AMT-APC network: hFT-Transformer with a style vector, for inference.

The layers and their parameter names are those of the original, so apc.pth
loads as it is. Dropout is gone, as it does nothing in eval, and so is the
frequency attention the original returns for visualisation. The original
computes a whole window at once and needs about 3.1 GiB of GPU memory. Here
the encoder and the decoder's frequency stage run a few frames at a time, which
they treat independently, and the time stage a few notes at a time; the
outputs are the same.
"""

import math
from pathlib import Path

import torch
from torch import nn

from app.transcription.amt_apc import constants


class Attention(nn.Module):
    def __init__(self, hid_dim: int, heads: int) -> None:
        super().__init__()
        self.heads = heads
        self.head_dim = hid_dim // heads
        self.scale = math.sqrt(self.head_dim)
        self.fc_q = nn.Linear(hid_dim, hid_dim)
        self.fc_k = nn.Linear(hid_dim, hid_dim)
        self.fc_v = nn.Linear(hid_dim, hid_dim)
        self.fc_o = nn.Linear(hid_dim, hid_dim)

    def forward(self, query: torch.Tensor, key: torch.Tensor, value: torch.Tensor) -> torch.Tensor:
        batch = query.shape[0]
        q = self.fc_q(query).view(batch, -1, self.heads, self.head_dim).permute(0, 2, 1, 3)
        k = self.fc_k(key).view(batch, -1, self.heads, self.head_dim).permute(0, 2, 1, 3)
        v = self.fc_v(value).view(batch, -1, self.heads, self.head_dim).permute(0, 2, 1, 3)
        attention = torch.softmax(torch.matmul(q, k.permute(0, 1, 3, 2)) / self.scale, dim=-1)
        x = torch.matmul(attention, v).permute(0, 2, 1, 3).contiguous()
        return self.fc_o(x.view(batch, -1, self.heads * self.head_dim))


class Feedforward(nn.Module):
    def __init__(self, hid_dim: int, pf_dim: int) -> None:
        super().__init__()
        self.fc_1 = nn.Linear(hid_dim, pf_dim)
        self.fc_2 = nn.Linear(pf_dim, hid_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc_2(torch.relu(self.fc_1(x)))


class EncoderLayer(nn.Module):
    """Self-attention, then a feedforward; one layer norm serves both, as in the original."""

    def __init__(self, hid_dim: int, heads: int, pf_dim: int) -> None:
        super().__init__()
        self.layer_norm = nn.LayerNorm(hid_dim)
        self.self_attention = Attention(hid_dim, heads)
        self.positionwise_feedforward = Feedforward(hid_dim, pf_dim)

    def forward(self, src: torch.Tensor) -> torch.Tensor:
        src = self.layer_norm(src + self.self_attention(src, src, src))
        return self.layer_norm(src + self.positionwise_feedforward(src))


class DecoderLayerZero(nn.Module):
    """The first decoder layer: the notes attend to the encoder output."""

    def __init__(self, hid_dim: int, heads: int, pf_dim: int) -> None:
        super().__init__()
        self.layer_norm = nn.LayerNorm(hid_dim)
        self.encoder_attention = Attention(hid_dim, heads)
        self.positionwise_feedforward = Feedforward(hid_dim, pf_dim)

    def forward(self, enc_src: torch.Tensor, trg: torch.Tensor) -> torch.Tensor:
        trg = self.layer_norm(trg + self.encoder_attention(trg, enc_src, enc_src))
        return self.layer_norm(trg + self.positionwise_feedforward(trg))


class DecoderLayer(nn.Module):
    def __init__(self, hid_dim: int, heads: int, pf_dim: int) -> None:
        super().__init__()
        self.layer_norm = nn.LayerNorm(hid_dim)
        self.self_attention = Attention(hid_dim, heads)
        self.encoder_attention = Attention(hid_dim, heads)
        self.positionwise_feedforward = Feedforward(hid_dim, pf_dim)

    def forward(self, enc_src: torch.Tensor, trg: torch.Tensor) -> torch.Tensor:
        trg = self.layer_norm(trg + self.self_attention(trg, trg, trg))
        trg = self.layer_norm(trg + self.encoder_attention(trg, enc_src, enc_src))
        return self.layer_norm(trg + self.positionwise_feedforward(trg))


class Encoder(nn.Module):
    """Turns each frame, with its margins, into one vector per frequency bin."""

    def __init__(self) -> None:
        super().__init__()
        self.n_proc = constants.MARGIN_BEFORE + constants.MARGIN_AFTER + 1
        self.cnn_dim = constants.CNN_CHANNEL * (self.n_proc - (constants.CNN_KERNEL - 1))
        self.scale = math.sqrt(constants.HID_DIM)
        self.conv = nn.Conv2d(1, constants.CNN_CHANNEL, kernel_size=(1, constants.CNN_KERNEL))
        self.tok_embedding_freq = nn.Linear(self.cnn_dim, constants.HID_DIM)
        self.pos_embedding_freq = nn.Embedding(constants.N_BINS, constants.HID_DIM)
        self.layers_freq = nn.ModuleList(
            EncoderLayer(constants.HID_DIM, constants.ENCODER_HEADS, constants.PF_DIM)
            for _ in range(constants.ENCODER_LAYERS)
        )

    def forward(self, frames: torch.Tensor) -> torch.Tensor:
        """frames [count, n_bins, n_proc] to [count, n_bins, hid_dim]."""
        count = frames.shape[0]
        x = self.conv(frames.unsqueeze(1)).permute(0, 2, 1, 3).contiguous()
        x = self.tok_embedding_freq(x.reshape(count, constants.N_BINS, self.cnn_dim))
        positions = torch.arange(constants.N_BINS, device=frames.device)
        x = x * self.scale + self.pos_embedding_freq(positions)
        for layer in self.layers_freq:
            x = layer(x)
        return x


class Decoder(nn.Module):
    """Reads the notes off the encoder output: first across frequency within
    a frame (head "A"), then across time within a note (head "B")."""

    def __init__(self) -> None:
        super().__init__()
        hid_dim, heads, pf_dim = constants.HID_DIM, constants.DECODER_HEADS, constants.PF_DIM
        self.scale = math.sqrt(hid_dim)
        self.pos_embedding_freq = nn.Embedding(constants.NUM_NOTE, hid_dim)
        self.layer_zero_freq = DecoderLayerZero(hid_dim, heads, pf_dim)
        self.layers_freq = nn.ModuleList(
            DecoderLayer(hid_dim, heads, pf_dim) for _ in range(constants.DECODER_LAYERS - 1)
        )
        self.fc_onset_freq = nn.Linear(hid_dim, 1)
        self.fc_offset_freq = nn.Linear(hid_dim, 1)
        self.fc_mpe_freq = nn.Linear(hid_dim, 1)
        self.fc_velocity_freq = nn.Linear(hid_dim, constants.NUM_VELOCITY)
        self.pos_embedding_time = nn.Embedding(constants.NUM_FRAME, hid_dim)
        self.layers_time = nn.ModuleList(
            EncoderLayer(hid_dim, heads, pf_dim) for _ in range(constants.DECODER_LAYERS)
        )
        self.fc_onset_time = nn.Linear(hid_dim, 1)
        self.fc_offset_time = nn.Linear(hid_dim, 1)
        self.fc_mpe_time = nn.Linear(hid_dim, 1)
        self.fc_velocity_time = nn.Linear(hid_dim, constants.NUM_VELOCITY)

    def frequency(self, enc: torch.Tensor) -> tuple[torch.Tensor, ...]:
        """enc [count, n_bins, hid_dim] to the notes' vectors [count, n_note, hid_dim] and
        the head "A" onset, offset, frame [count, n_note] and velocity [count, n_note, 128]."""
        count = enc.shape[0]
        positions = torch.arange(constants.NUM_NOTE, device=enc.device)
        midi = self.pos_embedding_freq(positions).unsqueeze(0).repeat(count, 1, 1)
        midi = self.layer_zero_freq(enc, midi)
        for layer in self.layers_freq:
            midi = layer(enc, midi)
        return (
            midi,
            torch.sigmoid(self.fc_onset_freq(midi)).reshape(count, constants.NUM_NOTE),
            torch.sigmoid(self.fc_offset_freq(midi)).reshape(count, constants.NUM_NOTE),
            torch.sigmoid(self.fc_mpe_freq(midi)).reshape(count, constants.NUM_NOTE),
            self.fc_velocity_freq(midi).reshape(count, constants.NUM_NOTE, constants.NUM_VELOCITY),
        )

    def time(self, midi: torch.Tensor) -> tuple[torch.Tensor, ...]:
        """The notes' vectors [count, n_frame, hid_dim] to the head "B" onset, offset, frame
        [count, n_frame] and velocity [count, n_frame, 128]."""
        count = midi.shape[0]
        positions = torch.arange(constants.NUM_FRAME, device=midi.device)
        y = midi * self.scale + self.pos_embedding_time(positions)
        for layer in self.layers_time:
            y = layer(y)
        return (
            torch.sigmoid(self.fc_onset_time(y)).reshape(count, constants.NUM_FRAME),
            torch.sigmoid(self.fc_offset_time(y)).reshape(count, constants.NUM_FRAME),
            torch.sigmoid(self.fc_mpe_time(y)).reshape(count, constants.NUM_FRAME),
            self.fc_velocity_time(y).reshape(count, constants.NUM_FRAME, constants.NUM_VELOCITY),
        )


class Spec2MIDI(nn.Module):
    """Spectrogram window and style vector to piano-roll outputs."""

    def __init__(self) -> None:
        super().__init__()
        self.encoder = Encoder()
        self.decoder = Decoder()
        self.fc_sv = nn.Linear(constants.STYLE_DIM, constants.HID_DIM)
        self.gate_sv = nn.Sequential(
            nn.Linear(constants.HID_DIM, constants.HID_DIM),
            nn.ReLU(),
            nn.Linear(constants.HID_DIM, constants.HID_DIM),
            nn.Sigmoid(),
        )

    def forward(self, spec: torch.Tensor, style: torch.Tensor) -> tuple[torch.Tensor, ...]:
        """spec [1, n_bins, WINDOW] and style [1, 24] to the onset, offset, frame and velocity
        outputs of head "A" and then of head "B": [1, n_frame, n_note], velocity with a last
        dimension of 128."""
        frames = spec.unfold(2, self.encoder.n_proc, 1).permute(0, 2, 1, 3)[0]
        gate_style = self.fc_sv(style).reshape(1, 1, -1)
        notes = []
        outputs_a: list[list[torch.Tensor]] = [[] for _ in range(4)]
        for first in range(0, constants.NUM_FRAME, constants.FRAME_CHUNK):
            enc = self.encoder(frames[first : first + constants.FRAME_CHUNK].contiguous())
            z = self.gate_sv(enc)
            enc = z * enc + (1 - z) * gate_style
            midi, *outputs = self.decoder.frequency(enc)
            del enc, z
            notes.append(midi)
            for parts, output in zip(outputs_a, outputs):
                parts.append(output)
        onset_a, offset_a, mpe_a, velocity_a = (torch.cat(parts).unsqueeze(0) for parts in outputs_a)
        # [n_frame, n_note, hid_dim] to [n_note, n_frame, hid_dim]
        by_note = torch.cat(notes).permute(1, 0, 2).contiguous()
        del notes
        outputs_b: list[list[torch.Tensor]] = [[] for _ in range(4)]
        for first in range(0, constants.NUM_NOTE, constants.NOTE_CHUNK):
            for parts, output in zip(outputs_b, self.decoder.time(by_note[first : first + constants.NOTE_CHUNK])):
                parts.append(output)
        onset_b, offset_b, mpe_b = (torch.cat(parts).permute(1, 0).unsqueeze(0) for parts in outputs_b[:3])
        velocity_b = torch.cat(outputs_b[3]).permute(1, 0, 2).unsqueeze(0)
        return onset_a, offset_a, mpe_a, velocity_a, onset_b, offset_b, mpe_b, velocity_b


def load_model(weights_path: str | Path, device: str) -> Spec2MIDI:
    """The model with the weights of apc.pth, in eval mode on the device.

    The file is read with weights_only, so it cannot run code, and every
    parameter must match: a missing or unexpected one is an error.
    """
    state = torch.load(weights_path, map_location=device, weights_only=True)
    model = Spec2MIDI()
    model.load_state_dict(state, strict=True)
    return model.to(device).eval()
