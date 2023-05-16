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
                self.trade = Trade("AAPL", 10, 1, enterPrice, enterTime, trend, config.LiveTrading, 0.20,printInfo=False)
                self.trades.append(self.trade)


            else:
                self.printStuff("Crossing Down!")

            time.sleep(1)

        if self.inTrade:
            self.printStuff("In a trade")
            if not self.trade.check(curpoint):
                # if self.trade.stopPrice < curpoint['Close']:
                #     self.trade.closePosition(curpoint['Close'],curpoint['Date'])
                # else:
                self.trade.closePosition(self.trade.stopPrice,curpoint['Date'])
                self.printStuff("Closing position based on stoploss")
                self.inTrade = False
            # time.sleep(1)


            #End of day trade closing
            endofDay = curpoint['Date'].replace(hour=12, minute=55, second=0, microsecond=0)
            if curpoint['Date'] > endofDay:
                self.trade.closePosition(curpoint['Close'],curpoint['Date'])
                self.printStuff("Closing position based on end of day")
                self.inTrade = False




    def printStuff(self,stuff):
        if self.printInfo:
            print(stuff)

    def printStats(self,FullPrint):
        totalProfit = 0
        winningTrades = 0
        avgWin = 0
        avgLoss = 0
        print("Total Trades Placed: ", len(self.trades))
        for trade in self.trades:
            trade.getStats(FullPrint)
            tempProfit = trade.getProfit()
            totalProfit += tempProfit
            if tempProfit > 0:
                winningTrades += 1
                avgWin += tempProfit

            else:
                avgLoss += tempProfit
            
        avgWin = avgWin/winningTrades
        avgLoss = avgLoss/(len(self.trades)-winningTrades)
        winRate = winningTrades/len(self.trades) * 100
        
        print("Total Profit: $", round(totalProfit, 3))
        print("Number of Trades: ",len(self.trades))
        print("Win Rate: ",int(winRate),"%")
        print("Average Win: ",round(avgWin, 2))
        print("Average Loss: ",round(avgLoss, 2))



