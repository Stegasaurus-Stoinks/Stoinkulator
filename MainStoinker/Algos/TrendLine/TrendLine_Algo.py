from csv import writer
from datetime import datetime
from datetime import timedelta
import talib as ta
import time
from enum import Enum

from MainStoinker.DataCollection.apiApi import IBapi
import MainStoinker.MainStuff.main_utils as utils
import numpy as np
import pandas as pd
import math

from scipy.signal import argrelextrema

from MainStoinker.Algos.ParentAlgo import ParentAlgo

'''
ALGO PLAN/IDEA:

Okokokokokok
Step 1: Trendline with breaks indication
        Trying to figure this shit out...
Step 2: TRAMA indicator (Trend Regularity Adaptive Moving Average)
        IMPLEMENTATION: https://luxalgo.medium.com/trend-regularity-adaptive-moving-average-b4e05707f739
Step 3: RSI (over bought: above 70 or over sold indicator: below 70)

Uptrade logic: Price above TRAMA, wait for a downward trendline break to the upside, on break, buy, hold until RSI is overbought
Downtrade logic: Price below TRAMA, wait for upward trendline break to the downside, on break, short, hold until RSI is oversold

'''

RSIUpperThreshold = 70
RSILowerThreshold = 30


class Algo(ParentAlgo):
    def __init__(self, algoConfigData):
        super().__init__(algoConfigData)

        #Initialize Algo with unique data from Algo Config
        self.short = int(algoConfigData['short'])
        self.long = int(algoConfigData['long'])

        self.ibape = IBapi()

        #Data to send to the frontend
        self.FrontEndDataStruct = ['mins','maxs','random','MA20','StopPrice',"Trade"]
        self.FrontEndDataType = ['marker-up','marker-down','marker-dot','line','segment','baseline']

        #Data frame to store data for Algo ( Uses Front End Data Struct to create dataframe, can add whaterver you want also))  
        self.DataColumns = ['time'] + self.FrontEndDataStruct
        self.AlgoData = pd.DataFrame(columns=self.DataColumns)
        # print(self.AlgoData.shape)
        # test commit changes
        # nothing to see here

        


        

    def update(self, StockData):


        # check if they are the same size, probably dont need this since they should only be called when theres a line added
        if StockData.shape[0] != self.AlgoData.shape[0]:
            diff = StockData.shape[0] - self.AlgoData.shape[0]
            new_row = pd.DataFrame(index=range(diff),columns=self.DataColumns)
            self.AlgoData = pd.concat([self.AlgoData.loc[:],new_row],ignore_index=True)
            
        
        self.AlgoData['MA20'] = ta.HT_TRENDLINE(StockData['close'])

        n = 5

        self.AlgoData['mins'] = StockData.iloc[argrelextrema(StockData.close.values, np.less_equal, order=n)[0]]['close']
        print(self.AlgoData['mins'])
        print(StockData)
        print(self.AlgoData)
        self.AlgoData['maxs'] = StockData.iloc[argrelextrema(StockData.close.values, np.greater_equal, order=n)[0]]['close']
        self.AlgoData['random'] = StockData.iloc[argrelextrema(StockData.close.values, np.less_equal, order=n+2)[0]]['close']

        
        self.RSI = ta.RSI(StockData['close'],14)
        # print(type(self.RSI))
        self.curRSI = self.RSI.iat[-1]

        if self.curRSI > RSIUpperThreshold:
            self.RSIState = "OVERBOUGHT"
        elif self.curRSI < RSILowerThreshold:
            self.RSIState = "OVERSOLD"
        else:
             self.RSIState = "NEUTRAL"

        print("RSI: ", self.curRSI ," - ", self.RSIState)




        # print(ta.HT_TRENDLINE(StockData['close']))
        self.AlgoData['upperband'],self.AlgoData['middleband'],self.AlgoData['lowerband'] = ta.BBANDS(StockData['close'], timeperiod=5, nbdevup=2, nbdevdn=2, matype=0)
        # print(self.AlgoData)

        #Variables to store most recent 2 stock data and algo data 
        self.curStockData = StockData.iloc[-1]
        self.curAlgoData = self.AlgoData.iloc[-1]
        self.lastAlgoData = self.AlgoData.iloc[-2]


        self.AlgoData['time'] = StockData['time']

        self.curAlgoData = self.AlgoData.iloc[-1]

