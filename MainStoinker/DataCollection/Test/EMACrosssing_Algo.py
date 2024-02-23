from csv import writer
from datetime import datetime
from datetime import timedelta
import talib as ta
import time

from livedatacollect4 import IBapi

import Start_config as config
import numpy as np
import pandas as pd
import math

from trade import Trade


inTrade = False
enterTime = 0

class Algo:
    def __init__(self, algoConfigData):
        #print("Starting EMA Crossing Algo with " + str(data))

        #Initialize Algo with data from Algo Config
        self.name = algoConfigData['idname']
        self.ticker = algoConfigData['ticker']
        self.EMA1 = algoConfigData['short']
        self.EMA2 = algoConfigData['long']

        self.ibape = IBapi()

        #Data frame to store data for Algo, (Stoploss, Analysis, Stuff to send to the front end)  
        self.DataColumns = ['time','StopPrice','MA20','MA50']
        self.AlgoData = pd.DataFrame(columns=self.DataColumns)
        print(self.AlgoData.shape)

        #Data to send to the frontend
       
        self.FrontEndDataStruct = ['MA20','MA50','StopPrice']
        self.FrontEndDataType = ['line','line','segment']

        #Other inits/variables
        self.inTrade = False
        self.printInfo = True
        self.trades = []

        self.ibkrApi = None

        print("Algo " + self.name + " Initialized")
        

    def update(self, StockData):
        # print("algo update readPositions")
        # print(self.ibape.readPositions())
        # print("Stock DAta in Ticker")
        # print(StockData)

        # check if they are the same size, probably dont need this since they should only be called when theres a line added
        if StockData.shape[0] != self.AlgoData.shape[0]:
            diff = StockData.shape[0] - self.AlgoData.shape[0]
            new_row = pd.DataFrame(index=range(diff),columns=self.DataColumns)
            self.AlgoData = pd.concat([self.AlgoData.loc[:],new_row],ignore_index=True)

        # else:
        # temp = StockData.iloc[-1]
        # temp1 = {'date':[temp['date']], 'time':[temp['time']], 'open':[temp['open']], 'high':[temp['high']], 'low':[temp['low']], 'close':[temp['close']], 'volume':[temp['volume']], 'average':[temp['average']]}
        # new_row = pd.DataFrame.from_dict(temp1,orient='columns')
        # self.AlgoData = pd.concat([self.AlgoData.loc[:],new_row],ignore_index=True)
            
        
        self.AlgoData['MA20'] = ta.EMA(StockData['close'],timeperiod=20)
        self.AlgoData['MA50'] = ta.EMA(StockData['close'],timeperiod=50)

        # print(self.AlgoData)

        #Variables to store most recent 2 stock data and algo data 
        self.curStockData = StockData.iloc[-1]
        self.curAlgoData = self.AlgoData.iloc[-1]
        self.lastAlgoData = self.AlgoData.iloc[-2]

        if self.lastAlgoData['MA20'] > self.lastAlgoData['MA50']:
            prevtrend = 1
        else:
            prevtrend = 0

        if self.curAlgoData['MA20'] > self.curAlgoData['MA50']:
            trend = 1
        else:
            trend = 0

        if prevtrend != trend: # Check if trend has changed
            if(self.inTrade):
                #close trade because we want to open one in a different direction
                self.printStuff("Closing trade due to opposing signal detected")
                self.inTrade = False
                closeTime = self.curStockData['date']
                closePrice = self.curStockData['close']
                self.trade.closePosition(closePrice,closeTime)
                self.trade.getStats()

            
            if trend:
                self.printStuff("Crossing Up!")
                self.inTrade = True
                enterTime = self.curStockData['date']
                enterPrice = self.curStockData['close']
                #Trade(symbol, volume, ID, openPrice, openTime, direction, live, stoploss, API, printinfo)
                self.trade = 0

                # print(self.ibkrApi)
                # print("trying reqPositions")
                # API.reqPositions()
                # time.sleep(3)
                # print("trying to read positions from algo object")
                # print(API.readPositions())
                # print("done trying to read positions from algo object")
                

                self.trade = Trade("AAPL", 10, len(self.trades), enterPrice, enterTime, trend, config.LiveTrading, 0.20, self.ibkrApi, printInfo=False)
                self.trades.append(self.trade)
                time.sleep(1)


            else:
                self.printStuff("Crossing Down!")

            

        if self.inTrade:
            self.printStuff("In a trade")
            if not self.trade.check(self.curStockData):
                self.trade.closePosition(self.trade.stopPrice,self.curStockData['date'])
                self.printStuff("Closing position based on stoploss")
                self.inTrade = False
            # time.sleep(1)

            # Update AlgoData with newest StopPrice Data
            self.AlgoData.at[self.AlgoData.index[-1],'StopPrice'] = self.trade.stopPrice

            #End of day trade closing
            endofDay = self.curStockData['date'].replace(hour=12, minute=55, second=0, microsecond=0)
            if self.curStockData['date'] > endofDay:
                self.trade.closePosition(self.curStockData['close'],self.curStockData['date'])
                self.printStuff("Closing position based on end of day")
                self.inTrade = False

        self.AlgoData['time'] = StockData['time']

        self.curAlgoData = self.AlgoData.iloc[-1]

        # print(self.AlgoData)
        # print(StockData['date'])
       


    def updatefrontend(self):
        dataToSend = []
        for x in range(0,len(self.FrontEndDataStruct)):
            data = self.curAlgoData[self.FrontEndDataStruct[x]]
            if math.isnan(data):
                data = None
            else:
                dataToSend.append({'name':self.FrontEndDataStruct[x],'data':data, 'type':self.FrontEndDataType[x]})
            
        return({'idname':self.name, 'time':int(self.curStockData['time']), 'data':dataToSend})
    

    def updatefrontendfulldata(self):
        dataToSend = []
        for x in range(0,len(self.FrontEndDataStruct)):
            data = self.AlgoData[['time',self.FrontEndDataStruct[x]]]
            data.rename(columns = {self.FrontEndDataStruct[x]:'value'}, inplace = True)
            data = data.to_json(orient="records")
            dataToSend.append({'name':self.FrontEndDataStruct[x],'data':data, 'type':self.FrontEndDataType[x]})
            
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