from csv import writer
from datetime import datetime
from datetime import timedelta
import talib as ta
import time
from enum import Enum

from MainStoinker.DataCollection.apiApi import IBapi
import MainStoinker.MainStuff.main_utils as utils
from MainStoinker.Algos.Elliot import ElliotImpulse as ElliotImpulse

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
        # setup that needs to be done in every algo is done here
        super().pre_update(StockData)

        self.AlgoData['mins'] = StockData.iloc[argrelextrema(StockData.close.values, np.less_equal, order=self.order)[0]]['close']
        print(StockData)
        print(self.AlgoData)
        self.AlgoData['maxs'] = StockData.iloc[argrelextrema(StockData.close.values, np.greater_equal, order=self.order)[0]]['close']













