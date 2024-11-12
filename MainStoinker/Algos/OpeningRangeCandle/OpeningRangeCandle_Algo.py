from csv import writer
from datetime import datetime
from datetime import timedelta
import talib as ta
import time

from MainStoinker.DataCollection.apiApi import IBapi
import MainStoinker.MainStuff.main_utils as utils
import numpy as np
import pandas as pd
import math

from statemachine import StateMachine, State

from MainStoinker.TradeTools.trade import Trade

from MainStoinker.Algos.ParentAlgo import ParentAlgo

'''
ALGO PLAN/IDEA:

Once opening candle has fully formed, record the min and max values.
Use these values as guidlines for what the market initially considered as a fair price for the ticker.

Wait for a break (candle closes outside the "fair value range"), 
then wait for a retest of the level, a pull back where the close of candle is still outside of the range but the min or max is in the range
(basically means the price climbed back within range but pulled back out indicating a strong move in the direction of the break)

set 2:1 RR trade with stop at opposite side of "fair value range"

(would be interesting to check different time frames for each ticker, recommended is 5min to establish solid range but not have trade take too long where you loose morning momentum)

'''

class Algo(ParentAlgo):
    def __init__(self, algoConfigData):
        super().__init__(algoConfigData)

        self.ibape = IBapi()

        #Data to send to the frontend
        self.FrontEndDataStruct = ['UpperBound','LowerBound','StopPrice',"Trade"]
        self.FrontEndDataType = ['line','line','segment','baseline']

        #Data frame to store data for Algo ( Uses Front End Data Struct to create dataframe, can add whaterver you want also))  
        self.DataColumns = ['time'] + self.FrontEndDataStruct
        self.AlgoData = pd.DataFrame(columns=self.DataColumns)
        # print(self.AlgoData.shape)

        self.TradeState = AlgoLogic()
        print(self.TradeState.current_state)

        self.firstcandletime = 0
        self.upperbound = 0
        self.lowerbound = 0
        

    def update(self, StockData):


        # check if they are the same size, probably dont need this since they should only be called when theres a line added
        if StockData.shape[0] != self.AlgoData.shape[0]:
            diff = StockData.shape[0] - self.AlgoData.shape[0]
            new_row = pd.DataFrame(index=range(diff),columns=self.DataColumns)
            self.AlgoData = pd.concat([self.AlgoData.loc[:],new_row],ignore_index=True)

        # print(self.AlgoData)

        #Variables to store most recent 2 stock data and algo data 
        self.curStockData = StockData.iloc[-1]
        self.curAlgoData = self.AlgoData.iloc[-1]
        self.lastAlgoData = self.AlgoData.iloc[-2]

        # datapoint print for current minute
        # print(str(self.ticker) + " : " + str(self.curStockData['close']))

        
        #initState
        if self.TradeState.current_state_value == 'initState':
            #check if new data received is really new data and first candle is fully closed
            if self.firstcandletime != 0:
                if self.firstcandletime != self.curStockData['time']:
                    self.TradeState.firstcandleconfirmed()
                    return

            self.firstcandle = self.curStockData
            self.firstcandletime = self.firstcandle['time']
            self.lowerbound = self.firstcandle['low']
            self.upperbound = self.firstcandle['high']

            print("first candle confirmed, upperbound: ", self.upperbound, " lowerbound: ", self.lowerbound)


        #waitingState
        if self.TradeState.current_state_value == 'waitingState':
            print("waiting")
            self.AlgoData.at[self.AlgoData.index[-1],'UpperBound'] = self.upperbound
            self.AlgoData.at[self.AlgoData.index[-1],'LowerBound'] = self.lowerbound

            #check if range is too small? (cancel trade if it is?)

            #check for break outside of range:
            if self.curStockData['close'] > self.upperbound or self.curStockData['close'] < self.lowerbound:
                print('detected a potential break...')
                #check to make sure candle stradled the bounds and didnt gap down
                if self.curStockData['open'] > self.lowerbound and self.curStockData['open'] < self.upperbound:
                    print('breakout confirmed')

                    #breakout direction:
                    if self.curStockData['close'] > self.upperbound:
                        self.direction = 'up'
                    else:
                        self.direction = 'down'

                    print("breakout direction: ", self.direction)
                    
                    self.TradeState.breakoutconfirmed()

        #breakoutState
        if self.TradeState.current_state_value == 'breakoutState':
            #check for retest
            print('waitng for retest')
                    

        #inTradeState
        if self.TradeState.current_state_value == 'inTradeState':
            print("In a trade")
            
            # logic for manual stoploss
            if not self.trade.check_stoploss(self.curStockData):
                self.logger.debug("***received false from check_stoploss***")
                self.inTrade = False

            else:
                # Update AlgoData with newest StopPrice Data
                self.AlgoData.at[self.AlgoData.index[-1],'StopPrice'] = self.trade.stopPrice

                # Update AlgoData with trade data (midpoint of price data)
                if self.trade.openTime == self.curStockData['time']: #if this is the first point in the trade
                    midpoint = self.curStockData['close']
                else:

                    diff = self.curStockData['close'] - self.curStockData['open']
                    if diff > 0:
                        midpoint = self.curStockData['open'] + (diff/2)
                    else:
                        midpoint = self.curStockData['close'] - (diff/2)

                self.AlgoData.at[self.AlgoData.index[-1],'Trade'] = midpoint

                #End of day trade closing
                endofDay = self.curStockData['date'].replace(hour=12, minute=55, second=0, microsecond=0)
                if self.curStockData['date'] > endofDay:
                    self.logger.info("***end of day close position***")
                    self.trade.close_position(self.curStockData['close'],self.curStockData['date'])
                    print("Closing position based on end of day")
                    self.inTrade = False


        #doneTradingState
        if self.TradeState.current_state_value == 'doneTradingState':
            print("Done for the day :)")


        self.AlgoData['time'] = StockData['time']

        self.curAlgoData = self.AlgoData.iloc[-1]




class AlgoLogic(StateMachine):
 
    # creating states

    #starting state, defines first candle + bounds
    initState = State("init", initial = True)

    #waiting for something to happen (breakout of range in a direction)
    waitingState = State("waiting")

    #broken out, waiting for a retest of the level
    breakoutState = State("breakout")

    #time to enter a trade :)
    inTradeState = State("inTrade")

    #done for the day, stop checking anything else
    doneTradingState = State("done")
      

    # transitions of the state
    firstcandleconfirmed = initState.to(waitingState)
    breakoutconfirmed = waitingState.to(breakoutState)
    retestconfirmed = breakoutState.to(inTradeState)
    closeout = inTradeState.to(doneTradingState)
    cancel = waitingState.to(doneTradingState)