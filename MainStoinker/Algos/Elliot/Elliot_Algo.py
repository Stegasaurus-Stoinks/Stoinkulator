import numpy as np
import pandas as pd

from MainStoinker.Algos.ParentAlgo import ParentAlgo
from MainStoinker.Algos.Elliot.ElliotImpulse import ElliotImpulse
from MainStoinker.Algos.Elliot import ElliotFuncs_new as ElliotFuncs


def _float_values(column):
    """The column as a float64 array. Read-only input: never write to the result."""
    return np.asarray(column.values, dtype=np.float64)


class Algo(ParentAlgo):
    """
    Elliott Wave Algorithm

    Detects Elliott Wave patterns (6-point impulse waves) and enters trades
    on wave 2 or wave 4 completion, using trailing stops for exit.
    """

    def __init__(self, algoConfigData):
        super().__init__(algoConfigData)

        # Algo config params
        self.order = int(algoConfigData.get('order', 10))
        self.trailingStopDelta = float(algoConfigData.get('trailingStopDelta', 0.10))

        # Frontend visualization columns
        self.FrontEndDataStruct = ['mins', 'maxs', 'StopPrice', 'Trade']
        self.FrontEndDataType = ['marker-up', 'marker-down', 'segment', 'baseline']

        # AlgoData DataFrame for calculations
        self.DataColumns = ['time'] + self.FrontEndDataStruct
        self.AlgoData = pd.DataFrame(columns=self.DataColumns)

        # Wave tracking
        self.finishedWaves = []
        self.tradingWaves = []
        self.currentWave = None
        self.waveNum = 0
        self.secondExitPrice = 0  # Previous wave high for tighter trailing

        # Trade state
        self.entryPrice = np.nan
        self.initialStopDelta = 0.02  # 2% initial trailing before breakout

        # Incremental state. Both caches verify the history they were built
        # from on every update and rebuild from scratch if it is not the same
        # history plus new bars (see ElliotFuncs.IncrementalExtrema and
        # _detect_waves), so a different or replaced history is always safe.
        self._close_min = ElliotFuncs.IncrementalExtrema(np.less_equal, self.order)
        self._close_max = ElliotFuncs.IncrementalExtrema(np.greater_equal, self.order)
        self._low_min = ElliotFuncs.IncrementalExtrema(np.less_equal, self.order)
        self._high_max = ElliotFuncs.IncrementalExtrema(np.greater_equal, self.order)
        self._start_cache = {}  # first-point index -> finished waves of a settled search
        self._start_cache_generation = None


    def update(self, StockData):
        """Main update loop called on each new candle."""

        # ParentAlgo handles: AlgoData sync, time column, curStockData, stoploss/tp checks
        super().pre_update(StockData)

        # Elliot-specific: tighten trailing stop when price exceeds wave high
        if self.inTrade and self.secondExitPrice > 0:
            curPrice = self.curStockData['close']
            if curPrice > self.secondExitPrice:
                self.trade.set_stopDelta(self.trailingStopDelta)

        # Calculate mins and maxs
        self._calculate_extrema(StockData)

        # Detect Elliott waves
        self.finishedWaves, self.tradingWaves = self._detect_waves(StockData)

        # Check for entry signal if not in trade
        if not self.inTrade:
            self._check_entry_signal()

        # Update AlgoData for frontend
        self._update_algo_data()

        # ParentAlgo handles: curAlgoData assignment
        super().post_update()


    def _calculate_extrema(self, StockData):
        """Find local mins and maxs using argrelextrema."""

        close = _float_values(StockData['close'])
        low = _float_values(StockData['low'])
        high = _float_values(StockData['high'])

        # Get indices of local extrema
        ilocs_min = self._extrema(self._close_min, close)
        ilocs_max = self._extrema(self._close_max, close)

        # Create mins column - mark with low price at extrema points
        # (AlgoData always has one row per StockData row after pre_update)
        size = len(self.AlgoData)
        mins = np.full(size, np.nan)
        sel = ilocs_min[ilocs_min < size]
        mins[sel] = low[sel] * 0.999
        self.AlgoData['mins'] = mins

        # Create maxs column - mark with high price at extrema points
        maxs = np.full(size, np.nan)
        sel = ilocs_max[ilocs_max < size]
        maxs[sel] = high[sel] * 1.001
        self.AlgoData['maxs'] = maxs


    # Filtered views of the extrema columns, built on demand from AlgoData
    # (they used to be rebuilt on every bar although the algo never read them).
    @property
    def mins_df(self):
        return self.AlgoData.loc[pd.notnull(self.AlgoData['mins'])][['time', 'mins']].rename(columns={'mins': 'price'})

    @property
    def maxs_df(self):
        return self.AlgoData.loc[pd.notnull(self.AlgoData['maxs'])][['time', 'maxs']].rename(columns={'maxs': 'price'})

    @property
    def mins_array(self):
        return self.AlgoData['mins'].tolist()

    @property
    def maxs_array(self):
        return self.AlgoData['maxs'].tolist()


    def _extrema(self, tracker, values):
        """Same as argrelextrema(values, tracker's comparator, order=self.order)[0]."""
        if tracker.order != self.order:
            tracker.order = self.order
            tracker.reset()
        return tracker.update(values)


    def _detect_waves(self, StockData):
        """
        Detect Elliott Wave patterns in the data.

        Returns tuple of (finishedWaves, tradingWaves) where:
        - finishedWaves: Complete 6-point waves
        - tradingWaves: Incomplete waves that could be traded

        Every local minimum of the lows is a possible wave start, and each
        start is searched independently (`_search_start`); the results are
        concatenated in start order. A start whose search is "settled" can
        never give a different result on later bars, so its finished waves
        are remembered and the search is not repeated.

        With k = order, the extrema at indices < len - k are final (see
        ElliotFuncs.IncrementalExtrema). A search is settled when every
        candidate list it computed (the extrema after a point) contains at
        least reach + 1 = 4 entries below that limit, and it raised nothing:

        - only the first 4 candidates of each list are ever tried and they
          are all final, so the same candidates come out on every later bar;
        - every rule check scans mins/maxs entries at indices before such a
          candidate, which are final too (values included);
        - the plotSize checks (x < plotSize - 3) hold for every final
          candidate when k >= 3 (x < plotSize - k <= plotSize - 3), and stay
          true as plotSize grows;
        - check_future_points can only add a trading wave from a list with
          fewer than 4 entries, and every list involved (it is also fed a
          list left over from a deeper level, see _search_start) has at
          least 4, so a settled search adds no trading waves, ever.

        For k < 3 nothing is remembered. If the low/high history is not the
        previous one plus new bars, the extrema trackers reset and so does
        this memory.
        """
        plotSize = StockData.shape[0]

        # Get indices of mins and maxs
        low = _float_values(StockData['low'])
        high = _float_values(StockData['high'])
        ilocs_min = self._extrema(self._low_min, low)
        ilocs_max = self._extrema(self._high_max, high)

        # Build mins/maxs arrays with price values
        mins = [np.nan] * plotSize
        sel = ilocs_min[ilocs_min < plotSize]
        for i, price in zip(sel, low[sel] * 0.9999):
            mins[i] = price

        maxs = [np.nan] * plotSize
        sel = ilocs_max[ilocs_max < plotSize]
        for i, price in zip(sel, high[sel] * 1.0001):
            maxs[i] = price

        generation = (self._low_min.generation, self._high_max.generation)
        if generation != self._start_cache_generation:
            self._start_cache = {}
            self._start_cache_generation = generation
        cache = self._start_cache
        limit = plotSize - self.order if self.order >= 3 else None  # extrema below this are final

        finishedWaves = []
        tradingWaves = []

        # For every min in chart (potential wave start)
        for i in range(len(ilocs_min)):
            start = int(ilocs_min[i])
            remembered = cache.get(start)
            if remembered is not None:
                for points in remembered:
                    finishedWaves.append(ElliotImpulse(plotSize, *points))
                continue

            finished, trading, settled = self._search_start(i, ilocs_min, ilocs_max, mins, maxs, plotSize, limit)
            finishedWaves.extend(finished)
            tradingWaves.extend(trading)
            if settled:
                cache[start] = [
                    (w.time_1, w.price_1, w.time_2, w.price_2, w.time_3, w.price_3,
                     w.time_4, w.price_4, w.time_5, w.price_5, w.time_6, w.price_6)
                    for w in finished
                ]

        return finishedWaves, tradingWaves


    def _search_start(self, i, ilocs_min, ilocs_max, mins, maxs, plotSize, limit):
        """
        Search for waves starting at the i-th local minimum.

        Returns (finishedWaves, tradingWaves, settled). `settled` is True only
        if `limit` is not None, nothing raised and every candidate list that
        was computed has its first reach + 1 entries below `limit` (see
        _detect_waves).

        The body is the original nested search, unchanged except that
        find_line is the lazy ElliotFuncs.LineAfter. In particular the
        `ilocs_min_valid` / `ilocs_max_valid` names are re-used by the deeper
        levels, so check_future_points sometimes sees a list from a different
        level; that is existing behaviour and is kept.
        """
        reach = 3  # How many future points to check
        finishedWaves = []
        tradingWaves = []
        settled = limit is not None
        try:
            wave = ElliotImpulse(plotSize)
            wave.time_1 = ilocs_min[i]
            wave.price_1 = mins[ilocs_min[i]]

            # Check wave 1 / point 2 [ / ]
            ilocs_max_valid = ElliotFuncs.LineAfter(ilocs_max, wave.time_1)
            settled = settled and ilocs_max_valid.settled(limit, reach)
            for curPoint in ilocs_max_valid[0:reach+1]:
                if wave.checkpoint2(curPoint, maxs[curPoint], mins):
                    wave.time_2 = curPoint
                    wave.price_2 = maxs[curPoint]

                    # Check wave 2 / point 3 [ /\ ]
                    ilocs_min_valid = ElliotFuncs.LineAfter(ilocs_min, wave.time_2)
                    settled = settled and ilocs_min_valid.settled(limit, reach)
                    for curPoint in ilocs_min_valid[0:reach+1]:
                        if wave.checkpoint3(curPoint, mins[curPoint], maxs, mins):
                            wave.time_3 = curPoint
                            wave.price_3 = mins[curPoint]

                            ElliotFuncs.check_future_points(curPoint, ilocs_min_valid, reach, tradingWaves, wave)

                            # Check wave 3 / point 4 [ /\/ ]
                            ilocs_max_valid = ElliotFuncs.LineAfter(ilocs_max, wave.time_3)
                            settled = settled and ilocs_max_valid.settled(limit, reach)
                            for curPoint in ilocs_max_valid[0:reach+1]:
                                if wave.checkpoint4(curPoint, maxs[curPoint], mins):
                                    wave.time_4 = curPoint
                                    wave.price_4 = maxs[curPoint]

                                    ElliotFuncs.check_future_points(curPoint, ilocs_max_valid, reach, tradingWaves, wave)

                                    # Check wave 4 / point 5 [ /\/\ ]
                                    ilocs_min_valid = ElliotFuncs.LineAfter(ilocs_min, wave.time_4)
                                    settled = settled and ilocs_min_valid.settled(limit, reach)
                                    for curPoint in ilocs_min_valid[0:reach+1]:
                                        if wave.checkpoint5(curPoint, mins[curPoint], maxs):
                                            wave.time_5 = curPoint
                                            wave.price_5 = mins[curPoint]

                                            ElliotFuncs.check_future_points(curPoint, ilocs_min_valid, reach, tradingWaves, wave)

                                            # Check wave 5 / point 6 [ /\/\/ ]
                                            ilocs_max_valid = ElliotFuncs.LineAfter(ilocs_max, wave.time_5)
                                            settled = settled and ilocs_max_valid.settled(limit, reach)
                                            for curPoint in ilocs_max_valid[0:reach+1]:
                                                if wave.checkpoint6(curPoint, maxs[curPoint], mins):
                                                    wave.time_6 = curPoint
                                                    wave.price_6 = maxs[curPoint]
                                                    finishedWaves.append(ElliotImpulse(
                                                        wave.plotSize,
                                                        wave.time_1, wave.price_1,
                                                        wave.time_2, wave.price_2,
                                                        wave.time_3, wave.price_3,
                                                        wave.time_4, wave.price_4,
                                                        wave.time_5, wave.price_5,
                                                        wave.time_6, wave.price_6
                                                    ))
        except Exception as e:
            self.logger.debug(f"Wave detection error: {e}")
            settled = False

        return finishedWaves, tradingWaves, settled


    def _check_wave_number(self, wave):
        """
        Determine which wave we're on based on defined points.

        Returns:
            0: Only point 1 defined
            1: Points 1-2 defined (wave 1 complete)
            2: Points 1-3 defined (wave 2 complete) - ENTRY SIGNAL
            3: Points 1-4 defined (wave 3 complete)
            4: Points 1-5 defined (wave 4 complete) - ENTRY SIGNAL
        """
        if np.isnan(wave.time_2):
            return 0
        elif np.isnan(wave.time_3):
            return 1
        elif np.isnan(wave.time_4):
            return 2  # Entry signal
        elif np.isnan(wave.time_5):
            return 3
        elif np.isnan(wave.time_6):
            return 4  # Entry signal
        return 5  # Complete wave


    def _check_entry_signal(self):
        """Check for wave 2 or wave 4 completion and enter trade."""

        if not self.tradingWaves:
            return

        # Get most recent incomplete wave
        curWave = self.tradingWaves[-1]
        self.waveNum = self._check_wave_number(curWave)

        # Entry on wave 2 completion
        if self.waveNum == 2:
            self.currentWave = curWave
            self.secondExitPrice = curWave.price_2  # Wave 2 high
            self._enter_trade(trend=1)
            self.logger.info(f"Entering trade on Wave 2 completion. secondExitPrice: {self.secondExitPrice}")

        # Entry on wave 4 completion
        elif self.waveNum == 4:
            self.currentWave = curWave
            self.secondExitPrice = curWave.price_4  # Wave 4 high
            self._enter_trade(trend=1)
            self.logger.info(f"Entering trade on Wave 4 completion. secondExitPrice: {self.secondExitPrice}")


    def _enter_trade(self, trend):
        """Open a trade position and configure the Trade object."""

        self.entryPrice = self.curStockData['close']
        self.trade = self.open_trade(volume=10, trend=trend)

        # Configure trailing stop
        self.trade.set_stopLossType('Trailing')
        self.trade.set_stopDelta(self.initialStopDelta * self.entryPrice)  # Initial wider stop

        self.logger.info(f"Trade opened at {self.entryPrice} with initial stop delta")


    def _update_algo_data(self):
        """Update AlgoData for frontend visualization."""

        # Note: time column is already synced by pre_update()

        # Update trade-related data if in position
        if self.inTrade and self.trade:
            self.AlgoData.at[self.AlgoData.index[-1], 'StopPrice'] = self.trade.stopPrice
            self.AlgoData.at[self.AlgoData.index[-1], 'Trade'] = self.curStockData['close']
