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

from MainStoinker.TradeTools.trade import Trade



class Algo:
    def __init__(self, algoConfigData):
        

        #Initialize Algo with data from Algo Config
        self.name = algoConfigData['idname']
        self.ticker = algoConfigData['ticker']
        self.short = int(algoConfigData['short'])
        self.long = int(algoConfigData['long'])
        self.stoplossPercent = float(algoConfigData['stoplossPercent'])
        self.logger = utils.create_logger("algo:"+self.name+":"+self.ticker)

        self.ibape = IBapi()

        #Data to send to the frontend
       
        self.FrontEndDataStruct = ['MA20','MA50','StopPrice',"Trade"]
        self.FrontEndDataType = ['line','line','segment','baseline']

        #Data frame to store data for Algo ( Uses Front End Data Struct to create dataframe, can add whaterver you want also))  
        self.DataColumns = ['time'] + self.FrontEndDataStruct
        self.AlgoData = pd.DataFrame(columns=self.DataColumns)
        # print(self.AlgoData.shape)

        #Other inits/variables
        self.inTrade = False
        self.trades = []

        print("Algo " + self.name + " Initialized")
        

    def update(self, StockData):


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
            
        
        self.AlgoData['MA20'] = ta.EMA(StockData['close'],timeperiod=self.short)
        self.AlgoData['MA50'] = ta.EMA(StockData['close'],timeperiod=self.long)

        # print(self.AlgoData)

        #Variables to store most recent 2 stock data and algo data 
        self.curStockData = StockData.iloc[-1]
        self.curAlgoData = self.AlgoData.iloc[-1]
        self.lastAlgoData = self.AlgoData.iloc[-2]

        # datapoint print for current minute
        # print(str(self.ticker) + " : " + str(self.curStockData['close']))

        if self.inTrade:
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
                print("Closing trade due to opposing signal detected")
                self.inTrade = False
                closeTime = self.curStockData['date']
                closePrice = self.curStockData['close']
                self.logger.info("***trend cross close position***")
                self.trade.close_position(closePrice,closeTime)
                self.trade.get_stats()

            
            if trend:
                print("Crossing Up!")
                self.inTrade = True
                enterTime = self.curStockData['date']
                enterPrice = self.curStockData['close']
                self.trade = 0
                
                self.logger.info("***opening trade on cross-up***")
                tradeid = str(self.name) + str(len(self.trades))
                self.trade = Trade(self.ticker, 10, tradeid, enterPrice, enterTime, trend, (self.stoplossPercent/100), self.logger)
                self.trades.append(self.trade)
                # ime.sleep(1)


            else:
                print("Crossing Down!")

            


        
        
        # if not self.inTrade:
        #     trend = 1
        #     self.logger.info("***opening trade just because***")
        #     self.inTrade = True
        #     enterTime = self.curStockData['date']
        #     enterPrice = self.curStockData['close']
        #     # self.trade = 0

        #     tradeid = str(self.name) + str(len(self.trades))
        #     self.trade = Trade(self.ticker, 10, tradeid, enterPrice, enterTime, trend, (self.stoplossPercent/100), self.logger)
        #     self.trades.append(self.trade)



        self.AlgoData['time'] = StockData['time']

        self.curAlgoData = self.AlgoData.iloc[-1]

       


    def update_frontend(self):
        dataToSend = []
        for x in range(0,len(self.FrontEndDataStruct)):
            data = self.curAlgoData[self.FrontEndDataStruct[x]]
            if math.isnan(data):
                data = None
            else:
                dataToSend.append({'name':self.FrontEndDataStruct[x],'data':data, 'type':self.FrontEndDataType[x]})
            
        self.logger.debug(str(self.name) + " - " + str({'idname':self.name, 'time':int(self.curStockData['time']), 'data':dataToSend})) 

        return({'idname':self.name, 'time':int(self.curStockData['time']), 'data':dataToSend})
    

    def update_frontend_fulldata(self):
        dataToSend = []
        for x in range(0,len(self.FrontEndDataStruct)):
            data = self.AlgoData[['time',self.FrontEndDataStruct[x]]]
            data.rename(columns = {self.FrontEndDataStruct[x]:'value'}, inplace = True)
            data = data.to_json(orient="records")
            dataToSend.append({'name':self.FrontEndDataStruct[x],'data':data, 'type':self.FrontEndDataType[x]})
            
        return({'idname':self.name, 'data':dataToSend})
    
    
    def printtrades(self):
        print(self.trades)


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

    def save_trades(self):
        tradeDataframe = pd.DataFrame.from_records([trade.to_json() for trade in self.trades])
        return tradeDataframe