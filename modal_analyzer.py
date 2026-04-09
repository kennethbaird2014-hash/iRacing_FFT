"""
modal_analyzer.py — Pure numpy/scipy modal analysis for iRacing FFB signals.

No Qt dependencies. All methods are stateless (arrays in, arrays out).
Designed to be instantiated once and called from any thread.
"""

import numpy as np
from scipy import signal as scipy_signal
from typing import List, Dict, Optional, Tuple

SAMPLE_RATE = 360.0   # must match main app


class ModalAnalyzer:
    def __init__(self, sample_rate: float = SAMPLE_RATE):
        self.fs = sample_rate

    # ── 1. Cepstrum ───────────────────────────────────────────────────────────
    def compute_cepstrum(self, mag: np.ndarray, freqs: np.ndarray
                         ) -> Tuple[np.ndarray, np.ndarray, Optional[float]]:
        """
        Real power cepstrum:  IFFT( log( |mag|² ) )
        A harmonic family at f0, 2f0, 3f0 … collapses to one peak at
        quefrency = 1/f0, regardless of how many harmonics are present.

        Returns
        -------
        cepstrum  : 1-D array — cepstrum values
        quefrency : 1-D array — time-lag axis in seconds
        fund_hz   : float | None — detected fundamental (None if not found)
        """
        log_power = np.log(np.maximum(mag ** 2, 1e-10))
        cepstrum  = np.abs(np.fft.irfft(log_power))

        f_max     = freqs[-1] if len(freqs) > 0 and freqs[-1] > 0 else self.fs / 2
        quef_step = 1.0 / (2.0 * f_max)
        quefrency = np.arange(len(cepstrum)) * quef_step

        # Search only between 2 Hz and 100 Hz, i.e. quefrency 10–500 ms
        min_q = 0.01    # 100 Hz max fundamental
        max_q = 0.50    # 2 Hz min fundamental
        mask  = (quefrency >= min_q) & (quefrency <= max_q)
        fund_hz = None
        if mask.any():
            search = cepstrum[mask]
            # Weight to favour longer quefrencies (lower fundamentals)
            # but only pick a peak that stands out from local neighbours
            idx      = int(np.argmax(search))
            full_idx = int(np.where(mask)[0][idx])
            peak_val = cepstrum[full_idx]
            # Reject if peak is not at least 20% above local median
            local    = cepstrum[mask]
            if peak_val > np.median(local) * 1.2:
                q_val   = quefrency[full_idx]
                fund_hz = 1.0 / q_val if q_val > 0 else None

        return cepstrum, quefrency, fund_hz

    # ── 2. Harmonic Product Spectrum ──────────────────────────────────────────
    def compute_hps(self, mag: np.ndarray, n_harmonics: int = 4
                    ) -> Tuple[np.ndarray, int]:
        """
        Multiply the spectrum with decimated (downsampled) copies of itself.
        Harmonically-related peaks reinforce; broadband noise cancels.

        Returns (hps_array, fund_bin_index)
        """
        product = mag.copy().astype(float)
        for h in range(2, n_harmonics + 1):
            ds = mag[::h]
            length = min(len(product), len(ds))
            product[:length] *= ds[:length]
        product[:3] = 0.0          # suppress DC
        fund_idx = int(np.argmax(product))
        return product, fund_idx

    # ── 3. Half-power Q estimation ────────────────────────────────────────────
    def compute_half_power_q(self, mag: np.ndarray, freqs: np.ndarray,
                              peak_freq: float) -> Optional[Dict]:
        """
        Find Q = f_peak / bandwidth and ζ = 1/(2Q) from the −3 dB bandwidth.
        Returns dict {q, damping, f1, f2, bandwidth} or None on failure.
        """
        if len(freqs) < 5 or peak_freq <= 0:
            return None

        peak_idx = int(np.argmin(np.abs(freqs - peak_freq)))
        half_val = mag[peak_idx] / np.sqrt(2.0)

        f1 = freqs[0]
        for i in range(peak_idx - 1, 0, -1):
            if mag[i] <= half_val:
                t  = (half_val - mag[i]) / (mag[i + 1] - mag[i] + 1e-12)
                f1 = freqs[i] + t * (freqs[i + 1] - freqs[i])
                break

        f2 = freqs[-1]
        for i in range(peak_idx + 1, len(mag)):
            if mag[i] <= half_val:
                t  = (half_val - mag[i - 1]) / (mag[i] - mag[i - 1] + 1e-12)
                f2 = freqs[i - 1] + t * (freqs[i] - freqs[i - 1])
                break

        bw = f2 - f1
        if bw > 0:
            q    = peak_freq / bw
            zeta = 1.0 / (2.0 * q)
            return {'q': q, 'damping': zeta, 'f1': f1, 'f2': f2, 'bandwidth': bw}
        return None

    # ── 4. Autocorrelation + damping ──────────────────────────────────────────
    def compute_acf(self, signal: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Normalised ACF via FFT.  Returns (acf, lags_seconds)."""
        x = signal - np.mean(signal)
        n = len(x)
        if n < 8:
            return np.zeros(1), np.zeros(1)
        fft_x = np.fft.rfft(x, n=2 * n)
        acf   = np.fft.irfft(fft_x * np.conj(fft_x))[:n].real
        acf  /= (acf[0] + 1e-12)
        lags  = np.arange(n) / self.fs
        return acf, lags

    def estimate_damping_from_acf(self, acf: np.ndarray,
                                   dominant_freq: float) -> Optional[float]:
        """ζ ≈ mean of -log(r_k) / (2πk) over successive ACF peaks."""
        if dominant_freq <= 0 or len(acf) < 4:
            return None
        period = self.fs / dominant_freq
        zetas  = []
        k = 1
        while True:
            idx = int(round(k * period))
            if idx >= len(acf):
                break
            rk = abs(acf[idx])
            if rk < 0.01:
                break
            z = -np.log(rk + 1e-12) / (2 * np.pi * k)
            if 0 < z < 1:
                zetas.append(z)
            k += 1
        return float(np.mean(zetas)) if zetas else None

    # ── 5. Spectral Kurtosis ──────────────────────────────────────────────────
    def compute_spectral_kurtosis(self, frames: np.ndarray) -> np.ndarray:
        """
        SK = E[|X|⁴] / E[|X|²]² − 2   computed across N short-time frames.
        High SK → impulsive content at that frequency.
        frames shape: (N_windows, N_bins)
        """
        if frames.ndim != 2 or frames.shape[0] < 4:
            return np.zeros(max(frames.shape[-1], 1))
        power = np.abs(frames) ** 2
        mu2   = np.mean(power, axis=0)
        mu4   = np.mean(power ** 2, axis=0)
        return mu4 / (mu2 ** 2 + 1e-20) - 2.0

    # ── 6. Order spectrum ─────────────────────────────────────────────────────
    def compute_order_spectrum(self, mag: np.ndarray, freqs: np.ndarray,
                                f_rot_hz: float,
                                max_order: float = 20.0
                                ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Resample magnitude spectrum onto a uniform order axis.
        Returns (orders, mag_on_orders).  orders[i] = freqs[i] / f_rot_hz
        """
        if f_rot_hz <= 0 or len(freqs) < 2:
            return np.array([]), np.array([])
        order_axis = np.linspace(0, max_order, 512)
        freq_axis  = order_axis * f_rot_hz
        valid      = freq_axis <= freqs[-1]
        mag_orders = np.zeros_like(order_axis)
        mag_orders[valid] = np.interp(freq_axis[valid], freqs, mag)
        return order_axis, mag_orders

    # ── 7. SSA (run off-thread — expensive) ───────────────────────────────────
    def compute_ssa(self, signal: np.ndarray, window_len: int,
                    n_components: int) -> List[Dict]:
        """
        Singular Spectrum Analysis via Hankel-matrix SVD.
        Decomposes the signal into quasi-periodic components.
        Returns list of {signal, frequency, strength}.

        NOTE: O(window_len² × k) — run in a background QThread.
        """
        n = len(signal)
        if n < window_len * 2 or window_len < 4:
            return []

        k = n - window_len + 1
        # Build trajectory (Hankel) matrix without extra memory copy
        X = np.lib.stride_tricks.as_strided(
            signal,
            shape=(window_len, k),
            strides=(signal.strides[0], signal.strides[0]),
        ).copy()   # copy needed so SVD doesn't corrupt original

        U, s, Vt = np.linalg.svd(X, full_matrices=False)
        n_comp    = min(n_components, len(s))
        results   = []

        for i in range(n_comp):
            Xi   = s[i] * np.outer(U[:, i], Vt[i, :])
            comp = np.zeros(n)
            cnt  = np.zeros(n)
            for r in range(window_len):
                comp[r: r + k] += Xi[r]
                cnt [r: r + k] += 1
            comp /= np.maximum(cnt, 1)

            sp      = np.abs(np.fft.rfft(comp))
            sp[0]   = 0
            peak_f  = int(np.argmax(sp)) * self.fs / n
            results.append({'signal': comp, 'frequency': peak_f, 'strength': float(s[i])})

        return results
