#This is my Ticker Class :D

import pandas as pd 
from datetime import datetime, timedelta
import time

class Ticker:

    def __init__(self, symbol, type, Database, dataSize = 100, startDate = 0, endDate = 0):
        self.symbol = symbol
        self.type = type
        self.status = "Not Initialized"
        
        #local array size
        self.dataSize = dataSize
        #backtest length minus the local array size
        self.length = 500
        if startDate == 0:
            self.firstDate = '2022-07-05'
            self.lastDate = '2022-07-07'
        else:
            self.firstDate = startDate
            self.lastDate = endDate

        #iteration to keep track of backtest
        self.iteration = 0
        #for timing the backtest
        self.exe_start_time = 0
        self.exe_end_time = 0

        #idk if these are gonna be calculated here yet
        self.EMA10 = 0
        self.EMA20 = 0
        self.EMA50 = 0
        self.EMA200 = 0

        #This object will be the only one communicating with the database
        self.DataBase = Database

        self.tradeList = pd.DataFrame()

        self.dayVolume = 0
        self.avgVolume = 0

        self.Current_time = datetime(2020, 11, 18, 18, 30, 0)
        self.Last_time = datetime(2020, 11, 18, 18, 30, 0)

        #Start and End times of normal market hours(just place holders, they are configured correctly below, trust me)
        self.DAY_START_TIME = datetime(2020, 11, 18, 18, 30, 0)
        self.DAY_END_TIME = datetime(2020, 11, 18, 18, 30, 0)

        self.validTradingHours = False

        self.Data_errors = 0



    #main update function
    def update(self, PrintStats = False):
        """Call this Function to update the local data array with new data from database or backtest data"""
        #print("Update the local data array")
        self.Last_time = self.Current_time


        #Update for BACKTEST Data
        if self.DataBase.BackTest:
            if self.exe_start_time == 0:
                self.exe_start_time = time.time()

            self.iteration += 1
            if PrintStats:
                print("Update local array with backtest data: ",self.iteration ,"/",self.length)
            self.AM_candlesticks = self.BackTestAM_candlesticks.iloc[self.length-self.dataSize-self.iteration:self.length-self.iteration]
            if self.iteration+self.dataSize >= self.BackTestAM_candlesticks.shape[0]:
                self.exe_end_time = time.time()
                print("Backtesting Complete!")
                print("Backtesting " + str(self.iteration) + " points took " + str(self.exe_end_time-self.exe_start_time) + " seconds")
                quit()


        #Update for LIVE Data
        else:
            data = self.queryNewData()
            self.Checked_data = self.checkData(data)
            print(self.Checked_data[0])

            series = pd.Series(self.Checked_data[0][1:], index = self.AM_candlesticks.columns, name=self.Checked_data[0][0])

            self.AM_candlesticks = self.AM_candlesticks.append(series)
            self.AM_candlesticks = self.AM_candlesticks.sort_index(ascending=False)

        #Define the valid trading hours based on the first dataset pulled in
        self.Current_time = self.AM_candlesticks.index[0]
        if self.Current_time.day is not self.DAY_START_TIME.day:
            self.DAY_START_TIME = self.Current_time.replace(hour=9, minute=30)
            self.DAY_END_TIME = self.Current_time.replace(hour=16, minute=00)

        #check valid trading hours
        self.Current_time = self.AM_candlesticks.index[0]
        two_minutes = timedelta(minutes = 2)
        #print(self.Current_time, self.DAY_START_TIME, self.DAY_END_TIME)
        #print(self.Current_time.day, self.DAY_START_TIME.day)
        if (self.Current_time < self.DAY_END_TIME - two_minutes) and (self.Current_time > self.DAY_START_TIME):
            self.validTradingHours = True

        else:
            self.validTradingHours = False




    def queryNewData(self):
        print("call query function for specific type")
        data = self.DataBase.QueryLast(self.symbol, 1)
        return data



    def checkData(self, data):
        print("check to make sure the new point is actually differnt from the last")
        self.Current_time = data.index
        if self.Last_time != self.Current_time: #Check if new timestamp is not equal to last time
            print("date time stamp not same")
            return(data)

        else:
            self.Data_errors += 1
            print("Possible data collection error | " + str(self.Data_errors))
            #Need to change timestamp to one minute ahead, but keep rest of the data.  This will help fill in missing data
            modified_data = data
            return(modified_data) 



    def warmUp(self):
        #warm up indicators and complete initial fill of data array
        print("Warm Up " + self.symbol)

        #Update for BACKTEST Data
        if self.DataBase.BackTest:
            #length and type of backtest
            data = self.DataBase.QueryDate(self.symbol, self.firstDate, self.lastDate)
            #data = self.DataBase.QueryLast(self.symbol, self.length)
            self.BackTestAM_candlesticks = pd.DataFrame(data)
            self.BackTestAM_candlesticks.columns = ['time','symbol','volume','open','high','close','low','unix']
            self.BackTestAM_candlesticks['datetime'] = pd.to_datetime(self.BackTestAM_candlesticks['time'])
            self.BackTestAM_candlesticks = self.BackTestAM_candlesticks.set_index('datetime')
            self.BackTestAM_candlesticks.drop(['time'], axis=1, inplace=True)
            self.BackTestAM_candlesticks = self.BackTestAM_candlesticks.between_time('8:00', '16:30')

            self.AM_candlesticks = self.BackTestAM_candlesticks.iloc[self.length-self.dataSize:self.length]
            self.length = self.BackTestAM_candlesticks.shape[0]

        #Update for LIVE Data
        else:
            data = self.DataBase.QueryLast(self.symbol, self.dataSize)
            #Pandas Array for local data storage
            self.AM_candlesticks = pd.DataFrame(data)
            self.AM_candlesticks.columns = ['time','symbol','volume','open','high','close','low','unix']
            self.AM_candlesticks['datetime'] = pd.to_datetime(self.AM_candlesticks['time'])
            self.AM_candlesticks = self.AM_candlesticks.set_index('datetime')
            self.AM_candlesticks.drop(['time'], axis=1, inplace=True)

        self.status = "Initialized"



    def getData(self, type = "LAST"):
        """
        Pull current price for asset
        3 Options for type
        - LAST - Returns the single most recent data point (Default)
        - HEAD - Returns the last 5 data points
        - FULL - Returns the entire data array
        """
        if type == "LAST":
            return(self.AM_candlesticks.iloc[0])

        if type == "HEAD":
            return(self.AM_candlesticks.head())

        if type == "FULL":
            return(self.AM_candlesticks)



    def getStatus(self):
        print(self.symbol + " is @ " + str(self.AM_candlesticks.iloc[0]['close']) + " Time: " + str(self.Current_time) + "Valid Trading Hours: " + str(self.validTradingHours))
        #print(self.AM_candlesticks.head(1))
        return self.status
        
    def toString(self):
        return self.symbol

    def addTrade(self):
        print("Trade recorded - Well not really this method still need to be implemented")
        #function will add trade to trade list which will then be sent to database, or maybe just send the newest trade? idk