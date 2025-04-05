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



class Algo(ParentAlgo):
    def __init__(self, algoConfigData):
        super().__init__(algoConfigData)

        self.order = 2
        self.i = 0
        #Data to send to the frontend
        self.FrontEndDataStruct = ['mins','maxs','StopPrice',"Trade"]
        self.FrontEndDataType = ['marker-up','marker-down','segment','baseline']

        #Data frame to store data for Algo ( Uses Front End Data Struct to create dataframe, can add whaterver you want also))  
        self.DataColumns = ['time'] + self.FrontEndDataStruct
        self.AlgoData = pd.DataFrame(columns=self.DataColumns)
        # print(self.AlgoData.shape)


    def update(self, StockData):
        # setup that needs to be done in every algo is done here
        super().pre_update(StockData)
        self.logger.debug("i = "+str(self.i))

        # setting mins and maxs for plotting 
        self.AlgoData['mins'] = StockData.iloc[argrelextrema(StockData.close.values, np.less_equal, order=self.order)[0]]['close']
        self.AlgoData['maxs'] = StockData.iloc[argrelextrema(StockData.close.values, np.greater_equal, order=self.order)[0]]['close']
        
        tp = self.curStockData['close'] + 3.00
        stopPrice = (self.curStockData['close'] - 1.00)

        if (self.inTrade):
            self.trade.update_stoploss_price(stopPrice)
            
            self.AlgoData.at[self.AlgoData.index[-1],'StopPrice'] = self.trade.stopPrice
            self.AlgoData.at[self.AlgoData.index[-1],'Trade'] = self.curStockData['close']
            self.AlgoData.at[self.AlgoData.index[-1],'tp'] = tp
            if (self.i == 3):
                self.trade.close_position(self.curStockData['close'],self.curStockData['date'])
                self.i = 0
        else:
            if (self.i == 1):
                self.trade = self.open_trade(10, 1)
                #change stoploss to fixed type and set price
                stopPrice = (self.curStockData['close'] - 1.00)
                self.trade.set_stopLossType('Fixed')
                self.trade.set_stopPrice(stopPrice)
                self.trade.create_tp(tp, 10)

                self.AlgoData.at[self.AlgoData.index[-1],'StopPrice'] = self.trade.stopPrice
                self.AlgoData.at[self.AlgoData.index[-1],'Trade'] = self.curStockData['close']
                self.AlgoData.at[self.AlgoData.index[-1],'tp'] = tp



        self.i += 1
        super().post_update()