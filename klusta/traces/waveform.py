# -*- coding: utf-8 -*-

"""Waveform extraction."""

#------------------------------------------------------------------------------
# Imports
#------------------------------------------------------------------------------

import logging

import numpy as np
from scipy.interpolate import interp1d

from ..utils import Bunch, _pad

logger = logging.getLogger(__name__)


#------------------------------------------------------------------------------
# Waveform extractor from a connected component
#------------------------------------------------------------------------------

def _get_padded(data, start, end):
    """Return `data[start:end]` filling in with zeros outside array bounds

    Assumes that either `start<0` or `end>len(data)` but not both.

    """
    if start < 0 and end >= data.shape[0]:
        raise RuntimeError()
    if start < 0:
        start_zeros = np.zeros((-start, data.shape[1]),
                               dtype=data.dtype)
        return np.vstack((start_zeros, data[:end]))
    elif end > data.shape[0]:
        end_zeros = np.zeros((end - data.shape[0], data.shape[1]),
                             dtype=data.dtype)
        return np.vstack((data[start:], end_zeros))
    else:
        return data[start:end]


class WaveformExtractor(object):
    """Extract waveforms after data filtering and spike detection."""
    def __init__(self,
                 extract_before=None,
                 extract_after=None,
                 weight_power=None,
                 thresholds=None,
                 channels_per_group=None,
                 ):
        self._extract_before = extract_before
        self._extract_after = extract_after
        self._weight_power = weight_power if weight_power is not None else 1.
        self._thresholds = thresholds or {}
        self._channels_per_group = channels_per_group
        # mapping channel => channels in the shank
        self._dep_channels = {i: channels
                              for channels in channels_per_group.values()
                              for i in channels}
        self._channel_groups = {i: g
                                for g, channels in channels_per_group.items()
                                for i in channels}

    def _component(self, component, data=None, n_samples=None):
        comp_s = component[:, 0]  # shape: (component_size,)
        comp_ch = component[:, 1]  # shape: (component_size,)
        channel = comp_ch[0]
        if channel not in self._dep_channels:
            raise RuntimeError("Channel `{}` appears to be dead and should "
                               "have been excluded from the threshold "
                               "crossings.".format(channel))
        channels = self._dep_channels[channel]
        group = self._channel_groups[comp_ch[0]]

        # Get the temporal window around the waveform.
        s_min, s_max = (comp_s.min() - 3), (comp_s.max() + 4)
        s_min = max(s_min, 0)
        s_max = min(s_max, n_samples)
        assert s_min < s_max

        return Bunch(comp_s=comp_s,
                     comp_ch=comp_ch,
                     s_min=s_min,
                     s_max=s_max,
                     channels=channels,
                     group=group,
                     )

    def _normalize(self, x):
        x = np.asarray(x)
        tw = self._thresholds['weak']
        ts = self._thresholds['strong']
        return np.clip((x - tw) / (ts - tw), 0, 1)

    def _comp_wave(self, data_t, comp):
        comp_s, comp_ch = comp.comp_s, comp.comp_ch
        s_min, s_max = comp.s_min, comp.s_max
        nc = data_t.shape[1]
        # Data on weak threshold crossings. shape: (some_length, nc)
        wave = np.zeros((s_max - s_min, nc), dtype=data_t.dtype)
        # The sample where the peak is reached, on each channel.
        wave[comp_s - s_min, comp_ch] = data_t[comp_s, comp_ch]
        return wave

    def masks(self, data_t, wave, comp):
        nc = data_t.shape[1]
        channels = comp.channels
        comp_ch = comp.comp_ch
        s_min = comp.s_min

        # Binary mask. shape: (nc,)
        masks_bin = np.zeros(nc, dtype=bool)
        masks_bin[np.unique(comp_ch)] = 1

        # Find the peaks (relative to the start of the chunk). shape: (nc,)
        peaks = np.argmax(wave, axis=0) + s_min
        # Peak values on each channel. shape: (nc,)
        peaks_values = data_t[peaks, np.arange(0, nc)] * masks_bin

        # Compute the float masks.
        masks_float = self._normalize(peaks_values)
        # Keep shank channels.
        masks_float = masks_float[channels]
        return masks_float

    def spike_sample_aligned(self, wave, comp):
        s_min, s_max = comp.s_min, comp.s_max
        # Compute the fractional peak.
        wave_n = self._normalize(wave)
        wave_n_p = np.power(wave_n, self._weight_power)
        u = np.arange(s_max - s_min)[:, np.newaxis]
        # Spike aligned time relative to the start of the chunk.
        s_aligned = np.sum(wave_n_p * u) / np.sum(wave_n_p) + s_min
        return s_aligned

    def extract(self, data, s_aligned, channels=None):
        s = int(s_aligned)
        # Get block of given size around peak sample.
        waveform = _get_padded(data,
                               s - self._extract_before - 1,
                               s + self._extract_after + 2)
        return waveform[:, channels]  # Keep shank channels.

    def align(self, waveform, s_aligned):
        s = int(s_aligned)
        sb, sa = self._extract_before, self._extract_after
        # Perform interpolation around the fractional peak.
        old_s = np.arange(s - sb - 1, s + sa + 2)
        new_s = np.arange(s - sb + 0, s + sa + 0) + (s_aligned - s)
        try:
            f = interp1d(old_s, waveform, bounds_error=True,
                         kind='cubic', axis=0)
        except ValueError:
            logger.warn("Interpolation error at time {0:d}".format(s))
            return waveform
        return f(new_s)

    def set_thresholds(self, **kwargs):
        self._thresholds.update(kwargs)

    def __call__(self, component=None, data=None, data_t=None):
        assert data.shape == data_t.shape
        comp = self._component(component,
                               data=data,
                               n_samples=data_t.shape[0],
                               )
        channels = comp.channels

        wave = self._comp_wave(data_t, comp)
        masks = self.masks(data_t, wave, comp)
        s_aligned = self.spike_sample_aligned(wave, comp)

        waveform_unaligned = self.extract(data, s_aligned, channels=channels)
        waveform_aligned = self.align(waveform_unaligned, s_aligned)

        assert waveform_aligned.ndim == 2
        assert masks.ndim == 1
        assert waveform_aligned.shape[1] == masks.shape[0]

        return comp.group, s_aligned, waveform_aligned, masks


#------------------------------------------------------------------------------
# Waveform loader from traces (used in the manual sorting GUI)
#------------------------------------------------------------------------------

def _before_after(n_samples):
    """Get the number of samples before and after."""
    if not isinstance(n_samples, (tuple, list)):
        before = n_samples // 2
        after = n_samples - before
    else:
        assert len(n_samples) == 2
        before, after = n_samples
        n_samples = before + after
    assert before >= 0
    assert after >= 0
    assert before + after == n_samples
    return before, after


def _slice(index, n_samples, margin=None):
    """Return a waveform slice."""
    if margin is None:
        margin = (0, 0)
    assert isinstance(n_samples, (tuple, list))
    assert len(n_samples) == 2
    before, after = n_samples
    assert isinstance(margin, (tuple, list))
    assert len(margin) == 2
    margin_before, margin_after = margin
    before += margin_before
    after += margin_after
    index = int(index)
    before = int(before)
    after = int(after)
    return slice(max(0, index - before), index + after, None)


class WaveformLoader(object):
    """Load waveforms from filtered or unfiltered traces."""

    def __init__(self,
                 traces=None,
                 offset=0,
                 filter=None,
                 filter_margin=0,
                 n_samples=None,
                 channels=None,
                 scale_factor=None,
                 dc_offset=None,
                 ):
        self.n_samples_trace = 0
        # A (possibly memmapped) array-like structure with traces.
        if traces is not None:
            self.traces = traces
        else:
            self._traces = None
        self.dtype = np.float32
        # Scale factor for the loaded waveforms.
        self._scale_factor = scale_factor
        self._dc_offset = dc_offset
        # Offset of the traces: time (in samples) of the first trace sample.
        self._offset = int(offset)
        # List of channels to use when loading the waveforms.
        self._channels = channels
        # A filter function that takes a (n_samples, n_channels) array as
        # input.
        self._filter = filter
        # Number of samples to return, can be an int or a
        # tuple (before, after).
        if n_samples is None:
            raise ValueError("'n_samples' must be specified.")
        self.n_samples_before_after = _before_after(n_samples)
        self.n_samples_waveforms = sum(self.n_samples_before_after)
        # Number of additional samples to use for filtering.
        self._filter_margin = _before_after(filter_margin)
        # Number of samples in the extracted raw data chunk.
        self._n_samples_extract = (self.n_samples_waveforms +
                                   sum(self._filter_margin))

    @property
    def traces(self):
        """Raw traces."""
        return self._traces

    @traces.setter
    def traces(self, value):
        self.n_samples_trace, self.n_channels_traces = value.shape
        self._traces = value

    @property
    def channels(self):
        """List of channels."""
        return self._channels

    @channels.setter
    def channels(self, value):
        self._channels = value

    @property
    def n_channels_waveforms(self):
        """Number of channels kept for the waveforms."""
        if self._channels is not None:
            return len(self._channels)
        else:
            return self.n_channels_traces

    def _load_at(self, time):
        """Load a waveform at a given time."""
        time = int(time)
        time_o = time - self._offset
        ns = self.n_samples_trace
        if not (0 <= time_o < ns):
            raise ValueError("Invalid time {0:d}/{1:d}.".format(time_o,
                                                                ns))
        slice_extract = _slice(time_o,
                               self.n_samples_before_after,
                               self._filter_margin)
        extract = self._traces[slice_extract]

        # Pad the extracted chunk if needed.
        if slice_extract.start <= 0:
            extract = _pad(extract, self._n_samples_extract, 'left')
        elif slice_extract.stop >= ns - 1:
            extract = _pad(extract, self._n_samples_extract, 'right')

        assert extract.shape[0] == self._n_samples_extract

        # Filter the waveforms.
        # TODO: do the filtering in a vectorized way for higher performance.
        if self._filter is not None:
            waveforms = self._filter(extract)
        else:
            waveforms = extract

        # Remove the margin.
        margin_before, margin_after = self._filter_margin
        if margin_after > 0:
            assert margin_before >= 0
            waveforms = waveforms[margin_before:-margin_after, :]

        # Make a subselection with the specified channels.
        if self._channels is not None:
            out = waveforms[..., self._channels]
        else:
            out = waveforms

        assert out.shape == (self.n_samples_waveforms,
                             self.n_channels_waveforms)
        return out

    def __getitem__(self, item):
        """Load waveforms."""
        if isinstance(item, slice):
            raise NotImplementedError("Indexing with slices is not "
                                      "implemented yet.")
        if not isinstance(item, (tuple, list, np.ndarray)):
            item = [item]
        # Ensure a list of time samples are being requested.
        spikes = np.asarray(item)
        try:
            n_spikes = len(spikes)
        except TypeError:
            # HACK: handle the case where item is an unsized np.ndarray
            # (weird sort of scalar).
            spikes = np.asarray([item])
            n_spikes = len(spikes)
        # Initialize the array.
        # TODO: int16
        shape = (n_spikes, self.n_samples_waveforms,
                 self.n_channels_waveforms)
        # No traces: return null arrays.
        if self.n_samples_trace == 0:
            return np.zeros(shape, dtype=self.dtype)
        waveforms = np.empty(shape, dtype=self.dtype)
        # Load all spikes.
        for i, time in enumerate(spikes):
            try:
                waveforms[i, ...] = self._load_at(time)
            except ValueError as e:
                logger.warn("Error while loading waveform: {0}".format(str(e)))
        if self._dc_offset:
            waveforms -= self._dc_offset
        if self._scale_factor:
            waveforms *= self._scale_factor
        return waveforms


class SpikeLoader(object):
    """Translate selection with spike ids into selection with
    absolute times."""
    def __init__(self, waveforms, spike_samples):
        self._spike_samples = spike_samples
        # waveforms is a WaveformLoader instance
        self._waveforms = waveforms
        self.dtype = waveforms.dtype
        self.shape = (len(spike_samples),
                      waveforms.n_samples_waveforms,
                      waveforms.n_channels_waveforms)

    def __getitem__(self, item):
        times = self._spike_samples[item]
        return self._waveforms[times]

    def __len__(self):
        return len(self._spike_samples)
