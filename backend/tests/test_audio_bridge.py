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


def voice(seconds, rate=48000, level=0.2):
    """Loud, and every sample different, so the order and completeness of what is played can be checked."""
    count = round(seconds * rate)
    return (level + level * np.arange(count) / count).astype(np.float32)


def hush(seconds, rate=48000, level=0.001):
    """Room noise between words: far below speech, every sample different."""
    count = round(seconds * rate)
    return (level + level * 0.1 * np.arange(count) / count).astype(np.float32)


def on_time(bridge, sound_in):
    """Deliver the way the recorder does (a quarter of a second at a time) while the card plays; return what was played."""
    rate, played = bridge.rate, []
    for start in range(0, len(sound_in), rate * 24 // 100):
        bridge.push(sound_in[start:start + rate * 24 // 100])
        played.append(play(bridge, rate * 24 // 100))
    return np.concatenate(played)


def test_a_backlog_is_worked_off_in_its_pauses_and_no_speech_is_lost(sound):
    bridge = bridge_module.OutputBridge('BlackHole 2ch')
    rate = bridge.rate
    before, pause, after = voice(0.5), hush(0.6), voice(0.5, level=0.3)
    bridge.push(np.concatenate([before, pause, after]))                  # 1.6 s at once: the link had stalled
    stats = bridge.stats()
    assert stats['skipped_seconds'] == 0.5 and stats['dropped_seconds'] == 0 and stats['queued_ms'] == 1100
    heard = play(bridge, rate * 11 // 10)
    kept = np.concatenate([before, pause[:rate * 6 // 100], pause[-rate * 4 // 100:], after])
    assert np.array_equal(heard, kept)                                   # both sentences whole; the pause is 0.1 s now


def test_gaps_inside_words_stay_and_a_pause_keeps_its_edges(sound):
    bridge = bridge_module.OutputBridge('BlackHole 2ch')
    rate = bridge.rate
    parts = [voice(0.4), hush(0.12), voice(0.4, level=0.3), hush(0.16), voice(0.3, level=0.4)]
    bridge.push(np.concatenate(parts))                                   # 1.38 s waiting
    assert bridge.stats()['skipped_seconds'] == 0.06                     # only the middle of the 0.16 s pause went
    heard = play(bridge, rate * 132 // 100)
    pause = parts[3]
    assert np.array_equal(heard, np.concatenate([*parts[:3], pause[:rate * 6 // 100], pause[-rate * 4 // 100:], parts[4]]))


def test_sound_that_arrives_on_time_is_played_exactly_as_it_came(sound):
    bridge = bridge_module.OutputBridge('BlackHole 2ch')
    said = np.concatenate([voice(0.96), hush(1.92), voice(0.96, level=0.3), hush(0.96)])     # long pauses, nothing late
    heard = on_time(bridge, said)
    stats = bridge.stats()
    assert stats['skipped_seconds'] == 0 and stats['dropped_seconds'] == 0 and stats['underflows'] == 0
    start = int(np.argmax(heard != 0))                                   # the cushion fills first, then every sample follows
    assert start == bridge.rate * 24 // 100 and np.array_equal(heard[start:], said[:len(heard) - start])


def test_a_silence_after_a_backlog_brings_the_delay_back_and_it_stays_there(sound):
    bridge = bridge_module.OutputBridge('BlackHole 2ch')
    rate = bridge.rate
    late = voice(1.2)
    bridge.push(late)                                                    # 1.2 s behind, and nobody pauses in it
    assert bridge.stats()['queued_ms'] == 1200 and bridge.stats()['skipped_seconds'] == 0
    heard = on_time(bridge, hush(1.44))                                  # then the speaker is quiet for a moment
    assert bridge.stats()['queued_ms'] <= 450
    assert np.array_equal(heard[:len(late)], late)                       # the late sentence was played in full
    more = voice(1.2, level=0.45)
    heard = np.concatenate([heard, on_time(bridge, more), play(bridge, rate)])
    stats = bridge.stats()
    assert stats['dropped_seconds'] == 0 and stats['queued_ms'] == 0
    position = int(np.argmax(heard == more[0]))
    assert np.array_equal(heard[position:position + len(more)], more)    # what is said next arrives whole
    assert position - len(late) <= rate * 6 // 10                        # 1.44 s of silence shrank to make up the delay


def test_only_what_does_not_fit_is_discarded_oldest_first(sound):
    bridge = bridge_module.OutputBridge('BlackHole 2ch')
    rate = bridge.rate
    bridge.push(np.full(rate * 2, 0.25, dtype=np.float32))               # two seconds of speech arrive at once
    bridge.push(np.full(rate // 2, 0.5, dtype=np.float32))
    stats = bridge.stats()
    assert stats['dropped_seconds'] == 1.0 and stats['skipped_seconds'] == 0 and stats['queued_ms'] == 1500
    heard = play(bridge, rate * 3 // 2)
    assert np.allclose(heard[:rate], 0.25) and np.allclose(heard[rate:], 0.5)    # the newest 1.5 s, in order


def test_what_counts_as_a_pause_follows_the_noise_of_the_room(sound):
    first, between, second = voice(0.4), hush(0.5, level=0.02), voice(0.4, level=0.45)

    def gap_heard(room_level):
        """Seconds between the two sentences once a late burst of them has been played in a room this noisy."""
        bridge = bridge_module.OutputBridge('BlackHole 2ch')
        on_time(bridge, hush(2.4, level=room_level))
        bridge.push(np.concatenate([first, between, second]))
        heard = play(bridge, bridge.rate * 2)
        return (int(np.argmax(heard == second[0])) - int(np.argmax(heard == first[0])) - len(first)) / bridge.rate

    assert gap_heard(0.001) == 0.5          # a silent room: sound at 0.02 is a soft voice and is played in full
    assert gap_heard(0.02) == 0.1           # a room that hums at 0.02: that is its silence, and it is shortened
