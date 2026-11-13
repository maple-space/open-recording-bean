"""Telling speakers apart while the sentences arrive.

The speech worker turns every finished sentence into a voiceprint (a vector that is close for the
same voice and far for different ones). This module keeps the voices heard so far in one session
and gives each sentence a speaker number the moment it arrives. It never knows who anybody is:
numbers only, in the order the voices were first heard. Nothing is decided or changed afterwards.

The rule is the first version, the one checked on ten recorded voices, plus one line:

- A full sentence (about a second and a half of speech or more) joins the known voice it is close
  to, and is a new speaker otherwise, provided it has enough words in it to be a sentence.
- A short sentence carries less voice. It joins a known voice a little more easily, and never
  opens a new speaker.
- Whatever the voice could not tell (a word or two unlike anyone, a noise) is whoever spoke last.
  Before anyone has been told it is speaker 1, the first to come.
"""
try:
    import numpy as np
except Exception:  # pragma: no cover - missing optional dependency
    np = None


class Voices:
    def __init__(self, *, join: float = 0.55, join_short: float = 0.45, short_seconds: float = 2.0,
                 least_seconds: float = 0.8):
        self.join, self.join_short = join, join_short
        self.short_seconds, self.least_seconds = short_seconds, least_seconds
        self.centres = []                       # one running sum of unit voiceprints per speaker
        self.last = None                        # the speaker of the latest sentence

    def __len__(self) -> int:
        return len(self.centres)

    def hear(self, voice, seconds: float, may_open: bool = True):
        """The speaker number (1, 2, ...) of a sentence."""
        if np is None:
            return None
        found = self._by_voice(voice, seconds, may_open)
        if found is None:
            found = self.last or 1              # the voice could not tell: whoever spoke last
        self.last = found
        return found

    def _by_voice(self, voice, seconds: float, may_open: bool):
        """The first version, unchanged: the speaker by voiceprint, or None when it cannot be told."""
        if voice is None or seconds < self.least_seconds:
            return None
        vector = np.asarray(voice, dtype=np.float32)
        norm = float(np.linalg.norm(vector)) if vector.ndim == 1 and vector.size else 0.0
        if not norm or not np.isfinite(norm):
            return None
        vector = vector / norm
        scores = [float(centre @ vector / np.linalg.norm(centre)) for centre in self.centres]
        best = int(np.argmax(scores)) if scores else -1
        short = seconds < self.short_seconds
        if best >= 0 and scores[best] >= (self.join_short if short else self.join):
            if not short:                       # only full sentences shape what a voice sounds like
                self.centres[best] = self.centres[best] + vector * min(seconds, 6.0)
            return best + 1
        if short or not may_open:
            return None
        self.centres.append(vector * min(seconds, 6.0))
        return len(self.centres)
