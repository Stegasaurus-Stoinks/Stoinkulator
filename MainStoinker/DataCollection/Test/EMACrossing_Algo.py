from csv import writer
from datetime import datetime
from datetime import timedelta
import talib as ta
import time


import Start_config as config
import numpy as np
import pandas as pd

from trade import Trade


inTrade = False
enterTime = 0

class Algo:
    def __init__(self, data):
        #print("Starting EMA Crossing Algo with " + str(data))

        #Initialize Algo with data from Algo Config
        self.name = data['idname']
        self.ticker = data['ticker']
        self.EMA1 = data['short']
        self.EMA2 = data['long']

        #Data frame to store data for Algo, (Stoploss, Analysis, Stuff to send to the front end)  
        self.DataColumns = ['StopPrice','TestColumn']
        self.AlgoData = pd.DataFrame(columns=self.DataColumns)
        print(self.AlgoData.shape)

        #Data to send to the frontend
        self.FrontEndDataStruct = ['MA20','MA50','StopPrice']
        self.FrontEndDataType = ['line','line','segment']

        #Other inits/variables
        self.inTrade = False
        self.printInfo = False
        self.trades = []

        print("Algo " + self.name + " Initialized")
        

    def update(self, data):
        StockData = data[0]

        #On first loop, append all warmup data to the AlgoData
        if(self.AlgoData.shape[0] == 0):
            self.AlgoData = pd.concat([self.AlgoData, StockData], axis=0, ignore_index=True)
            #print(self.AlgoData)

        # Adds line to Algo Data so the Algo Data and Stock Data are the same size

        #check if they are the same size, probably dont need this since they should only be called when theres a line added
        # if AAPLdata.shape[0] != self.AlgoData.shape[0]:
        #     diff = AAPLdata.shape[0] - self.AlgoData.shape[0]
        else:
            temp = StockData.iloc[-1]
            temp1 = {'date':[temp['date']], 'time':[temp['time']], 'open':[temp['open']], 'high':[temp['high']], 'low':[temp['low']], 'close':[temp['close']], 'volume':[temp['volume']], 'average':[temp['average']]}
            new_row = pd.DataFrame.from_dict(temp1,orient='columns')
            self.AlgoData = pd.concat([self.AlgoData.loc[:],new_row],ignore_index=True)
            
        
        self.AlgoData['MA20'] = ta.EMA(self.AlgoData['close'],timeperiod=20)
        self.AlgoData['MA50'] = ta.EMA(self.AlgoData['close'],timeperiod=50)

        # print(self.AlgoData)

        curpoint = self.AlgoData.iloc[-1]
        lastpoint = self.AlgoData.iloc[-2]
        #print("|",end="")

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
                self.trade = Trade("AAPL", 10, len(self.trades), enterPrice, enterTime, trend, config.LiveTrading, 0.20,printInfo=False)
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


    def updatefrontend(self):
        dataToSend = []
        for x in range(0,len(self.FrontEndDataStruct)):
            dataToSend.append({'name':self.FrontEndDataStruct[x],'data':self.AlgoData.iloc[-1][self.FrontEndDataStruct[x]], 'type':self.FrontEndDataType[x]})
        return({'idname':self.name, 'data':dataToSend})


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
        try:
            avgLoss = avgLoss/(len(self.trades)-winningTrades)

        except:
            avgLoss = 0

        winRate = winningTrades/len(self.trades) * 100
        
        print("Total Profit: $", round(totalProfit, 3))
        print("Number of Trades: ",len(self.trades))
        print("Win Rate: ",int(winRate),"%")
        print("Average Win: ",round(avgWin, 2))
        print("Average Loss: ",round(avgLoss, 2))