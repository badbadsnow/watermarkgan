# Copyright (c) 2024 NVIDIA CORPORATION.
#   Licensed under the MIT license.

# Adapted from https://github.com/jik876/hifi-gan under the MIT license.
#   LICENSE is in incl_licenses directory.


import torch
import torch.nn.functional as F
import torch.nn as nn
from librosa.filters import mel as librosa_mel_fn
from scipy import signal

import typing
from typing import Optional, List, Union, Dict, Tuple
from collections import namedtuple
import math
import functools


# Adapted from https://github.com/descriptinc/descript-audio-codec/blob/main/dac/nn/loss.py under the MIT license.
#   LICENSE is in incl_licenses directory.
class MultiScaleMelSpectrogramLoss(nn.Module):
    """Compute distance between mel spectrograms. Can be used
    in a multi-scale way.

    Parameters
    ----------
    n_mels : List[int]
        Number of mels per STFT, by default [5, 10, 20, 40, 80, 160, 320],
    window_lengths : List[int], optional
        Length of each window of each STFT, by default [32, 64, 128, 256, 512, 1024, 2048]
    loss_fn : typing.Callable, optional
        How to compare each loss, by default nn.L1Loss()
    clamp_eps : float, optional
        Clamp on the log magnitude, below, by default 1e-5
    mag_weight : float, optional
        Weight of raw magnitude portion of loss, by default 0.0 (no ampliciation on mag part)
    log_weight : float, optional
        Weight of log magnitude portion of loss, by default 1.0
    pow : float, optional
        Power to raise magnitude to before taking log, by default 1.0
    weight : float, optional
        Weight of this loss, by default 1.0
    match_stride : bool, optional
        Whether to match the stride of convolutional layers, by default False

    Implementation copied from: https://github.com/descriptinc/lyrebird-audiotools/blob/961786aa1a9d628cca0c0486e5885a457fe70c1a/audiotools/metrics/spectral.py
    Additional code copied and modified from https://github.com/descriptinc/audiotools/blob/master/audiotools/core/audio_signal.py
    """

    def __init__(
            self,
            sampling_rate: int,
            n_mels: List[int] = [5, 10, 20, 40, 80, 160, 320],
            window_lengths: List[int] = [32, 64, 128, 256, 512, 1024, 2048],
            loss_fn: typing.Callable = nn.L1Loss(),
            clamp_eps: float = 1e-5,
            mag_weight: float = 0.0,
            log_weight: float = 1.0,
            pow: float = 1.0,
            weight: float = 1.0,
            match_stride: bool = False,
            mel_fmin: List[float] = [0, 0, 0, 0, 0, 0, 0],
            mel_fmax: List[float] = [None, None, None, None, None, None, None],
            window_type: str = "hann",
    ):
        super().__init__()
        self.sampling_rate = sampling_rate

        STFTParams = namedtuple(
            "STFTParams",
            ["window_length", "hop_length", "window_type", "match_stride"],
        )

        self.stft_params = [
            STFTParams(
                window_length=w,
                hop_length=w // 4,
                match_stride=match_stride,
                window_type=window_type,
            )
            for w in window_lengths
        ]
        self.n_mels = n_mels
        self.loss_fn = loss_fn
        self.clamp_eps = clamp_eps
        self.log_weight = log_weight
        self.mag_weight = mag_weight
        self.weight = weight
        self.mel_fmin = mel_fmin
        self.mel_fmax = mel_fmax
        self.pow = pow

    @staticmethod
    @functools.lru_cache(None)
    def get_window(
            window_type,
            window_length,
    ):
        return signal.get_window(window_type, window_length)

    @staticmethod
    @functools.lru_cache(None)
    def get_mel_filters(sr, n_fft, n_mels, fmin, fmax):
        return librosa_mel_fn(sr=sr, n_fft=n_fft, n_mels=n_mels, fmin=fmin, fmax=fmax)

    def mel_spectrogram(
            self,
            wav,
            n_mels,
            fmin,
            fmax,
            window_length,
            hop_length,
            match_stride,
            window_type,
    ):
        """
        Mirrors AudioSignal.mel_spectrogram used by BigVGAN-v2 training from: 
        https://github.com/descriptinc/audiotools/blob/master/audiotools/core/audio_signal.py
        """
        B, C, T = wav.shape

        if match_stride:
            assert (
                    hop_length == window_length // 4
            ), "For match_stride, hop must equal n_fft // 4"
            right_pad = math.ceil(T / hop_length) * hop_length - T
            pad = (window_length - hop_length) // 2
        else:
            right_pad = 0
            pad = 0

        wav = torch.nn.functional.pad(wav, (pad, pad + right_pad), mode="reflect")

        window = self.get_window(window_type, window_length)
        window = torch.from_numpy(window).to(wav.device).float()

        stft = torch.stft(
            wav.reshape(-1, T),
            n_fft=window_length,
            hop_length=hop_length,
            window=window,
            return_complex=True,
            center=True,
        )
        _, nf, nt = stft.shape
        stft = stft.reshape(B, C, nf, nt)
        if match_stride:
            """
            Drop first two and last two frames, which are added, because of padding. Now num_frames * hop_length = num_samples.
            """
            stft = stft[..., 2:-2]
        magnitude = torch.abs(stft)

        nf = magnitude.shape[2]
        mel_basis = self.get_mel_filters(
            self.sampling_rate, 2 * (nf - 1), n_mels, fmin, fmax
        )
        mel_basis = torch.from_numpy(mel_basis).to(wav.device)
        mel_spectrogram = magnitude.transpose(2, -1) @ mel_basis.T
        mel_spectrogram = mel_spectrogram.transpose(-1, 2)

        return mel_spectrogram

    def forward(self, x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        """Computes mel loss between an estimate and a reference
        signal.

        Parameters
        ----------
        x : torch.Tensor
            Estimate signal
        y : torch.Tensor
            Reference signal

        Returns
        -------
        torch.Tensor
            Mel loss.
        """

        loss = 0.0
        for n_mels, fmin, fmax, s in zip(
                self.n_mels, self.mel_fmin, self.mel_fmax, self.stft_params
        ):
            kwargs = {
                "n_mels": n_mels,
                "fmin": fmin,
                "fmax": fmax,
                "window_length": s.window_length,
                "hop_length": s.hop_length,
                "match_stride": s.match_stride,
                "window_type": s.window_type,
            }

            x_mels = self.mel_spectrogram(x, **kwargs)
            y_mels = self.mel_spectrogram(y, **kwargs)
            x_logmels = torch.log(
                x_mels.clamp(min=self.clamp_eps).pow(self.pow)
            ) / torch.log(torch.tensor(10.0))
            y_logmels = torch.log(
                y_mels.clamp(min=self.clamp_eps).pow(self.pow)
            ) / torch.log(torch.tensor(10.0))

            loss += self.log_weight * self.loss_fn(x_logmels, y_logmels)
            loss += self.mag_weight * self.loss_fn(x_logmels, y_logmels)

        return loss


# Loss functions
def feature_loss(
        fmap_r: List[List[torch.Tensor]], fmap_g: List[List[torch.Tensor]]
) -> torch.Tensor:
    loss = 0
    for dr, dg in zip(fmap_r, fmap_g):
        for rl, gl in zip(dr, dg):
            loss += torch.mean(torch.abs(rl - gl))

    return loss * 2  # This equates to lambda=2.0 for the feature matching loss


def discriminator_loss(
        disc_real_outputs: List[torch.Tensor], disc_generated_outputs: List[torch.Tensor]
) -> Tuple[torch.Tensor, List[torch.Tensor], List[torch.Tensor]]:
    loss = 0
    r_losses = []
    g_losses = []
    for dr, dg in zip(disc_real_outputs, disc_generated_outputs):
        # 若dr=1（理想情况），损失为0；若dr=0（误判），损失为1。
        r_loss = torch.mean((1 - dr) ** 2)
        # 若dg=0（理想情况），损失为0；若dg=1（误判），损失为1。
        g_loss = torch.mean(dg ** 2)
        loss += r_loss + g_loss
        r_losses.append(r_loss.item())
        g_losses.append(g_loss.item())

    return loss, r_losses, g_losses


def generator_loss(
        disc_outputs: List[torch.Tensor],
) -> Tuple[torch.Tensor, List[torch.Tensor]]:
    loss = 0
    gen_losses = []
    for dg in disc_outputs:
        l = torch.mean((1 - dg) ** 2)
        gen_losses.append(l)
        loss += l

    return loss, gen_losses


def decoding_loss(message_prob, original_message):
    """
    多比特水印解码损失

    参数:
        message_prob (Tensor): 检测器输出的消息概率 [batch, bits]
        original_message (Tensor): 原始消息 [batch, bits] (0或1)

    返回:
        Tensor: 损失值
    """
    # 确保数值稳定
    message_prob = torch.clamp(message_prob, 1e-8, 1 - 1e-8)

    # 计算每个比特的二元交叉熵
    loss_per_bit = F.binary_cross_entropy(
        message_prob,
        original_message.float(),
        reduction='none'
    )

    # 平均所有比特
    return torch.mean(loss_per_bit)


def tf_loudness_loss(original_audio, watermarked_audio,
                     num_bands=10, window_size=2048,
                     overlap_ratio=0.25, sample_rate=44100):
    """
    时频响度损失 (公式2)

    参数:
        original_audio (Tensor): 原始音频 [batch, 1, samples]
        watermarked_audio (Tensor): 水印音频 [batch, 1, samples]
        num_bands (int): 频带数量 (B)
        window_size (int): 时窗大小 (W)
        overlap_ratio (float): 重叠比例
        sample_rate (int): 采样率

    返回:
        Tensor: 损失值
    """

    original_audio = original_audio.squeeze(1)
    watermarked_audio = watermarked_audio.squeeze(1)

    hop_length = int(window_size * (1 - overlap_ratio))

    # 计算STFT
    S = torch.stft(original_audio.squeeze(1),
                   n_fft=window_size,
                   hop_length=hop_length,
                   return_complex=True)
    D = torch.stft(watermarked_audio.squeeze(1),
                   n_fft=window_size,
                   hop_length=hop_length,
                   return_complex=True)

    # 频率轴
    freq_bins = torch.linspace(0, sample_rate / 2, S.shape[1], device=S.device)

    # 对数频带边界
    band_boundaries = torch.logspace(
        start=1,
        end=torch.log10(torch.tensor(sample_rate / 2)),
        steps=num_bands + 1,
        device=S.device
    )

    total_loss = 0.0

    for b in range(num_bands):
        low_freq = band_boundaries[b]
        high_freq = band_boundaries[b + 1]

        # 频带掩码
        band_mask = (freq_bins >= low_freq) & (freq_bins <= high_freq)
        band_mask = band_mask.view(1, -1, 1).expand_as(S)

        # 提取频带
        S_band = S * band_mask
        D_band = D * band_mask

        # 时域信号
        s_band = torch.istft(S_band, n_fft=window_size, hop_length=hop_length,
                             length=original_audio.size(-1))
        d_band = torch.istft(D_band, n_fft=window_size, hop_length=hop_length,
                             length=watermarked_audio.size(-1))

        # 时窗分割
        s_segments = s_band.unfold(-1, window_size, hop_length)
        d_segments = d_band.unfold(-1, window_size, hop_length)

        # 计算响度
        L_s = torch.sqrt(torch.mean(s_segments ** 2, dim=-1))
        L_phi = torch.sqrt(torch.mean(d_segments ** 2, dim=-1))
        l_b_w = L_phi - L_s

        # 应用softmax加权
        weights = F.softmax(l_b_w, dim=1)
        band_loss = torch.mean(weights * l_b_w)

        total_loss += band_loss

    return abs(total_loss / num_bands)
