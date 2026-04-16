import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

from MainStoinker.Algos.ParentAlgo import ParentAlgo
from MainStoinker.Algos.Elliot.ElliotImpulse import ElliotImpulse
from MainStoinker.Algos.Elliot import ElliotFuncs_new as ElliotFuncs


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

        # Get indices of local extrema
        ilocs_min = argrelextrema(StockData['close'].values, np.less_equal, order=self.order)[0]
        ilocs_max = argrelextrema(StockData['close'].values, np.greater_equal, order=self.order)[0]

        # Create mins column - mark with low price at extrema points
        self.AlgoData['mins'] = np.nan
        for i in ilocs_min:
            if i < len(self.AlgoData):
                self.AlgoData.at[i, 'mins'] = StockData.iloc[i]['low'] * 0.999

        # Create maxs column - mark with high price at extrema points
        self.AlgoData['maxs'] = np.nan
        for i in ilocs_max:
            if i < len(self.AlgoData):
                self.AlgoData.at[i, 'maxs'] = StockData.iloc[i]['high'] * 1.001

        # Store filtered DataFrames for wave detection
        self.mins_df = self.AlgoData.loc[pd.notnull(self.AlgoData['mins'])][['time', 'mins']].rename(columns={'mins': 'price'})
        self.maxs_df = self.AlgoData.loc[pd.notnull(self.AlgoData['maxs'])][['time', 'maxs']].rename(columns={'maxs': 'price'})

        # Store arrays for checkpoint validation
        self.mins_array = self.AlgoData['mins'].tolist()
        self.maxs_array = self.AlgoData['maxs'].tolist()


    def _detect_waves(self, StockData):
        """
        Detect Elliott Wave patterns in the data.

        Returns tuple of (finishedWaves, tradingWaves) where:
        - finishedWaves: Complete 6-point waves
        - tradingWaves: Incomplete waves that could be traded
        """
        plotSize = StockData.shape[0]

        # Get indices of mins and maxs
        ilocs_min = argrelextrema(StockData['low'].values, np.less_equal, order=self.order)[0]
        ilocs_max = argrelextrema(StockData['high'].values, np.greater_equal, order=self.order)[0]

        # Build mins/maxs arrays with price values
        mins = [np.nan] * plotSize
        for i in ilocs_min:
            if i < len(mins):
                mins[i] = StockData.iloc[i]['low'] * 0.9999

        maxs = [np.nan] * plotSize
        for i in ilocs_max:
            if i < len(maxs):
                maxs[i] = StockData.iloc[i]['high'] * 1.0001

        reach = 3  # How many future points to check
        finishedWaves = []
        tradingWaves = []

        # For every min in chart (potential wave start)
        for i in range(len(ilocs_min)):
            try:
                wave = ElliotImpulse(plotSize)
                wave.time_1 = ilocs_min[i]
                wave.price_1 = mins[ilocs_min[i]]

                # Check wave 1 / point 2 [ / ]
                ilocs_max_valid = ElliotFuncs.find_line(np.nan, wave.time_1, ilocs_max)
                for curPoint in ilocs_max_valid[0:reach+1]:
                    if wave.checkpoint2(curPoint, maxs[curPoint], mins):
                        wave.time_2 = curPoint
                        wave.price_2 = maxs[curPoint]

                        # Check wave 2 / point 3 [ /\ ]
                        ilocs_min_valid = ElliotFuncs.find_line(np.nan, wave.time_2, ilocs_min)
                        for curPoint in ilocs_min_valid[0:reach+1]:
                            if wave.checkpoint3(curPoint, mins[curPoint], maxs, mins):
                                wave.time_3 = curPoint
                                wave.price_3 = mins[curPoint]

                                ElliotFuncs.check_future_points(curPoint, ilocs_min_valid, reach, tradingWaves, wave)

                                # Check wave 3 / point 4 [ /\/ ]
                                ilocs_max_valid = ElliotFuncs.find_line(np.nan, wave.time_3, ilocs_max)
                                for curPoint in ilocs_max_valid[0:reach+1]:
                                    if wave.checkpoint4(curPoint, maxs[curPoint], mins):
                                        wave.time_4 = curPoint
                                        wave.price_4 = maxs[curPoint]

                                        ElliotFuncs.check_future_points(curPoint, ilocs_max_valid, reach, tradingWaves, wave)

                                        # Check wave 4 / point 5 [ /\/\ ]
                                        ilocs_min_valid = ElliotFuncs.find_line(np.nan, wave.time_4, ilocs_min)
                                        for curPoint in ilocs_min_valid[0:reach+1]:
                                            if wave.checkpoint5(curPoint, mins[curPoint], maxs):
                                                wave.time_5 = curPoint
                                                wave.price_5 = mins[curPoint]

                                                ElliotFuncs.check_future_points(curPoint, ilocs_min_valid, reach, tradingWaves, wave)

                                                # Check wave 5 / point 6 [ /\/\/ ]
                                                ilocs_max_valid = ElliotFuncs.find_line(np.nan, wave.time_5, ilocs_max)
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
                continue

        return finishedWaves, tradingWaves


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
