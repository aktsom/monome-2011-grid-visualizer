"""monome 2011 visualizer — entrypoint.

audio (RME UC loopback) -> analysis -> 16x16 grid.
UI at http://localhost:5000
"""
import asyncio
import sys
import threading
import time

if sys.platform == 'win32':
    import ctypes
import os
import numpy as np
import sounddevice as sd
import monome

import state
import server
import tray

# ── constants ────────────────────────────────────────────────────────────────

SAMPLE_RATE    = 48000
BLOCK_SIZE     = 512
RING_SECONDS   = 1.0
RING_SAMPLES   = int(SAMPLE_RATE * RING_SECONDS)
AUDIO_CHANNELS = 2

GRID_W    = 16
GRID_H    = 16
RENDER_HZ = 60

FREQ_MIN = 40.0
FREQ_MAX = 16000.0

ATTACK     = 0.8
DECAY      = 0.15
PEAK_DECAY = 0.08

LISS_DECAY_EVERY = 3   # frames between trail decay steps (~50ms per step)

WF_ENV_DECAY     = 0.80   # envelope decay per scroll step — slower = taller trail
WF_DECAY_EVERY   = 5      # scroll steps between row brightness decay (15→9→4→0)
WF_FLICKER_EVERY = 5      # scroll steps between flicker pattern re-rolls (~250ms)

PULSE_DECAY      = 0.35   # beat pulse only — faster than DECAY for punchy response



HEARTBEAT_TIMEOUT = 30.0

# ── audio ring buffer ─────────────────────────────────────────────────────────

ring     = np.zeros((RING_SAMPLES, AUDIO_CHANNELS), dtype=np.float32)
ring_pos = 0


def audio_callback(indata, frames, time_info, status):
    global ring_pos
    if status:
        print(f"audio status: {status}")
    end = ring_pos + frames
    if end <= RING_SAMPLES:
        ring[ring_pos:end] = indata
    else:
        split = RING_SAMPLES - ring_pos
        ring[ring_pos:] = indata[:split]
        ring[:end - RING_SAMPLES] = indata[split:]
    ring_pos = end % RING_SAMPLES


def latest_block(n):
    if ring_pos >= n:
        return ring[ring_pos - n:ring_pos]
    return np.concatenate([ring[ring_pos - n:], ring[:ring_pos]])


# ── precomputed analysis tables ───────────────────────────────────────────────

_edges     = np.logspace(np.log10(FREQ_MIN), np.log10(FREQ_MAX), GRID_W + 1)

# spectrum analyzer — 2048-point FFT
_fft_freqs = np.fft.rfftfreq(2048, d=1.0 / SAMPLE_RATE)
_band_lo   = np.searchsorted(_fft_freqs, _edges[:-1]).clip(1, len(_fft_freqs) - 1)
_band_hi   = np.maximum(np.searchsorted(_fft_freqs, _edges[1:]), _band_lo + 1).clip(1, len(_fft_freqs))

# 8-band spectrum (preset 06) — same log range, each band 2 cols wide
_edges_8   = np.logspace(np.log10(FREQ_MIN), np.log10(FREQ_MAX), 9)
_band_lo_8 = np.searchsorted(_fft_freqs, _edges_8[:-1]).clip(1, len(_fft_freqs) - 1)
_band_hi_8 = np.maximum(np.searchsorted(_fft_freqs, _edges_8[1:]), _band_lo_8 + 1).clip(1, len(_fft_freqs))

# beat pulse — three concentric frequency bands
_bass_lo  = int(np.searchsorted(_fft_freqs,    40.0))
_bass_hi  = int(np.searchsorted(_fft_freqs,   250.0))
_mid_lo   = int(np.searchsorted(_fft_freqs,   250.0))
_mid_hi   = int(np.searchsorted(_fft_freqs,  4000.0))
_high_lo  = int(np.searchsorted(_fft_freqs,  4000.0))
_high_hi  = int(np.searchsorted(_fft_freqs, 16000.0))


# ── visualizer ────────────────────────────────────────────────────────────────

class Visualizer(monome.GridApp):
    def __init__(self):
        super().__init__()
        self.framebuffer      = np.zeros((GRID_H, GRID_W), dtype=np.uint8)
        self._smoothed        = np.zeros(GRID_W, dtype=np.float32)
        self._peaks           = np.zeros(GRID_W, dtype=np.float32)
        self._smoothed_8      = np.zeros(8, dtype=np.float32)
        self._peaks_8         = np.zeros(8, dtype=np.float32)
        self._render_task     = None
        self._liss_decay_ctr  = 0
        self._wave_scroll_ctr = 0
        self._wf_envelope     = 0.0
        self._wf_decay_ctr    = 0
        self._wf_flicker_ctr  = 0
        self._wf_flicker_mask = {}   # {trail_index: set of dark cols}
        self._wf_ember_mask   = {}   # {trail_index: single lit col} for rows 4-6
        self._wf_rows         = []   # [[half_cols, brightness], ...] newest first
        self._ctr_rows        = []   # same structure for 07 waveform
        self._ctr_decay_ctr   = 0
        self._bass_level      = 0.0   # smoothed bass band level [0, 1]
        self._mid_level       = 0.0   # smoothed mid band level  [0, 1]
        self._high_level      = 0.0   # smoothed high band level [0, 1]
        self._dist_map        = None  # precomputed per-cell distance from centre

    def on_grid_ready(self):
        gw, gh = self.grid.width, self.grid.height
        print(f"grid connected: {gw}x{gh}")
        state.active_grid = 'connected'
        self.framebuffer  = np.zeros((gh, gw), dtype=np.uint8)
        self._smoothed    = np.zeros(gw, dtype=np.float32)
        self._peaks       = np.zeros(gw, dtype=np.float32)
        self._smoothed_8  = np.zeros(8, dtype=np.float32)
        self._peaks_8     = np.zeros(8, dtype=np.float32)
        self._wave_scroll_ctr = 0
        self._wf_envelope    = 0.0
        self._wf_decay_ctr   = 0
        self._wf_flicker_ctr  = 0
        self._wf_flicker_mask = {}
        self._wf_ember_mask   = {}
        self._wf_rows         = []
        self._ctr_rows        = []
        self._ctr_decay_ctr   = 0
        self._bass_level     = 0.0
        self._mid_level      = 0.0
        self._high_level     = 0.0
        cx, cy = (gw - 1) / 2.0, (gh - 1) / 2.0
        ys, xs = np.mgrid[0:gh, 0:gw]
        self._dist_map = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2).astype(np.float32)
        if self._render_task and not self._render_task.done():
            self._render_task.cancel()
        self._render_task = asyncio.ensure_future(render_loop(self))

    def on_grid_disconnect(self):
        print("grid disconnected")
        state.active_grid = None

    def on_grid_key(self, x, y, s):
        pass

    def render_spectrum(self):
        gh, gw = self.framebuffer.shape
        block    = latest_block(2048)
        mono     = block.mean(axis=1)
        spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))

        levels = np.array([spectrum[_band_lo[i]:_band_hi[i]].mean() for i in range(gw)])
        levels = np.log1p(levels) * (state.gain / 2.0) * np.array(state.band_trim[state.preset], dtype=np.float32)
        levels = np.clip(levels, 0, gh)

        rising         = levels > self._smoothed
        self._smoothed = np.where(rising,
            ATTACK * levels + (1 - ATTACK) * self._smoothed,
            DECAY  * levels + (1 - DECAY)  * self._smoothed)
        levels = self._smoothed

        self._peaks = np.where(levels > self._peaks, levels, self._peaks - PEAK_DECAY)
        self._peaks = np.clip(self._peaks, 0, gh)

        roles = state.led_roles[state.preset]
        cb    = state.col_brightness[state.preset]
        bar_v = roles['bar']
        tip_v = roles['tip']

        self.framebuffer.fill(0)
        for x, lvl in enumerate(levels):
            h     = int(lvl)
            scale = cb[x]
            for y in range(h):
                v = bar_v if y < h - 1 else tip_v
                self.framebuffer[gh - 1 - y, x] = int(v * scale)
            if state.preset == '06 spectrum peak':
                ph = int(self._peaks[x])
                if ph > h and ph > 0:
                    self.framebuffer[gh - 1 - ph, x] = int(roles['peak'] * scale)

    def render_spectrum_8(self):
        gh, gw = self.framebuffer.shape
        block    = latest_block(2048)
        mono     = block.mean(axis=1)
        spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))

        levels = np.array([spectrum[_band_lo_8[i]:_band_hi_8[i]].mean() for i in range(8)])
        levels = np.log1p(levels) * (state.gain / 2.0) * np.array(state.band_trim['02 spectrum 8'], dtype=np.float32)
        levels = np.clip(levels, 0, gh)

        rising           = levels > self._smoothed_8
        self._smoothed_8 = np.where(rising,
            ATTACK * levels + (1 - ATTACK) * self._smoothed_8,
            DECAY  * levels + (1 - DECAY)  * self._smoothed_8)
        levels = self._smoothed_8

        roles = state.led_roles['02 spectrum 8']
        cb    = state.col_brightness['02 spectrum 8']
        bar_v = roles['bar']
        tip_v = roles['tip']

        self.framebuffer.fill(0)
        for i, lvl in enumerate(levels):
            h     = int(lvl)
            x0    = i * 2
            scale = cb[i]
            for y in range(h):
                v  = bar_v if y < h - 1 else tip_v
                fv = int(v * scale)
                self.framebuffer[gh - 1 - y, x0]     = fv
                self.framebuffer[gh - 1 - y, x0 + 1] = fv

    def render_waveform(self):
        gh, gw  = self.framebuffer.shape
        center  = gw // 2           # col 8 (0-indexed); bar grows left and right
        n_rows  = gh - 3            # 13 trail rows occupying bottom (rows 4–16)

        self._wave_scroll_ctr += 1
        if self._wave_scroll_ctr < 3:
            return
        self._wave_scroll_ctr = 0

        # decay row history every N scroll steps
        _BD = {15: 9, 9: 4, 4: 0, 0: 0}
        self._wf_decay_ctr += 1
        if self._wf_decay_ctr >= WF_DECAY_EVERY:
            self._wf_decay_ctr = 0
            for entry in self._wf_rows:
                entry[1] = _BD[entry[1]]

        # instant attack / slow decay envelope — mono peak, gain-scaled
        block = latest_block(256)
        scale = state.gain / 2.0
        peak  = min(float(np.max(np.abs(block))) * scale, 2.0)
        self._wf_envelope = max(peak, self._wf_envelope * WF_ENV_DECAY)
        env = self._wf_envelope

        half_cols = int(np.clip(round(env * center), 0, center))
        if   env >= 1.10: bright = 15
        elif env >= 0.44: bright = 9
        elif env >= 0.14: bright = 4
        else:             bright = 0

        # push newest row to front, trim tail
        self._wf_rows.insert(0, [half_cols, bright])
        if len(self._wf_rows) > n_rows:
            self._wf_rows.pop()

        # re-roll flicker pattern every N steps — spots persist between re-rolls
        self._wf_flicker_ctr += 1
        if self._wf_flicker_ctr >= WF_FLICKER_EVERY:
            self._wf_flicker_ctr = 0
            fp = min(0.15, env * 0.12)
            self._wf_flicker_mask = {
                i: {c for c in range(7, 9) if np.random.random() < fp}
                for i in range(4, len(self._wf_rows))  # row 5 upward only
            }
            # ember LEDs for visual rows 4-6 (trail indices 10-12)
            self._wf_ember_mask = {}
            for ei in (10, 11, 12):
                if ei >= len(self._wf_rows):
                    continue
                if np.random.random() < min(0.7, env * 0.6):
                    hc_e, _ = self._wf_rows[ei]
                    ws_e    = max(0.15, 1.0 - ei / n_rows)
                    dc_e    = max(1, int(hc_e * ws_e))
                    col     = center - dc_e + np.random.randint(0, dc_e * 2)
                    self._wf_ember_mask[ei] = int(np.clip(col, 0, gw - 1))

        # render — row 1 (index 0) always dark, rows 2–3 fire tip, rows 4–16 trail
        # width tapers linearly from full at bottom to ~15% near top
        self.framebuffer.fill(0)

        for i, (hc, b) in enumerate(self._wf_rows):
            row     = gh - 1 - i                   # i=0 → index 15 (bottom row)
            w_scale = max(0.15, 1.0 - i / n_rows)
            dc      = max(1, int(hc * w_scale)) if (hc > 0 and b > 0) else 0
            if dc == 0 or b == 0:
                continue
            self.framebuffer[row, center - dc : center + dc] = b
            # bright centre zone — bottom 5 rows only, inner half at full brightness
            if i < 5:
                inner_dc = max(1, dc // 2)
                self.framebuffer[row, center - inner_dc : center + inner_dc] = 15
            # dim outer edge LEDs one brightness step (15→9, 9→4, 4→0)
            if dc >= 2:
                edge_b = {15: 9, 9: 4, 4: 0, 0: 0}[b]
                if edge_b > 0:
                    self.framebuffer[row, center - dc]     = edge_b
                    self.framebuffer[row, center + dc - 1] = edge_b
            # flicker: row 5 upward only (i >= 4)
            if i >= 4:
                for c in self._wf_flicker_mask.get(i, ()):
                    if center - dc <= c < center + dc:
                        self.framebuffer[row, c] = 0
            # ember: single overwritten LED for visual rows 4-6 (i=10,11,12)
            if i in self._wf_ember_mask:
                self.framebuffer[row, self._wf_ember_mask[i]] = 4

        # fire tip — only when trail has reached the top AND scaled by envelope
        # loud = tip fires often; quiet = tip rarely appears
        top_lit = any(b > 0 for _, b in self._wf_rows[-(n_rows // 2):]) \
                  if len(self._wf_rows) >= n_rows // 2 else False
        if top_lit:
            prob3 = min(1.0, env / 0.55)   # row 3: full rate at bright threshold
            prob2 = min(1.0, env / 1.4)    # row 2: appears on strong hits only
            if np.random.random() < prob3:
                fire_col = (center - 1) + np.random.randint(0, 2)
                self.framebuffer[2, fire_col] = 4
            if np.random.random() < prob2:
                self.framebuffer[1, center - 1] = 4

    def render_pulse(self):
        # ── three concentric frequency-band level meters ───────────────────────
        # inner  zone (dist < 3.5) → bass   40–250 Hz
        # middle zone (3.5–7.0)   → mids  250–4000 Hz
        # outer  zone (7.0–10.6)  → highs 4000–16000 Hz
        block    = latest_block(2048)
        mono     = block.mean(axis=1)
        spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))

        # per-band sensitivity: bass scaled down (dense energy), highs boosted (sparse)
        norm     = state.gain / (GRID_H * 3.0)
        bass_lvl = float(np.clip(np.log1p(spectrum[_bass_lo:_bass_hi].mean()) * norm * 0.7, 0.0, 1.0))
        mid_lvl  = float(np.clip(np.log1p(spectrum[_mid_lo:_mid_hi].mean())   * norm * 2.0, 0.0, 1.0))
        high_lvl = float(np.clip(np.log1p(spectrum[_high_lo:_high_hi].mean()) * norm * 3.0, 0.0, 1.0))

        # fast attack, fast decay — punchy response that drops between beats
        self._bass_level = (ATTACK      * bass_lvl + (1 - ATTACK)      * self._bass_level
                            if bass_lvl > self._bass_level
                            else PULSE_DECAY * bass_lvl + (1 - PULSE_DECAY) * self._bass_level)
        self._mid_level  = (ATTACK      * mid_lvl  + (1 - ATTACK)      * self._mid_level
                            if mid_lvl  > self._mid_level
                            else PULSE_DECAY * mid_lvl  + (1 - PULSE_DECAY) * self._mid_level)
        self._high_level = (ATTACK      * high_lvl + (1 - ATTACK)      * self._high_level
                            if high_lvl > self._high_level
                            else PULSE_DECAY * high_lvl + (1 - PULSE_DECAY) * self._high_level)

        INNER_R = 3.5
        MID_R   = 7.0
        OUTER_R = 10.6   # sqrt(7.5² + 7.5²) — grid corner distance

        bass_edge = self._bass_level * INNER_R
        mid_edge  = INNER_R + self._mid_level  * (MID_R   - INNER_R)
        high_edge = MID_R   + self._high_level * (OUTER_R - MID_R)

        dm = self._dist_map
        self.framebuffer.fill(0)

        # bass inner zone — fills from centre outward
        bass_lit = dm < bass_edge
        self.framebuffer[bass_lit] = 15
        self.framebuffer[bass_lit & (dm >= bass_edge - 0.8)] = 9

        # mid ring — fills from inner boundary outward
        mid_lit = (dm >= INNER_R) & (dm < mid_edge)
        self.framebuffer[mid_lit] = 15
        self.framebuffer[mid_lit & (dm >= mid_edge - 0.8)] = 9

        # high outer ring — fills from mid boundary outward
        high_lit = (dm >= MID_R) & (dm < high_edge)
        self.framebuffer[high_lit] = 15
        self.framebuffer[high_lit & (dm >= high_edge - 0.8)] = 9

    def render_lissajous(self):
        gh, gw = self.framebuffer.shape
        # decay trail: 15 → 9 → 4 → 0
        self._liss_decay_ctr += 1
        if self._liss_decay_ctr >= LISS_DECAY_EVERY:
            self._liss_decay_ctr = 0
            fb = self.framebuffer
            self.framebuffer = np.where(fb >= 12, 9,
                               np.where(fb >= 7,  4, 0)).astype(np.uint8)

        # 64 subsampled points from latest 512 samples
        block = latest_block(512)[::8]
        scale = state.gain / 4.0
        L = np.clip(block[:, 0] * scale, -1.0, 1.0)
        R = np.clip(block[:, 1] * scale, -1.0, 1.0)

        x = np.clip(((L + 1.0) * 0.5 * (gw - 1) + 0.5).astype(int), 0, gw - 1)
        y = np.clip(((R + 1.0) * 0.5 * (gh - 1) + 0.5).astype(int), 0, gh - 1)

        self.framebuffer[gh - 1 - y, x] = 15

    def render_centre(self):
        gh, gw    = self.framebuffer.shape
        crow      = gh // 2      # 8 — first row of bottom half
        ccol      = gw // 2      # 8 — horizontal centre
        n_entries = gh // 2      # 8 entries spread from centre to edges

        self._wave_scroll_ctr += 1
        if self._wave_scroll_ctr < 3:
            return
        self._wave_scroll_ctr = 0

        # decay history every N scroll steps
        _BD = {15: 9, 9: 4, 4: 0, 0: 0}
        self._ctr_decay_ctr += 1
        if self._ctr_decay_ctr >= WF_DECAY_EVERY:
            self._ctr_decay_ctr = 0
            for entry in self._ctr_rows:
                entry[1] = _BD[entry[1]]

        # envelope — shared with flame preset
        block = latest_block(256)
        scale = state.gain / 2.0
        peak  = min(float(np.max(np.abs(block))) * scale, 2.0)
        self._wf_envelope = max(peak, self._wf_envelope * WF_ENV_DECAY)
        env = self._wf_envelope

        half_cols = int(np.clip(round(env * ccol), 0, ccol))
        if   env >= 1.10: bright = 15
        elif env >= 0.44: bright = 9
        elif env >= 0.14: bright = 4
        else:             bright = 0

        # push newest entry, trim
        self._ctr_rows.insert(0, [half_cols, bright])
        if len(self._ctr_rows) > n_entries:
            self._ctr_rows.pop()

        # render symmetrically — entry i drawn at two rows equidistant from centre
        self.framebuffer.fill(0)
        for i, (hc, b) in enumerate(self._ctr_rows):
            row_a   = crow - 1 - i    # upward:   7, 6, 5, 4, 3, 2, 1, 0
            row_b   = crow + i        # downward: 8, 9, 10, 11, 12, 13, 14, 15
            w_scale = max(0.15, 1.0 - i / n_entries)
            dc      = max(1, int(hc * w_scale)) if (hc > 0 and b > 0) else 0
            if dc == 0 or b == 0:
                continue
            for row in (row_a, row_b):
                if 0 <= row < gh:
                    self.framebuffer[row, ccol - dc : ccol + dc] = b
                    if dc >= 2:
                        edge_b = {15: 9, 9: 4, 4: 0, 0: 0}[b]
                        if edge_b > 0:
                            self.framebuffer[row, ccol - dc]     = edge_b
                            self.framebuffer[row, ccol + dc - 1] = edge_b

    def flush(self):
        gh, gw = self.framebuffer.shape
        for qy in range(0, gh, 8):
            for qx in range(0, gw, 8):
                data = [
                    [int(self.framebuffer[qy + r, qx + c]) for c in range(8)]
                    for r in range(8)
                ]
                self.grid.led_level_map(qx, qy, data)


async def render_loop(viz):
    period = 1.0 / RENDER_HZ
    while True:
        if state.preset in ('01 spectrum', '06 spectrum peak'):
            viz.render_spectrum()
        elif state.preset == '02 spectrum 8':
            viz.render_spectrum_8()
        elif state.preset == '03 flame':
            viz.render_waveform()
        elif state.preset == '04 lissajous':
            viz.render_lissajous()
        elif state.preset == '05 rings':
            viz.render_pulse()
        elif state.preset == '07 ripple':
            viz.render_centre()
        viz.flush()
        await asyncio.sleep(period)


# ── audio stream helpers ──────────────────────────────────────────────────────

def make_stream(device):
    return sd.InputStream(
        device=device,
        channels=AUDIO_CHANNELS,
        samplerate=SAMPLE_RATE,
        blocksize=BLOCK_SIZE,
        callback=audio_callback,
        dtype='float32',
    )


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    state.load_config()

    flask_thread = threading.Thread(target=server.run, daemon=True)
    flask_thread.start()
    print("UI -> http://localhost:5000")

    stop_event = threading.Event()
    loop = asyncio.get_event_loop()
    tray_thread = threading.Thread(target=tray.run_tray, args=(stop_event, loop), daemon=True)
    tray_thread.start()

    viz = Visualizer()
    serialosc = monome.SerialOsc()

    def on_device_added(id, type, port):
        state.add_grid(id, port, type)
        print(f"grid found: {id} ({type}) on port {port}")
        if state.active_grid is None:
            state.active_grid = id
            asyncio.ensure_future(viz.grid.connect('127.0.0.1', port))

    serialosc.device_added_event.add_handler(on_device_added)
    await serialosc.connect()

    device = state.audio_device
    try:
        stream = make_stream(device)
        stream.start()
        print(f"audio -> device {device}")
    except Exception as e:
        print(f"audio -> device {device} failed ({e}), falling back to system default")
        state.audio_device = None
        stream = make_stream(None)
        stream.start()
        print("audio -> using system default input")

    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.5)

            # audio device change requested from UI
            if state.restart_audio:
                state.restart_audio = False
                print(f"audio -> switching to device {state.audio_device}")
                try:
                    stream.stop()
                    stream.close()
                    stream = make_stream(state.audio_device)
                    stream.start()
                    print(f"audio -> now on device {state.audio_device}")
                    state.save_config()
                except Exception as e:
                    print(f"audio -> switch failed: {e}")

            # grid reconnect requested from UI
            if state.reconnect_grid is not None:
                port = state.reconnect_grid
                state.reconnect_grid = None
                await viz.grid.connect('127.0.0.1', port)

            # heartbeat watchdog
            timed_out = time.time() - state.last_heartbeat > HEARTBEAT_TIMEOUT
            task_running = viz._render_task and not viz._render_task.done()

            if timed_out and task_running:
                viz._render_task.cancel()
                viz._render_task = None
                viz.framebuffer.fill(0)
                try:
                    viz.flush()
                except Exception:
                    pass
                print("heartbeat timeout — grid blanked")

            elif not timed_out and not task_running and state.active_grid is not None:
                viz._render_task = asyncio.ensure_future(render_loop(viz))
                print("heartbeat resumed — render loop restarted")

    finally:
        if viz._render_task and not viz._render_task.done():
            viz._render_task.cancel()
        viz.framebuffer.fill(0)
        try:
            viz.flush()
        except Exception:
            pass
        stream.stop()
        stream.close()
        print('bye')
        os._exit(0)


def _single_instance():
    """Prevent duplicate instances. Returns a handle/socket to keep alive."""
    if sys.platform == 'win32':
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, 'monome_visualizer_instance')
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            sys.exit(0)
        return handle
    else:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(('127.0.0.1', 47823))
            return sock
        except OSError:
            sys.exit(0)


if __name__ == '__main__':
    _instance = _single_instance()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
