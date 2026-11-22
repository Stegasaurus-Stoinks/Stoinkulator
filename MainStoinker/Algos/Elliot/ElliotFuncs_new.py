import numpy as np
from scipy.signal import argrelextrema

def calculate_slope(x1,y1,x2,y2):
        slope = (y2-y1)/(x2-x1)
        return slope

def display_waves(possibleWaves, array = []):
    wavesfordisplay = []
    #print(array)
    if array == []:
        for wave in possibleWaves:
            waveplot = wave.assemble()
            #wave.printdata()
            wavesfordisplay.append(waveplot)

    else:
        for i in array:
            #print(i)
            waveplot = possibleWaves[i].assemble()
            #print(waveplot)
            wavesfordisplay.append(waveplot)

    return wavesfordisplay
            



    

def find_line(endX,wave_x,ilocs_minMax):
    if np.isnan(endX):
        ilocs_minMax_valid = [x for x in ilocs_minMax if x>wave_x]#all max's past point 1
    else:
        ilocs_minMax_valid = [x for x in ilocs_minMax if (x>wave_x and x<endX)]#max's in wave 1
    return ilocs_minMax_valid

#code to detect if a lower min exists between points given in args
"""
wavex and wavey = wave.x123 and wave.y123. Use the wave values preceding the wave you're checking

"""
def min_limit_rule_break(x, wavex, wavey, mins):
    # NaN entries (no extremum there) never compare below anything, so they
    # need no separate filtering.
    for h in mins[wavex:x]:
        if h < wavey:
            return True
    return False

#code to detect if an upper min exists between points given in args
"""
wavex and wavey = wave.x123 and wave.y123. Use the wave values preceding the wave you're checking

"""
def max_limit_rule_break(x, wavex, wavey, maxs):
    for h in maxs[wavex:x]:
        if h > wavey:
            return True
    return False

#if there are more future points to check and wave is valid, then print incomplete wave
def check_future_points(x, ilocs_min_valid, reach, tradingWaves, wave):
    # Lazy import to avoid circular dependency
    from MainStoinker.Algos.Elliot.ElliotImpulse import ElliotImpulse

    if ilocs_min_valid:
        if x == ilocs_min_valid[-1] and reach > len(ilocs_min_valid):
            tradingWaves.append(ElliotImpulse(
                wave.plotSize,
                wave.time_1, wave.price_1,
                wave.time_2, wave.price_2,
                wave.time_3, wave.price_3,
                wave.time_4, wave.price_4,
                wave.time_5, wave.price_5,
                wave.time_6, wave.price_6
            ))
    
def check_retracement(num1,num2,num3,retList):
    reach = 0.03
    properRetracement = False

    ans = (num2-num3)/(num2-num1)
    for retrace in retList:
        lowLimit = retrace-reach
        highLimit = retrace+reach
        if ans > lowLimit and ans < highLimit:
            return True
    return properRetracement

class IncrementalExtrema:
    """`argrelextrema(data, comparator, order=order)[0]`, recomputed incrementally.

    `update(data)` returns exactly what scipy returns for `data` (same values,
    same dtype), but only does work proportional to `order` when `data` is the
    previous call's data with bars appended.

    Why that is exact. scipy (mode='clip') decides index i from the 2*order
    neighbours i-1..i-order and i+1..i+order, where an out-of-range neighbour
    index is clipped to the nearest end of the array. So for an array of
    length n the result at i depends only on data[max(0, i-order) : i+order+1]
    as long as i + order <= n - 1, i.e. i < n - order. Appending bars never
    changes those indices: they are FINAL. Indices >= n - order look at the
    clipped right edge, so they are recomputed on every call. To recompute
    indices >= f exactly it is enough to run scipy on data[max(0, f-order):]:
    for every i >= f, i-order >= max(0, f-order), so the slice contains all
    the left neighbours, and the right edge (the clip) is the same as in the
    full array; the left clip only applies when the slice starts at 0, which
    is also the full array's start.

    Safety net: the previous data is kept (copied) and compared with the
    prefix of the new data on every call (a vectorised compare, microseconds).
    If the data is shorter than before, or any earlier value differs, all
    state is dropped and the result is computed from scratch (`generation`
    counts such drops, including the first call), so a caller that is handed
    a different history never gets a stale answer.

    `data` is only read; the caller's array is never written to.
    """

    def __init__(self, comparator, order):
        self.comparator = comparator
        self.order = order
        self.generation = 0
        self.reset()

    def reset(self):
        self.generation += 1
        self._seen = np.empty(0, dtype=np.float64)   # capacity buffer: first _n entries are the data seen
        self._n = 0
        self._final = np.empty(0, dtype=np.intp)     # extrema with index < _f
        self._f = 0

    def update(self, data):
        order = self.order
        if not isinstance(order, int) or isinstance(order, bool) or order < 1 or len(data) == 0:
            # Not a case the incremental scheme covers; let scipy answer (or raise) as before.
            self.reset()
            return argrelextrema(data, self.comparator, order=order)[0]
        data = np.asarray(data, dtype=np.float64)
        n = len(data)
        if n < self._n or not np.array_equal(data[:self._n], self._seen[:self._n], equal_nan=True):
            self.reset()
        if n > len(self._seen):
            grown = np.empty(max(n, 2 * len(self._seen)), dtype=np.float64)
            grown[:self._n] = self._seen[:self._n]
            self._seen = grown
        self._seen[self._n:n] = data[self._n:n]
        self._n = n

        f = self._f
        start = max(0, f - order)
        found = argrelextrema(data[start:], self.comparator, order=order)[0] + start
        found = found[found >= f]
        new_f = max(0, n - order)
        self._final = np.concatenate((self._final, found[found < new_f]))
        self._f = max(f, new_f)
        return np.concatenate((self._final, found[found >= new_f]))


class LineAfter:
    """Lazy `find_line(np.nan, x, ilocs)` for a sorted index array: the entries > x.

    Supports what the wave search uses: `len`, truthiness, `[-1]` and slices.
    Slices and items come back as numpy values (the original returned a list
    of the same numpy integers). `settled(limit, reach)` tells whether the
    first `reach + 1` entries exist and are all below `limit`.
    """

    __slots__ = ("_arr", "_pos")

    def __init__(self, ilocs, x):
        self._arr = ilocs
        self._pos = int(np.searchsorted(ilocs, x, side="right"))

    def __len__(self):
        return len(self._arr) - self._pos

    def __getitem__(self, key):
        return self._arr[self._pos:][key]

    def settled(self, limit, reach):
        return len(self) > reach and self._arr[self._pos + reach] < limit
