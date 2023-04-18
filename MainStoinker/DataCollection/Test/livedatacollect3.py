from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.common import BarData
from ibapi.common import *
from csv import writer
from datetime import datetime
from datetime import timedelta

import numpy as np
import pandas as pd

import threading
import time

def createStockContact(ticker: str):
    contract = Contract()
    contract.secType = "STK"
    contract.symbol = ticker
    contract.currency = "USD"
    contract.exchange = "SMART"

    return contract


class IBapi(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        
                
    def tickPrice(self, reqId, tickType, price, attrib):
        if tickType == 2 and reqId == 1:
            print('The current ask price is: ', price)

    def nextValidId(self, orderId: int):
        print("Setting nextValidOrderId: %d", orderId)
        self.nextValidOrderId = orderId
        # self.startData("AAPL")

    def historicalData(self, reqId: int, bar: BarData):
        # print("HistoricalData. ReqId:", reqId, "BarData.", bar)
        candleData = [datetime.fromtimestamp(int(bar.date)), bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]

        if(self.liveData):
            self.datadict[reqId] = self.datadict[reqId].append([candleData], ignore_index=True)
        else:
            self.simulatedDatadict[reqId] = self.simulatedDatadict[reqId].append([candleData], ignore_index=True)


    def historicalDataEnd(self, reqId: int, start: str, end: str):
        # print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)
        
        
        if(self.liveData):
            self.datadict[reqId].columns=['Date','Open','High','Low','Close','Volume','Average']
            print("All Historical Data Collected: Live Data Starting Now...")
                # nothing else is needed here because historical data was set to keep live data
                   
        else:
            self.simulatedDatadict[reqId].columns=['Date','Open','High','Low','Close','Volume','Average']
            print("Historical Data Collected for " + self.tickers[reqId])
            self.datacollectednum += 1
            print(self.simulatedDatadict[reqId])
            # Create datadict data frame here
            # ____________________________________________________________________________________
            firstDate = self.simulatedDatadict[reqId].at[0,'Date']
            startDate = firstDate + timedelta(days=self.warmup)
            print(firstDate)
            print(startDate)
            self.datadict[reqId] = self.simulatedDatadict[reqId].loc[(self.simulatedDatadict[reqId]['Date'] < startDate)]
            self.datadict[reqId].columns=['Date','Open','High','Low','Close','Volume','Average']
            print(self.datadict[reqId])

            if self.datacollectednum >= len(self.tickers): #all historical data collected
                print("------All Historical Data Collected------")
                print("---Simulated Live Data Starting Now...---")

            self.backtestingDataUpdate()

            


        f = open("./MainStoinker/DataCollection/Test/Collected_Data/liveData_"+self.tickers[reqId]+".csv", "w")
        self.datadict[reqId].to_csv(f, index=False, header=False, lineterminator='\n')
        f.close()


            
        


    def historicalDataUpdate(self, reqId: int, bar: BarData):
        # if self.lastbardict[reqId] != 0:
        # print(self.lastbardict)
        updatecsv = False
        data = {'Date':[datetime.fromtimestamp(int(bar.date))],
                'Open':[bar.open],
                'High':[bar.high],
                'Low':[bar.low],
                'Close':[bar.close],
                'Volume':[bar.volume],
                'Average':[bar.average]}
        newdata = pd.DataFrame(data)
        #candleData = [datetime.fromtimestamp(int(bar.date)), bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]
        if self.lastbardict[reqId]:
            if bar.date == self.lastbardict[reqId].date:
                if (bar.average != self.lastbardict[reqId].average):
                    print("HistoricalDataUpdate. ReqId:", reqId, "BarData.", bar)
                    self.datadict[reqId].drop(self.datadict[reqId].tail(1).index,inplace=True)
                    self.datadict[reqId] = pd.concat([self.datadict[reqId],newdata],ignore_index=True)
                    # print(self.datadict[reqId])
                    updatecsv = True
            else:
                self.datadict[reqId] = pd.concat([self.datadict[reqId],newdata],ignore_index=True)
                print(self.datadict[reqId])
                updatecsv = True
        else:
            print("empty :D Probably becauseu its the first loop")

        self.lastbardict[reqId] = bar
        if updatecsv:
            f = open("./MainStoinker/DataCollection/Test/Collected_Data/liveData_"+self.tickers[reqId]+".csv", "w")
            self.datadict[reqId].to_csv(f, index=False, header=False, lineterminator='\n')
            f.close()

        

    

    def error(self, reqId, errorCode, errorString):
        print("Error. Id: ", reqId, " Code: ", errorCode, " Msg: ", errorString)

    def backtestingDataUpdate(self):
        startpoint = self.datadict[0].shape[0]
        numpoints = self.simulatedDatadict[0].shape[0] - startpoint
        print("running data loop for " + str(numpoints))
        for k in range(numpoints):
            for i in range(len(self.tickers)):
                print("Looping data for " + self.tickers[i])
                entry = self.simulatedDatadict[i].iloc[startpoint+k]
                print(entry)
                self.datadict[i] = pd.concat([self.datadict[i],entry],ignore_index=True)
                print(self.datadict[i])
                time.sleep(2)

        

    def startData(self, tickers, warmup, liveData, duration=10):
        i = 0
        self.datadict = {}
        self.simulatedDatadict = {}
        self.lastbardict = {}
        self.tickers = tickers
        self.liveData = liveData
        self.warmup = warmup
        for ticker in tickers:

            contract = createStockContact(ticker)

            if(self.liveData):
                self.reqHistoricalData(i, contract, "", str(warmup) + " D", "1 min", "TRADES", 1, 2, True, [])
                

            else:
                self.datacollectednum = 0 #variable to track completed historical data pulls
                self.reqHistoricalData(i, contract, "", str(warmup+duration) + " D", "1 min", "TRADES", 1, 2, False, [])
                self.simulatedDatadict[i] = pd.DataFrame()


            f = open("./MainStoinker/DataCollection/Test/Collected_Data/liveData_"+ticker+".csv", "w")
            f.truncate()
            f.close()
            
            self.datadict[i] = pd.DataFrame()
            self.lastbardict[i] = 0
            i += 1
            

            
