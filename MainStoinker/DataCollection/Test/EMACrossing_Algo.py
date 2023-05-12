from csv import writer
from datetime import datetime
from datetime import timedelta
import talib as ta
import time


import Start_config as config
import numpy as np
import pandas as pd

from trade import Trade

AlgoData = pd.DataFrame()

inTrade = False
enterTime = 0

class Algo:
    def __init__(self, ticker, EMA1, EMA2, printInfo):
        self.ticker = ticker
        self.EMA1 = EMA1
        self.EMA2 = EMA2
        self.inTrade = False
        self.printInfo = printInfo
        self.trades = []

    def update(self, data):
        AAPLdata = data[0]
        # print(AAPLdata)
        AAPLdata['MA20'] = ta.SMA(AAPLdata['Close'],timeperiod=20)
        AAPLdata['MA50'] = ta.SMA(AAPLdata['Close'],timeperiod=50)
        # print(AAPLdata)

        curpoint = AAPLdata.iloc[-1]
        lastpoint = AAPLdata.iloc[-2]

        if lastpoint['MA20'] > lastpoint['MA50']:
            prevtrend = 1
        else:
            prevtrend = 0

        if curpoint['MA20'] > curpoint['MA50']:
            trend = 1
        else:
            trend = 0

        if prevtrend != trend: # Check if trend has changed
            if(self.inTrade):
                #close trade because we want to open one in a different direction
                self.printStuff("Closing trade due to opposing signal detected")
                self.inTrade = False
                closeTime = curpoint['Date']
                closePrice = curpoint['Close']
                self.trade.closePosition(closePrice,closeTime)
                self.trade.getStats()

            
            if trend:
                self.printStuff("Crossing Up!")
                self.inTrade = True
                enterTime = curpoint['Date']
                enterPrice = curpoint['Close']
                #Trade(symbol, volume, ID, openPrice, openTime, direction, live, stoploss)
                self.trade = Trade("AAPL", 10, 1, enterPrice, enterTime, trend, config.LiveTrading, 0.20)
                self.trades.append(self.trade)


            else:
                self.printStuff("Crossing Down!")

            # self.inTrade = True
            # enterTime = curpoint['Date']
            # enterPrice = curpoint['Close']
            # #Trade(symbol, volume, ID, openPrice, openTime, direction, live, stoploss)
            # self.trade = Trade("AAPL", 10, 1, enterPrice, enterTime, trend, config.LiveTrading, 0.20)
           

            time.sleep(1)

        if self.inTrade:
            print("In a trade")
            if not self.trade.check(curpoint['Close']):
                self.trade.closePosition(curpoint['Close'],curpoint['Date'])
                self.printStuff("Closing position based on stoploss")
                self.inTrade = False
            time.sleep(.1)




    def printStuff(self,stuff):
        if self.printInfo:
            print(stuff)

    def printStats(self):
        for trade in self.trades:
            trade.getStats()

