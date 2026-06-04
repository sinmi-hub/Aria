"""Silero VAD wrapper + an utterance segmenter built on top of it.

The segmenter consumes the continuous 16kHz float stream coming off the phone
line and emits two kinds of events:
  * ("speech_start",)            — caller began talking (used for barge-in)
  * ("utterance", np.ndarray)    — caller finished a phrase (used for STT)

Turn-end is detected by ``vad_silence_threshold_ms`` of silence after speech.
A short pre-roll is prepended so word onsets are not clipped.
"""
from __future__ import annotations

from collections import deque

import numpy as np
import torch

from config import settings

# NOTE: do NOT call torch.set_num_threads(1) here — Kokoro TTS also runs on torch,
# and globally pinning to one thread makes synthesis many times slower on a
# multi-core CPU. Silero's per-window cost is negligible either way.


class SileroVad:
    """Loads the Silero model once and scores 512-sample 16kHz windows."""

    def __init__(self) -> None:
        from silero_vad import load_silero_vad

        self.model = load_silero_vad()
        self.sr = settings.sample_rate_internal

    def speech_prob(self, window: np.ndarray) -> float:
        tensor = torch.from_numpy(window).float()
        with torch.no_grad():
            return float(self.model(tensor, self.sr).item())

    def reset(self) -> None:
        if hasattr(self.model, "reset_states"):
            self.model.reset_states()


class UtteranceSegmenter:
    """Per-call state machine. Feed it arbitrary-length 16kHz float chunks."""

    def __init__(self, vad: SileroVad) -> None:
        self.vad = vad
        self.win = settings.vad_window_samples
        self.threshold = settings.vad_speech_prob_threshold
        self.silence_windows = settings.silence_windows
        self.min_speech_windows = settings.min_speech_windows
        preroll_windows = max(1, round(settings.vad_preroll_ms / (self.win / 16)))

        # Phase 7: emit ("speech_partial", arr) snapshots during speech so STT can overlap
        # the caller still talking (app/audio/streaming_stt.py). Boundary logic is unchanged.
        self.emit_partials = settings.stt_streaming
        window_ms = self.win / 16
        self.partial_every = max(1, round(settings.stt_partial_interval_ms / window_ms))

        self._buf = np.zeros(0, dtype=np.float32)        # leftover < one window
        self._preroll: deque[np.ndarray] = deque(maxlen=preroll_windows)
        self._speech: list[np.ndarray] = []
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0
        self._since_partial = 0

    def push(self, audio: np.ndarray) -> list[tuple]:
        """Process new audio, returning any events triggered."""
        events: list[tuple] = []
        self._buf = np.concatenate([self._buf, audio])
        while len(self._buf) >= self.win:
            window = self._buf[: self.win]
            self._buf = self._buf[self.win :]
            events.extend(self._process_window(window))
        return events

    def _process_window(self, window: np.ndarray) -> list[tuple]:
        events: list[tuple] = []
        is_speech = self.vad.speech_prob(window) >= self.threshold

        if not self._in_speech:
            self._preroll.append(window)
            if is_speech:
                self._speech_run += 1
                if self._speech_run >= self.min_speech_windows:
                    self._in_speech = True
                    self._silence_run = 0
                    self._speech = list(self._preroll)  # include pre-roll
                    self._preroll.clear()
                    events.append(("speech_start",))
            else:
                self._speech_run = 0
        else:
            self._speech.append(window)
            if is_speech:
                self._silence_run = 0
            else:
                self._silence_run += 1
                if self._silence_run >= self.silence_windows:
                    events.append(("utterance", np.concatenate(self._speech)))
                    self._reset_after_utterance()
                    return events
            if self.emit_partials:
                self._since_partial += 1
                if (self._since_partial >= self.partial_every
                        and len(self._speech) >= self.min_speech_windows):
                    self._since_partial = 0
                    events.append(("speech_partial", np.concatenate(self._speech)))
        return events

    def _reset_after_utterance(self) -> None:
        self._in_speech = False
        self._speech = []
        self._speech_run = 0
        self._silence_run = 0
        self._since_partial = 0
        self._preroll.clear()
