"""Jitter buffer of the virtual sound card output, driven by a stand-in for PortAudio."""
import numpy as np
import pytest

import backend.audio_bridge as bridge_module


class FakeStream:
    def __init__(self, **options):
        self.options, self.active = options, False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        pass


class FakeSound:
    """Two devices: a loopback virtual card and the built-in speakers."""
    devices = [
        {'name': 'BlackHole 2ch', 'max_output_channels': 2, 'max_input_channels': 2, 'default_samplerate': 48000.0},
        {'name': 'MacBook Pro扬声器', 'max_output_channels': 2, 'max_input_channels': 0, 'default_samplerate': 44100.0},
        {'name': 'MacBook Pro麦克风', 'max_output_channels': 0, 'max_input_channels': 1, 'default_samplerate': 48000.0},
    ]
    default = type('Default', (), {'device': (2, 1)})()
    OutputStream = FakeStream
    rescans = 0

    @classmethod
    def query_devices(cls):
        return cls.devices

    @staticmethod
    def check_output_settings(**options):
        pass

    @classmethod
    def _terminate(cls):
        cls.rescans += 1

    @staticmethod
    def _initialize():
        pass


@pytest.fixture
def sound(monkeypatch):
    monkeypatch.setattr(bridge_module, 'sd', FakeSound)
    monkeypatch.setattr(bridge_module, '_active', 0)
    FakeSound.rescans = 0
    return FakeSound


def play(bridge, frames):
    out = np.ones((frames, bridge.channels), dtype=np.float32)      # the callback must overwrite all of it
    bridge._callback(out, frames, None, None)
    return out[:, 0]


def test_outputs_put_the_virtual_card_first_and_suggest_only_a_loopback_device(sound):
    listing = bridge_module.list_outputs()
    assert [row['name'] for row in listing['devices']] == ['BlackHole 2ch', 'MacBook Pro扬声器']     # no microphones
    assert listing['suggested'] == 'BlackHole 2ch' and listing['devices'][1]['system_default']
    assert sound.rescans == 1                                           # a newly installed card shows up without a restart
    sound_without_card = [device for device in sound.devices if device['name'] != 'BlackHole 2ch']
    sound.devices, original = sound_without_card, sound.devices
    try:
        listing = bridge_module.list_outputs()
        assert listing['suggested'] is None and '没有找到虚拟声卡' in listing['message']   # never suggests the speakers
    finally:
        sound.devices = original


def test_device_list_is_not_rescanned_while_a_stream_is_playing(sound):
    bridge = bridge_module.OutputBridge('BlackHole 2ch')
    bridge.start()
    bridge_module.list_outputs()
    assert sound.rescans == 0 and bridge.active
    bridge.close()
    bridge_module.list_outputs()
    assert sound.rescans == 1 and not bridge.active


def test_unknown_device_is_refused_by_name(sound):
    with pytest.raises(ValueError, match='没有找到名为“Soundflower \\(2ch\\)”的输出设备'):
        bridge_module.OutputBridge('Soundflower (2ch)')


def test_playback_waits_for_a_cushion_then_plays_in_order_and_rebuffers_after_running_dry(sound):
    bridge = bridge_module.OutputBridge('BlackHole 2ch')
    rate = bridge.rate
    ramp = np.arange(1, rate + 1, dtype=np.float32) / rate              # one second, every sample distinct
    bridge.push(ramp[:rate // 5])                                        # 0.2 s: less than the 0.3 s cushion
    assert not play(bridge, 480).any()                                   # silence, nothing consumed
    bridge.push(ramp[rate // 5:rate // 2])                               # now 0.5 s is waiting
    first = play(bridge, 4800)
    assert np.array_equal(first, ramp[:4800])                            # starts from the very first sample
    rest = play(bridge, rate)                                            # asks for more than is left
    assert np.array_equal(rest[:rate // 2 - 4800], ramp[4800:rate // 2]) and not rest[rate // 2 - 4800:].any()
    assert bridge.underflows == 1
    bridge.push(ramp[:480])                                              # a trickle does not restart playback
    assert not play(bridge, 480).any()
    stats = bridge.stats()
    assert stats['underflows'] == 1 and stats['queued_ms'] == 10 and stats['played_seconds'] == 0.5


def test_a_backlog_is_dropped_instead_of_becoming_permanent_delay(sound):
    bridge = bridge_module.OutputBridge('BlackHole 2ch')
    rate = bridge.rate
    bridge.push(np.full(rate * 2, 0.25, dtype=np.float32))               # two seconds arrive at once (a BLE burst)
    bridge.push(np.full(rate // 2, 0.5, dtype=np.float32))               # over the 1.5 s limit: the old audio goes
    stats = bridge.stats()
    assert stats['dropped_seconds'] == 2.0 and stats['queued_ms'] == 500
    assert np.allclose(play(bridge, 480), 0.5)                           # what plays next is the newest audio
