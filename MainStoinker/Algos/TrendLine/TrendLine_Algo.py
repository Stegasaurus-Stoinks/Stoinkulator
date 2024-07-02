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

from scipy.signal import argrelextrema

from MainStoinker.TradeTools.trade import Trade

from MainStoinker.Algos.ParentAlgo import ParentAlgo



class Algo(ParentAlgo):
    def __init__(self, algoConfigData):
        super().__init__(algoConfigData)

        #Initialize Algo with unique data from Algo Config
        self.short = int(algoConfigData['short'])
        self.long = int(algoConfigData['long'])

        self.ibape = IBapi()

        #Data to send to the frontend
        self.FrontEndDataStruct = ['MA20','upperband','middleband','lowerband','StopPrice',"Trade"]
        self.FrontEndDataType = ['line','line','line','line','segment','baseline']

        #Data frame to store data for Algo ( Uses Front End Data Struct to create dataframe, can add whaterver you want also))  
        self.DataColumns = ['time'] + self.FrontEndDataStruct
        self.AlgoData = pd.DataFrame(columns=self.DataColumns)
        # print(self.AlgoData.shape)
        

    def update(self, StockData):


        # check if they are the same size, probably dont need this since they should only be called when theres a line added
        if StockData.shape[0] != self.AlgoData.shape[0]:
            diff = StockData.shape[0] - self.AlgoData.shape[0]
            new_row = pd.DataFrame(index=range(diff),columns=self.DataColumns)
            self.AlgoData = pd.concat([self.AlgoData.loc[:],new_row],ignore_index=True)
            
        
        self.AlgoData['MA20'] = ta.HT_TRENDLINE(StockData['close'])
        # print(ta.HT_TRENDLINE(StockData['close']))
        # print(ta.MACD(StockData['close']))
        # print(self.AlgoData['MA50'])
        self.AlgoData['upperband'],self.AlgoData['middleband'],self.AlgoData['lowerband'] = ta.BBANDS(StockData['close'], timeperiod=5, nbdevup=2, nbdevdn=2, matype=0)

        # print(self.AlgoData)

        #Variables to store most recent 2 stock data and algo data 
        self.curStockData = StockData.iloc[-1]
        self.curAlgoData = self.AlgoData.iloc[-1]
        self.lastAlgoData = self.AlgoData.iloc[-2]


        self.AlgoData['time'] = StockData['time']

        self.curAlgoData = self.AlgoData.iloc[-1]
