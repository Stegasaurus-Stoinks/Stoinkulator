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

Psychological Trading Algo, 
Friday Frenzy, Using momentum beginning of day on fridays to scalp trade based on first 15 min candle

Still in concept mode...

'''

class Algo(ParentAlgo):
    def __init__(self, algoConfigData):
        super().__init__(algoConfigData)

        self.ibape = IBapi()
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
                if self.curStockData['open'] > self.lowerbound or self.curStockData['open'] < self.upperbound:
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

            #if down direction, check for retest is high of candle is above lowerbound and close is below range
            wiggleroom = 0.05
            if self.direction == 'down':
                if self.curStockData['high'] >= self.lowerbound-wiggleroom and self.curStockData['close'] < self.lowerbound:
                    print('retest completed')
                    self.entertrade()
                    self.TradeState.retestconfirmed()

                if self.curStockData['close'] > self.lowerbound:
                    print('retest failed, trade conditions invalid')
                    self.TradeState.retestfailed()

            if self.direction == 'up':
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

            if self.trade.check_tp(self.curStockData):
                self.logger.debug("***received true from check_tp***")
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


    def entertrade(self):
        self.inTrade = True
        enterTime = self.curStockData['date']
        enterPrice = self.curStockData['close']
        self.trade = 0

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

        
        
        
        self.logger.info("***opening trade***")
        tradeid = str(self.name) + str(len(self.trades))
        self.trade = Trade(self.ticker, 10, tradeid, enterPrice, enterTime, trend, 1, self.logger)

        #change stoploss to fixed type and set price
        self.trade.change_stoploss_type('Fixed')
        self.trade.update_stoploss_price(self.stoplossprice)

        self.trades.append(self.trade)

        # TODO: gonna need a system to take profits... (sort of done? need to add live stuff)
        # use self.RRRatio

        self.trade.create_tp(self.tp)

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
    doneTradingState = State("done")
      

    # transitions of the state
    firstcandleconfirmed = initState.to(waitingState)
    breakoutconfirmed = waitingState.to(breakoutState)
    retesttimout = breakoutState.to(doneTradingState)
    retestconfirmed = breakoutState.to(inTradeState)
    retestfailed = breakoutState.to(waitingState)
    closeout = inTradeState.to(doneTradingState)
    cancel = waitingState.to(doneTradingState)