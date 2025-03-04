import MainStoinker.MainStuff.main_utils as utils
import numpy as np
import pandas as pd
import math

fulldatatypes = ['marker-up','marker-down','marker-dot','line-f']




class ParentAlgo:
    def __init__(self, algoConfigData):
        print("hello :D")
        #Initialize Algo with data from Algo Config
        self.name = algoConfigData['idname']
        self.ticker = algoConfigData['ticker']

        #dont think i want this in the parent class
        # self.stoplossPercent = float(algoConfigData['stoplossPercent'])
        
        self.logger = utils.create_logger("algo:"+self.name+":"+self.ticker)


        #Other inits/variables
        self.inTrade = False
        self.trades = []

        print("Algo " + self.name + " Initialized")

        self.curStockData = 0
        self.curAlgoData = 0
        self.lastAlgoData = 0
        self.AlgoData = 0
        self.FrontEndDataStruct = 0
        self.FrontEndDataType = 0

    def pre_update(self, StockData):
        print("YARGHHH")
        if StockData.shape[0] != self.AlgoData.shape[0]:
            diff = StockData.shape[0] - self.AlgoData.shape[0]
            new_row = pd.DataFrame(index=range(diff),columns=self.DataColumns)
            self.AlgoData = pd.concat([self.AlgoData.loc[:],new_row],ignore_index=True)
        self.AlgoData['time'] = StockData['time']


    def update_frontend(self):
        self.curAlgoData = self.AlgoData.iloc[-1]
        dataToSend = []
        for x in range(0,len(self.FrontEndDataStruct)):
            data = self.curAlgoData[self.FrontEndDataStruct[x]]
            if math.isnan(data):
                data = None
            else:
                if self.FrontEndDataType[x] in fulldatatypes:
                    #TODO: Modify original object instead of creating new one each time: will help with speed optimization if needed
                    df1 = self.AlgoData[['time', self.FrontEndDataStruct[x]]]
                    df1 = df1.dropna(subset=[self.FrontEndDataStruct[x]])
                    df1 = df1.reset_index()
                    df1 = df1[['time',self.FrontEndDataStruct[x]]]
                    fuckingshit = df1['time'].tolist()
                    if "marker-" in self.FrontEndDataType[x]:
                        markerdata = {'name':self.FrontEndDataStruct[x], 'data':fuckingshit, 'type':self.FrontEndDataType[x]}
                        dataToSend.append(markerdata)

                else:
                    dataToSend.append({'name':self.FrontEndDataStruct[x],'data':data, 'type':self.FrontEndDataType[x]})
            
        self.logger.debug(str(self.name) + " - " + str({'idname':self.name, 'time':int(self.curStockData['time']), 'data':dataToSend})) 

        return({'idname':self.name, 'time':int(self.curStockData['time']), 'data':dataToSend})


    def update_frontend_fulldata(self):
        dataToSend = []
        for x in range(0,len(self.FrontEndDataStruct)):
            if self.FrontEndDataType[x] not in fulldatatypes:
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