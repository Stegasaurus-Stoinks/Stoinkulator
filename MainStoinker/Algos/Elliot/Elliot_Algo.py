from csv import writer
from datetime import datetime
from datetime import timedelta
import talib as ta
import time
from enum import Enum

from MainStoinker.DataCollection.apiApi import IBapi
import MainStoinker.MainStuff.main_utils as utils
from MainStoinker.Algos.Elliot import ElliotImpulse
from MainStoinker.Algos.Elliot import ElliotFuncs_new as ElliotFuncs

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

        # setting mins and maxs for plotting 
        self.AlgoData['mins'] = StockData.iloc[argrelextrema(StockData.close.values, np.less_equal, order=self.order)[0]]['close']
        self.AlgoData['maxs'] = StockData.iloc[argrelextrema(StockData.close.values, np.greater_equal, order=self.order)[0]]['close']
        # pruning NaNs from mins and maxs in AlgoData, for more usable data
        # creates pandas df with [time, mins] and [time, maxs] where all min/max values are real values
        self.mins = self.AlgoData.loc[pd.notnull(self.AlgoData.mins)][['time', 'mins']].rename(columns={'mins':'price'})
        self.maxs = self.AlgoData.loc[pd.notnull(self.AlgoData.maxs)][['time', 'maxs']].rename(columns={'maxs':'price'})

        tradingWave = self.elliot_recursive_blast()


        super().post_update()





    ############ Big boy function. father of all functions. Tamper with if you dare. A single wrong change will cause a cataclysmic chain of events

    def elliot_recursive_blast(self, plotSize, n, startX=np.NaN, endX=np.NaN, level=0):
        # Uncomment this for debugging recursive stuff
        #print("level: ",level)
        #Calculating mins and maxs
        #print("startX:",startX)
        #print("endX:",endX)
        #print("BEFORE RODER: ",n)

        global seg1top
        
        if np.isnan(startX): #if this is a fresh blast
            o = n #then set order = base order
        else: #else, we are in a recursive blast
            o = round(n/3) #Adjust this to add more or less mins and maxs (2 was the best one I found for short term)
            if o == 0:
                return list()
            #print("CURRENT RODER: ",o)
        
        ilocs_min = argrelextrema(backtest.low.values, np.less_equal, order=o)[0]
        ilocs_max = argrelextrema(backtest.high.values, np.greater_equal, order=o)[0]
        print(ilocs_min)
        #print(ilocs_max)

        #array of min and max plotpoints
        #fill array with nan's first, then replace nan's with min and max values where necessary

        mins = [np.NaN] * plotSize
        for i in range (0,len(ilocs_min)):
            if ilocs_min[i] < len(mins):
                mins[ilocs_min[i]] = backtest.iloc[ilocs_min[i]].low * 0.9999

        maxs = [np.NaN] * plotSize
        for i in range (0,len(ilocs_max)):
            if ilocs_max[i] < len(maxs):
                maxs[ilocs_max[i]] = backtest.iloc[ilocs_max[i]].high * 1.0001

        

        reach = 3
        finishedWaves = list()
        tradingWaves = list()

        #for every min in chart
        for index, min in self.mins.iterrows():
            if(1):
            #try:
                #temp block checks if we are in a recursive function and already have a startX. We only want to be checking elliots with that startX
                temp = False
                if not np.isnan(startX):
                    if min.time != startX:
                        temp = True
                if temp is True:
                    continue
                wave = ElliotImpulse(plotSize)
                wave.time_1 = min.time
                wave.price_1 = min.price
                
                #checking wave 1/point 2 [ / ]
                ilocs_max_valid = ElliotFuncs.find_line(endX,wave.time_1,ilocs_max)
                for curPoint in ilocs_max_valid[0:reach+1]:
                    if(wave.checkpoint2(curPoint,maxs[curPoint], mins)):
                        wave.time_2 = curPoint
                        wave.price_2 = maxs[curPoint]

                        #checking wave 2/point 3 [ /\ ]
                        ilocs_min_valid = ElliotFuncs.find_line(endX,wave.time_2,ilocs_min)
                        for curPoint in ilocs_min_valid[0:reach+1]:
                            if(wave.checkpoint3(curPoint,mins[curPoint], maxs,mins)):
                                wave.time_3 = curPoint
                                wave.price_3 = mins[curPoint]
                                
                                ElliotFuncs.check_future_points(curPoint, ilocs_min_valid,reach,tradingWaves,wave)
                                
                                #checking wave 3/point 4 [ /\/ ]
                                ilocs_max_valid = ElliotFuncs.find_line(endX,wave.time_3,ilocs_max)
                                for curPoint in ilocs_max_valid[0:reach+1]:
                                    if(wave.checkpoint4(curPoint,maxs[curPoint], mins)):
                                        wave.time_4 = curPoint
                                        wave.price_4 = maxs[curPoint]

                                        ElliotFuncs.check_future_points(curPoint, ilocs_max_valid,reach,tradingWaves,wave)

                                        #checking wave 4/point 5 [ /\/\ ]
                                        ilocs_min_valid = ElliotFuncs.find_line(endX,wave.time_4,ilocs_min)
                                        for curPoint in ilocs_min_valid[0:reach+1]:
                                            if(wave.checkpoint5(curPoint,mins[curPoint],maxs)):
                                                wave.time_5 = curPoint
                                                wave.price_5 = mins[curPoint]
                                                
                                                ElliotFuncs.check_future_points(curPoint, ilocs_min_valid,reach,tradingWaves,wave)

                                                #checking wave 5/point 6 [ /\/\/ ]
                                                ilocs_max_valid = ElliotFuncs.find_line(endX,wave.time_5,ilocs_max)
                                                for curPoint in ilocs_max_valid[0:reach+1]:
                                                    if(wave.checkpoint6(curPoint,maxs[curPoint],mins)):
                                                        wave.time_6 = curPoint
                                                        wave.price_6 = maxs[curPoint]
                                                        finishedWaves.append(ElliotImpulse(wave.plotSize,wave.time_1,wave.price_1,wave.time_2,wave.price_2,wave.time_3,wave.price_3,wave.time_4,wave.price_4,wave.time_5,wave.price_5,wave.time_6,wave.price_6))
                                                            
                                                        # possWaves1 = elliotRecursiveBlast(backtest,plotSize,o,wave.time_1,wave.time_2,level+1)
                                                        # if possWaves1 != []:
                                                        #     possibleWaves.extend(possWaves1)
                                                        # possWaves3 = elliotRecursiveBlast(backtest,plotSize,o,wave.time_3,wave.time_4,level+1)
                                                        # if possWaves1 != []:
                                                        #     possibleWaves.extend(possWaves3)
                                                        
                                                        #waveplot = wave.assemble()
                                                        #print(wave.printdata())
                                                        #print(waveplot)
                                                        #extraplots.append(plotting.make_addplot(waveplot,ax=atime_1))        
            
            else:
            #except:       
                print("something broke in the try thingy")
        return finishedWaves,tradingWaves
