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

from MainStoinker.TradeTools.trade import Trade

from MainStoinker.Algos.ParentAlgo import ParentAlgo



class Algo(ParentAlgo):
    def __init__(self, algoConfigData):
        super().__init__(algoConfigData)

        #Initialize Algo with unique data from Algo Config
        # order, srictness?, recursion
        # self.recursion = bool(algoConfigData['recursion'])
        self.order = int(algoConfigData['order'])

        #Data to send to the frontend
        self.FrontEndDataStruct = ['mins','maxs','StopPrice',"Trade"]
        self.FrontEndDataType = ['marker-up','marker-down','segment','baseline']

        #Data frame to store data for Algo ( Uses Front End Data Struct to create dataframe, can add whaterver you want also))  
        self.DataColumns = ['time'] + self.FrontEndDataStruct
        self.AlgoData = pd.DataFrame(columns=self.DataColumns)
        # print(self.AlgoData.shape)


    def update(self, StockData):
        # setup that needs to be done in every algo. Should turn this into parent functions
        # -------------------------------------------------------------------------------------------
        # check if they are the same size, probably dont need this since they should only be called when theres a line added
        if StockData.shape[0] != self.AlgoData.shape[0]:
            diff = StockData.shape[0] - self.AlgoData.shape[0]
            new_row = pd.DataFrame(index=range(diff),columns=self.DataColumns)
            self.AlgoData = pd.concat([self.AlgoData.loc[:],new_row],ignore_index=True)
        self.AlgoData['time'] = StockData['time']

        # -----------------------------------------------------------------------------------------

        self.AlgoData['mins'] = StockData.iloc[argrelextrema(StockData.close.values, np.less_equal, order=self.order)[0]]['close']
        print(StockData)
        print(self.AlgoData)
        self.AlgoData['maxs'] = StockData.iloc[argrelextrema(StockData.close.values, np.greater_equal, order=self.order)[0]]['close']













        # -----------Run at end of update for every algo ever----------------------
        #Variables to store most recent stock data and algo data 
        self.curStockData = StockData.iloc[-1]
        self.curAlgoData = self.AlgoData.iloc[-1]