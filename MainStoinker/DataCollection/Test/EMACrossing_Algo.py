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
    def __init__(self, data):

        print("Starting EMA Crossing Algo with " + str(data))
        self.ticker = data['ticker']
        self.EMA1 = data['short']
        self.EMA2 = data['long']
        self.inTrade = False
        self.printInfo = True
        self.trades = []

        self.DataColumns = {'StopPrice','TestColumn'}

        self.AlgoData = pd.DataFrame(columns= self.DataColumns)


        print("Algo Initialized")
        

    def update(self, data):
        AAPLdata = data[0]

        # Adds line to Algo Data so the Algo Data and Stock Data are the same size
        if AAPLdata.shape[0] != self.AlgoData.shape[0]:
            diff = AAPLdata.shape[0] - self.AlgoData.shape[0]
            new_row = pd.DataFrame(columns = self.DataColumns, index=np.empty(diff))
            self.AlgoData = pd.concat([self.AlgoData.loc[:],new_row],ignore_index=True)
            
        # print(AAPLdata)
        AAPLdata['MA20'] = ta.SMA(AAPLdata['close'],timeperiod=20)
        AAPLdata['MA50'] = ta.SMA(AAPLdata['close'],timeperiod=50)
        # print(AAPLdata)
        # print(self.AlgoData)

        curpoint = AAPLdata.iloc[-1]
        lastpoint = AAPLdata.iloc[-2]
        print("|",end="")

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
                closeTime = curpoint['date']
                closePrice = curpoint['close']
                self.trade.closePosition(closePrice,closeTime)
                self.trade.getStats()

            
            if trend:
                self.printStuff("Crossing Up!")
                self.inTrade = True
                enterTime = curpoint['date']
                enterPrice = curpoint['close']
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
                self.trade.closePosition(self.trade.stopPrice,curpoint['date'])
                self.printStuff("Closing position based on stoploss")
                self.inTrade = False
            # time.sleep(1)

            # Update AlgoData with newest StopPrice Data
            self.AlgoData.at[self.AlgoData.index[-1],'StopPrice'] = self.trade.stopPrice

            #End of day trade closing
            endofDay = curpoint['date'].replace(hour=12, minute=55, second=0, microsecond=0)
            if curpoint['date'] > endofDay:
                self.trade.closePosition(curpoint['close'],curpoint['date'])
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

    def getTickers(self):
        return self.ticker