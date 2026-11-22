from csv import writer
from datetime import datetime
from datetime import timedelta
import talib as ta
import time

import MainStoinker.MainStuff.main_utils as utils
import numpy as np
import pandas as pd
import math

from statemachine import StateMachine, State

from MainStoinker.Algos.ParentAlgo import ParentAlgo

'''
ALGO PLAN/IDEA:

Psychological Trading Algo, 
Friday Frenzy, Using momentum beginning of day on fridays to scalp trade based on first 15 min candle

Still in concept mode...

'''

# End-of-day cut-off as (hour, minute), in the data's LOCAL time (US Pacific; the session
# runs 06:30-12:59). Five minutes before the session end; confirmed by the repo owner.
# A trade still open on a bar after this minute is closed, and nothing is opened at or after it.
END_OF_DAY_CUTOFF = (12, 55)

class Algo(ParentAlgo):
    def __init__(self, algoConfigData):
        super().__init__(algoConfigData)

        #algo_config
        self.RRRatio = float(algoConfigData['RRRatio'])

        #duration we wait for a retest before its become too long and invalid
        self.retestTimeout = 20

        #Data to send to the frontend
        self.FrontEndDataStruct = ['UpperBound','LowerBound','tp','StopPrice',"Trade"]
        self.FrontEndDataType = ['segment','segment','segment','segment','baseline']

        #Data frame to store data for Algo ( Uses Front End Data Struct to create dataframe, can add whaterver you want also))  
        self.DataColumns = ['time'] + self.FrontEndDataStruct
        self.AlgoData = pd.DataFrame(columns=self.DataColumns)
        # print(self.AlgoData.shape)

        self.TradeState = AlgoLogic()
        print(self.TradeState.current_state)

        self.firstcandletime = 0
        self.upperbound = 0
        self.lowerbound = 0
        self.tp = 0

        #calendar date of the trading day being worked on (a bar from another date resets the algo)
        self.tradingDay = None

        self.inTrade = False
        

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

        
        #new calendar day (the bar's own date): start over BEFORE processing this bar,
        #which then acts as the first candle of the new day
        if self.curStockData['date'].date() != self.tradingDay:
            if self.tradingDay is not None:
                self.new_day()
            self.tradingDay = self.curStockData['date'].date()

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

            #frontend
            self.AlgoData.at[self.AlgoData.index[-1],'UpperBound'] = self.upperbound
            self.AlgoData.at[self.AlgoData.index[-1],'LowerBound'] = self.lowerbound


        #waitingState
        elif self.TradeState.current_state_value == 'waitingState':
            print("waiting")

            #frontend
            self.AlgoData.at[self.AlgoData.index[-1],'UpperBound'] = self.upperbound
            self.AlgoData.at[self.AlgoData.index[-1],'LowerBound'] = self.lowerbound

            #check if range is too small? (cancel trade if it is?)

            #check for break outside of range:
            if self.curStockData['close'] > self.upperbound or self.curStockData['close'] < self.lowerbound:
                print('detected a potential break...')
                #check to make sure candle stradled the bounds and didnt gap across bounds
                if self.curStockData['open'] > self.lowerbound and self.curStockData['open'] < self.upperbound:
                    print('breakout confirmed')

                    #breakout direction:
                    if self.curStockData['close'] > self.upperbound:
                        self.direction = 'up'
                    else:
                        self.direction = 'down'

                    print("breakout direction: ", self.direction)
                    
                    self.breakout_time = self.curStockData['date']
                    self.TradeState.breakoutconfirmed()

        #breakoutState
        elif self.TradeState.current_state_value == 'breakoutState':

            #frontend
            self.AlgoData.at[self.AlgoData.index[-1],'UpperBound'] = self.upperbound
            self.AlgoData.at[self.AlgoData.index[-1],'LowerBound'] = self.lowerbound


            #check for retest
            print('waitng for retest')

            #check if its been too long since breakout for retest to be valid
            time_difference = self.curStockData['date'] - self.breakout_time
            if ((time_difference.total_seconds() / 60) > self.retestTimeout):
                print("Retest took too long")
                self.TradeState.retesttimout()
            elif self.curStockData['date'] >= self.end_of_day():
                print("End of day reached before a retest, done for the day")
                self.TradeState.retesttimout()

            #if down direction, check for retest is high of candle is above lowerbound and close is below range
            wiggleroom = 0.05
            if self.TradeState.current_state_value == 'breakoutState' and self.direction == 'down':
                if self.curStockData['high'] >= self.lowerbound-wiggleroom and self.curStockData['close'] < self.lowerbound:
                    print('retest completed')
                    self.entertrade()
                    self.TradeState.retestconfirmed()

                if self.curStockData['close'] > self.lowerbound:
                    print('retest failed, trade conditions invalid')
                    self.TradeState.retestfailed()

            if self.TradeState.current_state_value == 'breakoutState' and self.direction == 'up':
                if self.curStockData['low'] <= self.upperbound+wiggleroom and self.curStockData['close'] > self.upperbound:
                    print('retest completed')
                    self.entertrade()
                    self.TradeState.retestconfirmed()

                if self.curStockData['close'] < self.upperbound:
                    print('retest failed, trade conditions invalid')
                    self.TradeState.retestfailed()
            
                    

        #inTradeState
        elif self.TradeState.current_state_value == 'inTradeState':

            
            print("In a trade")

            # logic for manual stoploss
            if not self.trade.check_stoploss(self.curStockData):
                self.logger.debug("***received false from check_stoploss***")
                self.inTrade = False
                self.TradeState.closeout()

            elif self.trade.check_tp(self.curStockData):
                self.logger.debug("***received true from check_tp***")
                self.inTrade = False
                self.TradeState.closeout()

            #End of day trade closing (same rule as EMACrossing: from the first bar after the cut-off)
            elif self.curStockData['date'] > self.end_of_day():
                self.logger.info("***end of day close position***")
                self.trade.close_position(self.curStockData['close'],self.curStockData['date'])
                print("Closing position based on end of day")
                self.inTrade = False
                self.TradeState.closeout()

            #frontend
            self.AlgoData.at[self.AlgoData.index[-1],'UpperBound'] = self.upperbound
            self.AlgoData.at[self.AlgoData.index[-1],'LowerBound'] = self.lowerbound

                # Update AlgoData with newest StopPrice Data
            self.AlgoData.at[self.AlgoData.index[-1],'StopPrice'] = self.trade.stopPrice
            self.AlgoData.at[self.AlgoData.index[-1],'Trade'] = self.curStockData['close']
            self.AlgoData.at[self.AlgoData.index[-1],'tp'] = self.tp

            

        #doneTradingState
        elif self.TradeState.current_state_value == 'doneTradingState':
            print("Done for the day :)")


        self.AlgoData['time'] = StockData['time']

        self.curAlgoData = self.AlgoData.iloc[-1]


    def end_of_day(self):
        #cut-off minute on the current bar's date
        return self.curStockData['date'].replace(hour=END_OF_DAY_CUTOFF[0], minute=END_OF_DAY_CUTOFF[1], second=0, microsecond=0)


    def new_day(self):
        #a trade should already have been closed at the end of the previous day, but if the data
        #ended early (short session) close it now, at this bar, before forgetting about it
        if self.inTrade:
            self.logger.warning("trade still open at the start of a new trading day, closing it at this bar's close")
            self.close_trade()

        #fresh state machine (back to init) and per-day values, trade history (self.trades) is kept
        self.TradeState = AlgoLogic()
        self.firstcandletime = 0
        self.upperbound = 0
        self.lowerbound = 0
        self.tp = 0
        self.direction = None
        self.breakout_time = None
        self.inTrade = False


    def entertrade(self):
        range = self.upperbound - self.lowerbound

        if self.direction == 'up':
            trend = 1
            self.tp = round(self.upperbound+(self.RRRatio*range),2)
            #sets stoploss at the opposite side of the range
            self.stoplossprice = self.lowerbound

        else:
            trend = 0
            self.tp = round(self.lowerbound-(self.RRRatio*range),2)
            #sets stoploss at the opposite side of the range
            self.stoplossprice = self.upperbound
 
        
        self.trade = self.open_trade(10, trend)

        #change stoploss to fixed type and set price
        self.trade.set_stopLossType('Fixed')
        self.trade.set_stopPrice(self.stoplossprice)

        # TODO: gonna need a system to take profits... (sort of done? need to add live stuff)
        # use self.RRRatio

        self.trade.create_tp(self.tp,10)

        self.AlgoData.at[self.AlgoData.index[-1],'StopPrice'] = self.trade.stopPrice
        self.AlgoData.at[self.AlgoData.index[-1],'Trade'] = self.curStockData['close']
        self.AlgoData.at[self.AlgoData.index[-1],'tp'] = self.tp




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
    doneTradingState = State("done", final=True)
      

    # transitions of the state
    firstcandleconfirmed = initState.to(waitingState)
    breakoutconfirmed = waitingState.to(breakoutState)
    retesttimout = breakoutState.to(doneTradingState)
    retestconfirmed = breakoutState.to(inTradeState)
    retestfailed = breakoutState.to(waitingState)
    closeout = inTradeState.to(doneTradingState)
    cancel = waitingState.to(doneTradingState)