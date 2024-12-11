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

Detect Fair Value Gaps:  Not planning on trading anything yet, just detecting them as the ticker moves and possibly detecting bounces off off them.

Grab liquidity, load into it and then bounce up like pushing into a rubber band

Still in concept mode...

'''

class Algo(ParentAlgo):
    def __init__(self, algoConfigData):
        super().__init__(algoConfigData)

        self.ibape = IBapi()
        #algo_config
        # self.RRRatio = float(algoConfigData['RRRatio'])

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
        print("looking for FVGs")
        
        #initState
        if self.TradeState.current_state_value == 'initState':
            
            #check last 3 candles if all going in the same direction
            if isupcandle(StockData.iloc[-1]) and isupcandle(StockData.iloc[-2]) and isupcandle(StockData.iloc[-3]):
                print("last 3 going up!")

                #check for gap
                if StockData.iloc[-1]["low"] - StockData.iloc[-3]["high"] > 0.03:
                    print("FVG Detected")
                    self.upperbound = StockData.iloc[-1]["low"]
                    self.lowerbound = StockData.iloc[-3]["high"]

                    



            elif isdowncandle(StockData.iloc[-1]) and isdowncandle(StockData.iloc[-2]) and isdowncandle(StockData.iloc[-3]):
                print("last 3 going down!")

            if self.upperbound is not 0:
                self.AlgoData.at[self.AlgoData.index[-1],'UpperBound'] = self.upperbound
                self.AlgoData.at[self.AlgoData.index[-1],'LowerBound'] = self.lowerbound

        #loadingState
        elif self.TradeState.current_state_value == 'loadingState':
            print("loading")


        #bounceState
        elif self.TradeState.current_state_value == 'bounceState':
            print("bouncing")
                    

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



class AlgoLogic(StateMachine):
 
    # creating states

    #starting state, defines first candle + bounds
    initState = State("init", initial = True)

    #waiting for something to happen (breakout of range in a direction)
    loadingState = State("loading")

    #broken out, waiting for a retest of the level
    bounceState = State("bounce")

    #time to enter a trade :)
    inTradeState = State("inTrade")

    #done for the day, stop checking anything else
    doneTradingState = State("done")
      

    # transitions of the state
    approachedFVG = initState.to(loadingState)
    returntoinit = loadingState.to(initState)
    launching = loadingState.to(bounceState)
    retestconfirmed = bounceState.to(inTradeState)
    # retestfailed = breakoutState.to(waitingState)
    # closeout = inTradeState.to(doneTradingState)
    cancel = initState.to(doneTradingState)


def isupcandle(candle):
    if candle["open"] < candle["close"]:
        return True
    
def isdowncandle(candle):
    if candle["open"] > candle["close"]:
        return True