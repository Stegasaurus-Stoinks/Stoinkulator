from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.common import BarData
from ibapi.common import *
from csv import writer
from datetime import datetime
from datetime import timedelta

import Start_config as config

import EMACrossing_Algo

import numpy as np
import pandas as pd

import threading
import time

import json

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
        candleData = [datetime.fromtimestamp(int(bar.date)),int(bar.date), bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]

        if(self.liveData):
            self.datadict[reqId] = self.datadict[reqId].append([candleData], ignore_index=True)
        else:
            self.simulatedDatadict[reqId] = self.simulatedDatadict[reqId].append([candleData], ignore_index=True)


    def historicalDataEnd(self, reqId: int, start: str, end: str):
        # print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)
        
        
        if(self.liveData):
            self.datadict[reqId].columns=['date','time','open','high','low','close','volume','average']
            print(self.datadict[reqId])
            print("All Historical Data Collected: Live Data Starting Now...")
            # print(self.getDataJson())
                # nothing else is needed here because historical data was set to keep live data
                   
        else:
            self.simulatedDatadict[reqId].columns=['date','time','open','high','low','close','volume','average']
            print("Historical Data Collected for " + self.tickers[reqId])
            self.datacollectednum += 1
            # print(self.simulatedDatadict[reqId])
            # Create datadict data frame here
            # ____________________________________________________________________________________
            firstDate = self.simulatedDatadict[reqId].at[0,'date']
            startDate = firstDate + timedelta(days=self.warmup)
            print("Warmup Start Date: " + str(firstDate))
            print("Warmup End Date: " + str(startDate))
            self.datadict[reqId] = self.simulatedDatadict[reqId].loc[(self.simulatedDatadict[reqId]['date'] < startDate)]
            self.datadict[reqId].columns=['date','time', 'open','high','low','close','volume','average']
            # print(self.datadict[reqId])

            if self.datacollectednum >= len(self.tickers): #all historical data collected
                print("------All Historical Data Collected------")
                print("---Simulated Live Data Starting Now...---")

            self.backtestingDataUpdate()


    def historicalDataUpdate(self, reqId: int, bar: BarData):
        # if self.lastbardict[reqId] != 0:
        # print(self.lastbardict)
        updatecsv = False
        data = {'date':[datetime.fromtimestamp(int(bar.date))],
                'time':[int(bar.date)],
                'open':[bar.open],
                'high':[bar.high],
                'low':[bar.low],
                'close':[bar.close],
                'volume':[bar.volume],
                'average':[bar.average]}
        newdata = pd.DataFrame(data)
        #candleData = [datetime.fromtimestamp(int(bar.date)), bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]
        if self.lastbardict[reqId]:
            if bar.date == self.lastbardict[reqId].date:
                if (bar.average != self.lastbardict[reqId].average):
                    print("IntraMinute Update: HistoricalDataUpdate. ReqId:", reqId, "BarData.", bar)
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
    


    def error(self, reqId, errorCode, errorString):
        print("Error. Id: ", reqId, " Code: ", errorCode, " Msg: ", errorString)


    def backtestingDataUpdate(self):
        startpoint = self.datadict[0].shape[0]
        numpoints = self.simulatedDatadict[0].shape[0] - startpoint
        print("Running data loop for " + str(numpoints) + " points of data")
        Fulldata = self.getDataJson(index = 0)
        self.socket.emit('data_send', Fulldata)


        starttime = datetime.now()
        for k in range(numpoints):
            for i in range(len(self.tickers)):
                # print("Looping data for " + self.tickers[i])
                entry = self.simulatedDatadict[i].iloc[startpoint+k]
                print(entry[0])

                self.datadict[i] = pd.concat([self.datadict[i],pd.DataFrame.from_records([entry])],ignore_index=True)

                # time.sleep(.1)
                
                sendData = { "time":float(entry['time']), "open":float(entry['open']),"high":float(entry['high']),"low":float(entry['low']),"close":float(entry['close']),"volume":float(entry['volume'])}
                # print(sendData)
                self.socket.emit('update_send',sendData)
                # algo update  stuffs
                # for algo in self.algos:
                #     algo.update(self.getData())



        endtime = datetime.now()
        duration = endtime-starttime
        print("Backtesting "+str(numpoints)+" Points is Done!")
        print("Duration: " + str(duration))
        print("___________________________________________________________")
        print("--------------Press 'CTRL' to Close Program----------------")
        print("___________________________________________________________")


    

    def startData(self,socket, tickers, algos, warmup, liveData, duration=1,):
        i = 0
        self.datadict = {}
        self.simulatedDatadict = {}
        self.lastbardict = {}
        self.tickers = tickers
        self.algos = algos
        self.liveData = liveData
        self.warmup = warmup
        self.socket = socket
        for ticker in tickers:

            contract = createStockContact(ticker)

            if(self.liveData):
                self.reqHistoricalData(i, contract, "", str(warmup) + " D", "1 min", "TRADES", 1, 2, True, [])
                

            else:
                self.datacollectednum = 0 #variable to track completed historical data pulls
                self.reqHistoricalData(i, contract, "", str(warmup+duration) + " D", "1 min", "TRADES", 1, 2, False, [])
                self.simulatedDatadict[i] = pd.DataFrame()


            # writing data to csv files:

            # if config.FrontEndDisplay:
            #     f = open("./MainStoinker/DataCollection/Test/Collected_Data/liveData_"+ticker+".csv", "w")
            #     f.truncate()
            #     f.close()
            
            self.datadict[i] = pd.DataFrame()
            self.lastbardict[i] = 0
            i += 1


    def getData(self):
        return self.datadict
    
    def get1DataPoint(self,index):
        return self.datadict[0].iloc[index]
    
    def printAlgoStats(self, FullPrint = True):
        i = 0
        for algo in self.algos:
            print(f"Printing Stats for Algo {i}")
            algo.printStats(FullPrint)
            i += 1

    def getDataJson(self,index):
        result = self.datadict[index].to_json(orient="records")
        return(result)     
        parsed = json.loads(result)
        return(json.dumps(parsed, indent=4))

            
