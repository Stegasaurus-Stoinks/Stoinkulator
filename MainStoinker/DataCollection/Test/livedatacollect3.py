from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.common import BarData
from ibapi.common import *
from csv import writer
from datetime import datetime

import numpy as np
import pandas as pd

import threading
import time


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
        # self.timeformat = datetime.strptime(bar.date, "%Y%b%d %H:%M:%S")
        candleData = [datetime.fromtimestamp(int(bar.date)), bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]
        self.datadict[reqId] = self.datadict[reqId].append([candleData], ignore_index=True)

    # print(candleData)
    # print(self.test)
    # np.append(self.dataArray,[candleData])

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)

        # print(self.df)
        f = open("./MainStoinker/DataCollection/Test/liveData_"+self.tickers[reqId]+".csv", "w")
        self.datadict[reqId].to_csv(f, index=False, header=False, lineterminator='\n')
        f.close()
        print("All Historical Data Collected: Live Data Starting Now...")
        # self.disconnect()


    def historicalDataUpdate(self, reqId: int, bar: BarData):
        # if self.lastbardict[reqId] != 0:
        # print(self.lastbardict)
        if self.lastbardict[reqId]:
            if (bar.average != self.lastbardict[reqId].average):
                print("HistoricalDataUpdate. ReqId:", reqId, "BarData.", bar)
        else:
            print("empty :D Probably becauseu its the first loop")
            self.lastbardict[reqId] = bar

    # def realtimeBar(self, reqId: TickerId, time:int, open_: float, high: float, low: float, close: float,
    #                          volume: Decimal, wap: Decimal, count: int):
    #     super().realtimeBar(reqId, time, open_, high, low, close, volume, wap, count)
    #     print("RealTimeBar. TickerId:", reqId, RealTimeBar(time, -1, open_, high, low, close, volume, wap, count))
    

    def error(self, reqId, errorCode, errorString):
        print("Error. Id: ", reqId, " Code: ", errorCode, " Msg: ", errorString)

    def startData(self, tickers, duration):
        i = 0
        self.datadict = {}
        self.lastbardict = {}
        self.tickers = tickers
        for ticker in tickers:

            contract = Contract()
            contract.secType = "STK"
            contract.symbol = ticker
            contract.currency = "USD"
            contract.exchange = "SMART"

            self.reqHistoricalData(i, contract, "", duration, "1 min", "TRADES", 1, 2, True, [])

            f = open("./liveData_"+ticker+".csv", "w")
            f.truncate()
            f.close()
            
            self.datadict[i] = pd.DataFrame()
            self.lastbardict[i] = 0
            i += 1
